"""Build the SQLite index (texts, full-text search, lexica) from the cloned PerseusDL repositories."""

import html.entities
import io
import re
import sqlite3
import sys
import time
from pathlib import Path

from lxml import etree

from .textnorm import beta_to_unicode, looks_like_beta, normalize, squash

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DB_PATH = DATA / "perseus.db"

TEXT_REPOS = ["canonical-greekLit", "canonical-latinLit"]
TEI_NS = "http://www.tei-c.org/ns/1.0"
CTS_NS = "http://chs.harvard.edu/xmlns/cts"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

LEXICA = {
    "lsj": ("Liddell-Scott-Jones, A Greek-English Lexicon (1940)", "CTS_XML_TEI/perseus/pdllex/grc/lsj/grc.lsj.perseus-eng*.xml"),
    "ls": ("Lewis & Short, A Latin Dictionary (1879)", "CTS_XML_TEI/perseus/pdllex/lat/ls/lat.ls.perseus-eng2.xml"),
    "lewis": ("Lewis, An Elementary Latin Dictionary (1890)", "CTS_XML_TEI/perseus/pdllex/lat/viaf2845558/viaf001/*.xml"),
}

SCHEMA = """
CREATE TABLE versions (
    id INTEGER PRIMARY KEY, urn TEXT UNIQUE, work_urn TEXT, author TEXT, title TEXT,
    lang TEXT, kind TEXT, label TEXT, description TEXT, path TEXT, levels TEXT,
    n_passages INTEGER, search TEXT
);
CREATE TABLE passages (
    id INTEGER PRIMARY KEY, version_id INTEGER, seq INTEGER, ref TEXT, text TEXT
);
CREATE VIRTUAL TABLE fts USING fts5(norm, content='', tokenize="unicode61 remove_diacritics 0");
CREATE TABLE lexicon (
    id INTEGER PRIMARY KEY, lex TEXT, key TEXT, headword TEXT, norm TEXT, text TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

INDEXES = """
CREATE INDEX passages_version_seq ON passages(version_id, seq);
CREATE INDEX versions_work ON versions(work_urn);
CREATE INDEX lexicon_norm ON lexicon(lex, norm);
"""

PARSER = etree.XMLParser(recover=True, resolve_entities=False, load_dtd=False, no_network=True, huge_tree=True)
SKIP_TAGS = {"note", "teiHeader"}
PRED_RE = re.compile(r"""\[@n\s*=\s*['"]\$(\d)['"]\]""")


ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9.]*);")
XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
ENTITY_OVERRIDES = {"cdot": "\u00b7", "ymacr": "\u0233", "Ymacr": "\u0232", "emdash": "\u2014"}


def load_xml(path: Path) -> bytes:
    """File contents with the DTD-defined character entities (&aelig;, &umacr;, &mdash;) made literal.

    The DTDs are not shipped with the data, so the parser would otherwise drop these characters silently.
    """

    def expand(m: re.Match) -> str:
        name = m.group(1)
        if name in XML_ENTITIES:
            return m.group(0)
        return ENTITY_OVERRIDES.get(name) or html.entities.html5.get(name + ";", "")

    text = path.read_text(encoding="utf-8", errors="replace")
    return ENTITY_RE.sub(expand, text).encode("utf-8")


def parse_xml(path: Path):
    return etree.fromstring(load_xml(path), PARSER)


def local(el) -> str:
    tag = el.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def node_text(el) -> str:
    parts: list[str] = []

    def walk(node):
        if not isinstance(node.tag, str) or local(node) in SKIP_TAGS:
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(el)
    return squash("".join(parts))


def line_text(el) -> str:
    """Text of a verse line, prefixed with the speaker when it opens a speech."""
    text = node_text(el)
    parent = el.getparent()
    if parent is not None and local(parent) == "sp":
        first = next((c for c in parent if local(c) == "l"), None)
        if first is el:
            speaker = next((node_text(c) for c in parent if local(c) == "speaker"), "")
            if speaker:
                text = f"[{speaker}] {text}"
    return text


# --- catalogue ---------------------------------------------------------------


def read_catalog(repo: Path) -> dict[str, dict]:
    """Map version URN -> metadata from the __cts__.xml files."""
    catalog: dict[str, dict] = {}
    for group_file in sorted(repo.glob("data/*/__cts__.xml")):
        group = etree.parse(str(group_file), PARSER).getroot()
        name = group.find(f"{{{CTS_NS}}}groupname")
        author = squash(name.text or "") if name is not None else group_file.parent.name
        for work_file in sorted(group_file.parent.glob("*/__cts__.xml")):
            work = etree.parse(str(work_file), PARSER).getroot()
            if work is None:
                continue
            title_el = work.find(f"{{{CTS_NS}}}title")
            title = squash(title_el.text or "") if title_el is not None else work_file.parent.name
            for el in work:
                kind = local(el)
                if kind not in ("edition", "translation", "commentary") or not el.get("urn"):
                    continue
                label = el.find(f"{{{CTS_NS}}}label")
                desc = el.find(f"{{{CTS_NS}}}description")
                catalog[el.get("urn")] = {
                    "work_urn": work.get("urn"),
                    "author": author,
                    "title": title,
                    "kind": kind,
                    "lang": el.get(XML_LANG) or (work.get(XML_LANG) if kind == "edition" else None),
                    "label": squash(label.text or "") if label is not None else title,
                    "description": squash("".join(desc.itertext())) if desc is not None else "",
                }
    return catalog


# --- passage extraction ------------------------------------------------------


def cref_patterns(root, namespaced: bool) -> list[tuple[str, str]]:
    """(level name, xpath) for each cRefPattern, shallowest first."""
    found = []
    for el in root.iter():
        if local(el) != "cRefPattern":
            continue
        repl = el.get("replacementPattern") or ""
        m = re.match(r"#xpath\((.*)\)\s*$", repl, re.S)
        if not m:
            continue
        xpath = m.group(1)
        if not namespaced:
            xpath = xpath.replace("tei:", "").replace("/TEI/", f"/{local(root)}/")
        found.append((len(PRED_RE.findall(xpath)), el.get("n") or "", xpath))
    found.sort(key=lambda t: t[0])
    return [(name, xpath) for depth, name, xpath in found if depth]


def numbered_lines(nodes):
    """Yield (n, line), numbering lines that the edition leaves unnumbered."""
    counter = 0
    for node in nodes:
        n = node.get("n")
        if n and n.isdigit():
            counter = int(n)
        elif not n:
            counter += 1
            n = str(counter)
        yield n, node


def extract_by_pattern(root, xpath: str, ns: dict) -> list[tuple[str, str]]:
    pieces = PRED_RE.split(xpath)
    segments = pieces[0::2][: len(pieces) // 2]
    if not segments:
        return []
    out: list[tuple[str, str]] = []

    def descend(node, depth: int, prefix: list[str]):
        seg = segments[depth]
        is_last = depth == len(segments) - 1
        expr = seg if depth == 0 else "." + seg
        if is_last and re.search(r"(^|[/:])l$", seg):
            children = list(numbered_lines(node.xpath(expr, namespaces=ns)))
        else:
            children = [(c.get("n"), c) for c in node.xpath(expr + "[@n]", namespaces=ns)]
        if not children and depth > 0:
            text = node_text(node)
            if text:
                out.append((".".join(prefix), text))
            return
        for n, child in children:
            ref = prefix + [n.strip()]
            if is_last:
                text = line_text(child) if local(child) == "l" else node_text(child)
                if text:
                    out.append((".".join(ref), text))
            else:
                descend(child, depth + 1, ref)

    descend(root, 0, [])
    return out


def extract_generic(root) -> tuple[list[tuple[str, str]], str]:
    """Fallback for files with no usable cRefPattern: follow the nested numbered divs."""
    body = next((el for el in root.iter() if local(el) == "body"), None)
    if body is None:
        return [], ""
    out: list[tuple[str, str]] = []
    names: dict[int, str] = {}

    def is_div(el) -> bool:
        return local(el).startswith("div")

    def child_divs(node):
        divs = []
        for child in node:
            if not is_div(child):
                continue
            if child.get("n") and child.get("type") not in ("edition", "translation", "commentary"):
                divs.append(child)
            else:
                divs.extend(child_divs(child))
        return divs

    def descend(node, prefix: list[str]):
        divs = child_divs(node)
        if not divs:
            text = node_text(node)
            if text and prefix:
                out.append((".".join(prefix), text))
            return
        for div in divs:
            names.setdefault(len(prefix), (div.get("subtype") or div.get("type") or "section").lower())
            descend(div, prefix + [div.get("n").strip()])

    descend(body, [])
    if out:
        return out, ".".join(names[i] for i in sorted(names))
    paragraphs = [node_text(p) for p in body.iter() if local(p) in ("p", "l")]
    return [(str(i), t) for i, t in enumerate((p for p in paragraphs if p), 1)], "paragraph"


def extract(path: Path) -> tuple[list[tuple[str, str]], str]:
    root = parse_xml(path)
    if root is None:
        return [], ""
    namespaced = root.tag.startswith("{")
    ns = {"tei": TEI_NS} if namespaced else {}
    patterns = cref_patterns(root, namespaced)
    if patterns:
        try:
            passages = extract_by_pattern(root, patterns[-1][1], ns)
        except etree.XPathError:
            passages = []
        if passages:
            return passages, ".".join(name.lower() or "?" for name, _ in patterns)
    return extract_generic(root)


def index_texts(db: sqlite3.Connection) -> None:
    pid = 0
    for repo_name in TEXT_REPOS:
        repo = DATA / repo_name
        catalog = read_catalog(repo)
        namespace = "greekLit" if "greek" in repo_name else "latinLit"
        files = sorted(p for p in repo.glob("data/*/*/*.xml") if p.name != "__cts__.xml")
        for i, path in enumerate(files, 1):
            stem = path.stem
            meta = next((catalog[u] for u in catalog if u.endswith(":" + stem)), None)
            urn = next((u for u in catalog if u.endswith(":" + stem)), f"urn:cts:{namespace}:{stem}")
            if meta is None:
                sibling = next((m for u, m in catalog.items() if u.split(":")[-1].startswith(stem.rsplit(".", 1)[0] + ".")), None)
                meta = {
                    "work_urn": urn.rsplit(".", 1)[0],
                    "author": sibling["author"] if sibling else path.parts[-3],
                    "title": sibling["title"] if sibling else path.parts[-2],
                    "kind": None, "lang": None, "label": "", "description": "",
                }
            suffix = re.search(r"-([a-z]{3})\d*$", stem)
            lang = meta["lang"] or (suffix.group(1) if suffix else "")
            kind = meta["kind"] or ("edition" if lang in ("grc", "lat") else "translation")
            try:
                passages, levels = extract(path)
            except Exception as exc:  # one malformed file must not sink the build
                print(f"  ! {path.name}: {exc}", file=sys.stderr)
                passages, levels = [], ""
            cur = db.execute(
                "INSERT INTO versions (urn, work_urn, author, title, lang, kind, label, description, path, levels, n_passages, search)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (urn, meta["work_urn"], meta["author"], meta["title"], lang, kind, meta["label"], meta["description"],
                 str(path.relative_to(DATA)), levels, len(passages), normalize(f"{meta['author']} {meta['title']} {meta['label']}")),
            )
            vid = cur.lastrowid
            rows, fts_rows = [], []
            for seq, (ref, text) in enumerate(passages):
                pid += 1
                rows.append((pid, vid, seq, ref, text))
                fts_rows.append((pid, normalize(text)))
            db.executemany("INSERT INTO passages VALUES (?,?,?,?,?)", rows)
            db.executemany("INSERT INTO fts (rowid, norm) VALUES (?,?)", fts_rows)
            if i % 200 == 0 or i == len(files):
                print(f"  {repo_name}: {i}/{len(files)} files", flush=True)
        db.commit()


# --- lexica ------------------------------------------------------------------


def tidy(text: str) -> str:
    text = squash(text)
    text = re.sub(r"\s+([,;:.)\]])", r"\1", text)
    return re.sub(r"([(\[])\s+", r"\1", text)


def entry_text(el) -> str:
    """Plain-text rendering of a dictionary entry, one line per numbered sense."""
    parts: list[str] = []

    def conv(text: str, greek: bool) -> str:
        return beta_to_unicode(text) if greek and looks_like_beta(text) else text

    def walk(node, greek: bool):
        if not isinstance(node.tag, str):
            return
        lang = node.get("lang")
        if lang:
            greek = lang == "greek"
        if node.tag == "sense":
            level = node.get("level") or "1"
            label = node.get("n") or ""
            parts.append(f"\x00{max(int(level), 1) if level.isdigit() else 1}\x01{'' if label == '0' else label}\x02")
        if node.text:
            parts.append(conv(node.text, greek))
        for child in node:
            walk(child, greek)
            if child.tail:
                parts.append(conv(child.tail, greek))

    walk(el, False)
    head, *senses = "".join(parts).split("\x00")
    lines = [tidy(head)]
    for sense in senses:
        level, rest = sense.split("\x01", 1)
        label, body = rest.split("\x02", 1)
        body = tidy(body)
        if body or label:
            lines.append("  " * (int(level) - 1) + (f"{label}. " if label else "") + body)
    return "\n".join(line for line in lines if line.strip())


def index_lexica(db: sqlite3.Connection) -> None:
    for lex, (title, pattern) in LEXICA.items():
        files = sorted((DATA / "lexica").glob(pattern), key=lambda p: [int(x) for x in re.findall(r"\d+", p.stem)[-1:]] or [0])
        count = 0
        for path in files:
            rows = []
            for _, el in etree.iterparse(io.BytesIO(load_xml(path)), events=("end",), tag=("entryFree", "entry"),
                                         recover=True, resolve_entities=False, load_dtd=False, no_network=True, huge_tree=True):
                key = el.get("key") or ""
                orth = next((o for o in el.iter("orth")), None)
                head = squash("".join(orth.itertext())) if orth is not None else key
                bare_key = re.sub(r"\d+$", "", key)
                if lex == "lsj":
                    head = beta_to_unicode(head) if looks_like_beta(head) else head
                    bare_key = beta_to_unicode(bare_key) if looks_like_beta(bare_key) else bare_key
                norm = re.sub(r"[^\w]", "", normalize(bare_key or head))
                rows.append((lex, key, head, norm, entry_text(el)))
                el.clear()
            db.executemany("INSERT INTO lexicon (lex, key, headword, norm, text) VALUES (?,?,?,?,?)", rows)
            count += len(rows)
        db.execute("INSERT INTO meta VALUES (?,?)", (f"lexicon:{lex}", title))
        db.commit()
        print(f"  {lex}: {count} entries", flush=True)


def main() -> None:
    missing = [r for r in TEXT_REPOS + ["lexica"] if not (DATA / r).is_dir()]
    if missing:
        sys.exit(f"Missing {missing} under {DATA}. Clone them from https://github.com/PerseusDL/ first (see README).")
    start = time.time()
    tmp = DB_PATH.with_suffix(".db.building")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
    print("Indexing texts...", flush=True)
    index_texts(db)
    print("Indexing lexica...", flush=True)
    index_lexica(db)
    db.executescript(INDEXES)
    db.execute("INSERT INTO meta VALUES ('built', ?)", (time.strftime("%Y-%m-%d"),))
    db.commit()
    db.close()
    tmp.replace(DB_PATH)
    print(f"Done in {time.time() - start:.0f}s -> {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
