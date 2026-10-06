"""MCP server exposing Perseus Digital Library texts and lexica from the local index."""

import re
import sqlite3
from functools import lru_cache

from mcp.server.mcpserver import MCPServer

from .build_index import DB_PATH
from .textnorm import beta_to_unicode, looks_like_beta, normalize

mcp = MCPServer(
    "perseus",
    instructions=(
        "Direct access to the Perseus Digital Library's Greek and Latin texts, English translations, and the LSJ and "
        "Lewis & Short lexica, served from a local copy of the PerseusDL open data. Quote primary texts from "
        "get_passage rather than from memory, and cite the CTS URN it returns. Use find_work to discover works and "
        "their citation schemes, list_refs to see how a work is divided, search_corpus to find where a word form or "
        "phrase occurs, and lookup_lexicon for dictionary entries."
    ),
)

ORIGINAL_LANGS = ("grc", "lat")
ALIASES = {"virgil": "vergil", "vergilius": "vergil", "horatius": "horace", "ovidius": "ovid", "livius": "livy"}
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


class LookupFailed(Exception):
    pass


@lru_cache(maxsize=1)
def db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise LookupFailed(f"No index at {DB_PATH}. Build it with: uv run --directory {DB_PATH.parents[1]} perseus-mcp-index")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# --- works and versions ------------------------------------------------------


def split_urn(urn: str) -> tuple[str, str | None, str | None]:
    """urn:cts:ns:group.work[.version][:ref] -> (work urn, version urn or None, ref or None)."""
    parts = urn.strip().split(":")
    if len(parts) < 4:
        raise LookupFailed(f"Not a CTS URN: {urn}")
    ids = parts[3].split(".")
    base = ":".join(parts[:3])
    if len(ids) < 2:
        raise LookupFailed(f"URN names a text group, not a work: {urn}. Use find_work to list its works.")
    work = f"{base}:{'.'.join(ids[:2])}"
    version = f"{base}:{'.'.join(ids[:3])}" if len(ids) >= 3 else None
    ref = ":".join(parts[4:]) or None
    return work, version, ref


def score_works(query: str) -> list[tuple[float, bool, sqlite3.Row]]:
    """Works whose author/title words each query word prefixes: (score, a word hit the title, row), best first."""
    tokens = [ALIASES.get(t, t) for t in WORD_RE.findall(normalize(query))]
    if not tokens:
        return []
    scored = []
    rows = db().execute("SELECT work_urn, author, title, group_concat(search, ' ') AS search FROM versions GROUP BY work_urn").fetchall()
    for row in rows:
        title_words = set(WORD_RE.findall(normalize(row["title"])))
        words = set(WORD_RE.findall(row["search"])) | set(WORD_RE.findall(row["work_urn"].split(":")[-1]))
        score = 0.0
        for token in tokens:
            best = max((len(token) / len(w) for w in words if w.startswith(token)), default=0)
            if not best:
                break
            score += best
        else:
            in_title = any(w.startswith(t) for t in tokens for w in title_words)
            scored.append((round(score, 6), in_title, row))
    scored.sort(key=lambda t: (-t[0], len(t[2]["title"]), t[2]["author"], t[2]["title"]))
    return scored


def match_works(query: str, limit: int = 25) -> list[sqlite3.Row]:
    return [row for _, _, row in score_works(query)[:limit]]


def resolve_work(work: str) -> tuple[str, str | None, str | None]:
    if work.strip().lower().startswith("urn:cts:"):
        return split_urn(work)
    scored = score_works(work)
    if not scored:
        raise LookupFailed(f'No work matches "{work}". Try find_work with the author or title alone.')
    # Accept the top match only when it is the sole match or it names a title and outscores the rest.
    if len(scored) > 1 and not (scored[0][1] and scored[0][0] > scored[1][0]):
        listing = "\n".join(f"  {row['author']}, {row['title']}  ({row['work_urn']})" for _, _, row in scored[:12])
        more = f"\n  ... and {len(scored) - 12} more (use find_work)" if len(scored) > 12 else ""
        raise LookupFailed(f'"{work}" matches {len(scored)} works; pass one of these URNs as `work`:\n{listing}{more}')
    return scored[0][2]["work_urn"], None, None


def versions_of(work_urn: str) -> list[sqlite3.Row]:
    return db().execute("SELECT * FROM versions WHERE work_urn = ? ORDER BY urn", (work_urn,)).fetchall()


def version_rank(row: sqlite3.Row) -> tuple:
    number = re.search(r"(\d+)$", row["urn"])
    return (row["n_passages"] > 0, "perseus-" in row["urn"], int(number.group(1)) if number else 0)


def pick_version(work_urn: str, lang: str | None = None, version_urn: str | None = None) -> sqlite3.Row:
    versions = versions_of(work_urn)
    if not versions:
        raise LookupFailed(f"No texts indexed for {work_urn}. Use find_work to see what is available.")
    if version_urn:
        row = next((v for v in versions if v["urn"] == version_urn), None)
        if row is None:
            # Hopper and older citations use superseded version ids (perseus-grc1); fall back by language.
            suffix = re.search(r"-([a-z]{3})\d*$", version_urn)
            lang = lang or (suffix.group(1) if suffix else None)
        else:
            return row
    if lang:
        pool = [v for v in versions if v["lang"] == lang]
        if not pool:
            have = ", ".join(sorted({v["lang"] for v in versions}))
            raise LookupFailed(f"{work_urn} has no '{lang}' version (available: {have}).")
    else:
        pool = [v for v in versions if v["lang"] in ORIGINAL_LANGS and v["kind"] == "edition"] or versions
    return max(pool, key=version_rank)


# --- reference resolution ----------------------------------------------------


@lru_cache(maxsize=64)
def refs_of(version_id: int) -> tuple[str, ...]:
    rows = db().execute("SELECT ref FROM passages WHERE version_id = ? ORDER BY seq", (version_id,))
    return tuple(r[0] for r in rows)


def locate(refs: tuple[str, ...], target: str, end: bool, strict: bool = False) -> tuple[int, bool] | None:
    """Index of the first (or last) passage covered by `target`, and whether it matched exactly."""
    target = target.strip().strip(".").lower()
    prefix = target + "."
    hits = [i for i, r in enumerate(refs) if r.lower() == target or r.lower().startswith(prefix)]
    if hits:
        return (hits[-1] if end else hits[0]), True
    if strict:
        return None
    # A bare lower-level reference ("514" for Republic 7.514) when it is unambiguous across the work.
    tail, inner = "." + target, "." + target + "."
    hits = [i for i, r in enumerate(refs) if r.lower().endswith(tail) or inner in r.lower()]
    if hits and len({refs[i].lower().split(tail)[0] for i in hits}) == 1:
        return (hits[-1] if end else hits[0]), True
    # Stephanus/Bekker letters ("514a") where the text is only divided by page.
    page = re.fullmatch(r"(.*\d)[a-z]", target)
    if page:
        found = locate(refs, page.group(1), end)
        return (found[0], False) if found else None
    parent, _, last = target.rpartition(".")
    if last.isdigit():
        # Versions divided more coarsely (a translation chunked by the line that opens each chunk,
        # or two verse lines printed as one): take the division that contains the reference.
        depth = target.count(".")
        siblings = []
        for i, r in enumerate(refs):
            comps = r.lower().split(".")
            if len(comps) > depth and ".".join(comps[:depth]) == parent and comps[depth].isdigit():
                siblings.append((int(comps[depth]), i))
        want = int(last)
        below = [(n, i) for n, i in siblings if n <= want]
        if below and want <= max(n for n, _ in siblings):
            floor = max(n for n, _ in below)
            hits = [i for n, i in below if n == floor]
            return (hits[-1] if end else hits[0]), False
    if parent:
        # The version stops at a higher level (a translation cited by chapter, asked for a section).
        found = locate(refs, parent, end, strict=True)
        while found is None and "." in parent:
            parent = parent.rpartition(".")[0]
            found = locate(refs, parent, end, strict=True)
        return (found[0], False) if found else None
    return None


def resolve_range(version: sqlite3.Row, ref: str) -> tuple[int, int, bool]:
    refs = refs_of(version["id"])
    if not refs:
        raise LookupFailed(f"{version['urn']} is catalogued but no citable text could be extracted from it.")
    ref = ref.strip().replace("–", "-")
    whole = locate(refs, ref, end=False) if "-" in ref else None
    if whole and whole[1]:
        start_ref = end_ref = ref
    else:
        start_ref, _, end_ref = ref.partition("-")
        end_ref = end_ref or start_ref
        missing = start_ref.count(".") - end_ref.count(".")
        if missing > 0:
            end_ref = ".".join(start_ref.split(".")[:missing] + [end_ref])
    start, end = locate(refs, start_ref, end=False), locate(refs, end_ref, end=True)
    if start is None or end is None:
        raise LookupFailed(
            f"Reference {ref} not found in {version['urn']} (cited by {version['levels'] or 'unknown scheme'}; "
            f"runs {refs[0]} to {refs[-1]}). Use list_refs to browse its divisions."
        )
    if end[0] < start[0]:
        raise LookupFailed(f"Range {ref} ends before it starts in {version['urn']}.")
    return start[0], end[0], start[1] and end[1]


def render_passage(version: sqlite3.Row, ref: str, max_chars: int) -> str:
    start, end, exact = resolve_range(version, ref)
    rows = db().execute(
        "SELECT ref, text FROM passages WHERE version_id = ? AND seq BETWEEN ? AND ? ORDER BY seq",
        (version["id"], start, end),
    ).fetchall()
    body, used, shown = [], 0, 0
    for row in rows:
        if body and used + len(row["text"]) > max_chars:
            break
        body.append(f"[{row['ref']}] {row['text']}")
        used += len(row["text"])
        shown += 1
    first, last = rows[0]["ref"], rows[shown - 1]["ref"]
    span = first if first == last else f"{first}-{last}"
    urn = f"{version['urn']}:{span}"
    head = [
        f"{version['author']}, {version['title']} {span}  ({version['lang']}, {version['kind']})",
        f"URN: {urn}",
    ]
    if version["description"]:
        head.append(f"Source: {version['description']}")
    head.append(f"Read online: https://scaife.perseus.org/reader/{urn}/")
    if not exact:
        head.append(f"Note: this version is not divided at exactly {ref}; showing the enclosing passage(s) {span}.")
    if shown < len(rows):
        head.append(
            f"Truncated at {max_chars} characters: {shown} of {len(rows)} passages shown. "
            f"Continue from {rows[shown]['ref']} (range ends at {rows[-1]['ref']})."
        )
    return "\n".join(head) + "\n\n" + "\n".join(body)


# --- tools -------------------------------------------------------------------


@mcp.tool()
def find_work(query: str, limit: int = 15) -> str:
    """Find works in the Perseus collection by author and/or title, e.g. "Homer Iliad", "Thuc.", "Cicero Catil".

    Words may be abbreviated to a prefix. Returns each work's CTS URN, its available editions and translations,
    and how each is cited (e.g. book.line), which is what get_passage needs.
    """
    try:
        works = match_works(query, limit=max(1, min(limit, 50)))
        if not works:
            return f'No work matches "{query}". Try the author alone, or an English or Latin form of the title.'
        out = []
        for work in works:
            out.append(f"{work['author']}, {work['title']}  ({work['work_urn']})")
            for v in versions_of(work["work_urn"]):
                scheme = f"cited by {v['levels']}, {v['n_passages']} passages" if v["n_passages"] else "no citable text extracted"
                out.append(f"  - {v['urn']}  [{v['lang']} {v['kind']}; {scheme}]")
                if v["description"]:
                    out.append(f"      {v['description']}")
        return "\n".join(out)
    except LookupFailed as exc:
        return f"Error: {exc}"


@mcp.tool()
def get_passage(
    work: str,
    ref: str = "",
    translation: bool = False,
    lang: str = "",
    max_chars: int = 8000,
) -> str:
    """Fetch the exact text of a passage, with its CTS URN and source edition.

    work: a CTS URN (work or version level, optionally ending in :ref) or an author/title query such as
        "Homer Iliad" or "Verg. Aen.". Ambiguous queries return the candidate URNs.
    ref: canonical reference in the work's own scheme: "1.1-1.10" (or "1.1-10"), "2.43", "17a-17c", or a whole
        division such as "1". Check find_work or list_refs if unsure of the scheme.
    translation: also return the English translation of the same passage when Perseus has one.
    lang: return a specific language instead of the original ("grc", "lat", "eng", ...).
    max_chars: cap on the text returned per version; the reply says where to continue when truncated.
    """
    try:
        work_urn, version_urn, urn_ref = resolve_work(work)
        ref = ref.strip() or (urn_ref or "")
        if not ref:
            return "Error: give a reference (e.g. ref=\"1.1-1.10\"). Use list_refs to see how the work is divided."
        max_chars = max(500, min(max_chars, 40000))
        primary = pick_version(work_urn, lang or None, version_urn)
        blocks = [render_passage(primary, ref, max_chars)]
        if translation and primary["lang"] != "eng":
            try:
                blocks.append(render_passage(pick_version(work_urn, "eng"), ref, max_chars))
            except LookupFailed as exc:
                blocks.append(f"(No English translation returned: {exc})")
        return "\n\n---\n\n".join(blocks)
    except LookupFailed as exc:
        return f"Error: {exc}"


@mcp.tool()
def list_refs(work: str, under: str = "", lang: str = "", limit: int = 200) -> str:
    """Show how a work is divided: the references one level below `under` (top level when empty).

    Example: list_refs("Homer Iliad") lists the 24 books with their line counts; list_refs("Thucydides", "2")
    lists the chapters of book 2.
    """
    try:
        work_urn, version_urn, _ = resolve_work(work)
        version = pick_version(work_urn, lang or None, version_urn)
        refs = refs_of(version["id"])
        under = under.strip().strip(".")
        depth = under.count(".") + 1 if under else 0
        groups: dict[str, int] = {}
        for r in refs:
            if under and not (r == under or r.startswith(under + ".")):
                continue
            comps = r.split(".")
            key = ".".join(comps[: depth + 1])
            groups[key] = groups.get(key, 0) + 1
        if not groups:
            return f"Error: nothing under {under!r} in {version['urn']} (cited by {version['levels']}; runs {refs[0]} to {refs[-1]})."
        limit = max(1, min(limit, 2000))
        lines = [
            f"{version['author']}, {version['title']}  ({version['urn']})",
            f"Cited by: {version['levels']}; {len(refs)} passages in all.",
            f"{len(groups)} divisions" + (f" under {under}" if under else "") + (f" (first {limit} shown)" if len(groups) > limit else "") + ":",
        ]
        for key, count in list(groups.items())[:limit]:
            lines.append(f"  {key}" + (f"  ({count} passages)" if count > 1 else ""))
        return "\n".join(lines)
    except LookupFailed as exc:
        return f"Error: {exc}"


def snippet(text: str, tokens: list[str], width: int = 16) -> str:
    words = text.split()
    if len(words) <= width * 2:
        return text
    plain = [t.rstrip("*") for t in tokens]
    hit = next(
        (i for i, w in enumerate(words) if any(n.startswith(p) if t.endswith("*") else n == p
                                               for n in WORD_RE.findall(normalize(w)) for p, t in zip(plain, tokens))),
        0,
    )
    lo, hi = max(0, hit - width), min(len(words), hit + width)
    return ("… " if lo else "") + " ".join(words[lo:hi]) + (" …" if hi < len(words) else "")


@mcp.tool()
def search_corpus(query: str, lang: str = "", work: str = "", phrase: bool = False, limit: int = 20, offset: int = 0) -> str:
    """Search the texts for word forms, ignoring accents, breathings, case and u/v, i/j.

    query: one or more words; all must occur in the same passage (line or section). End a word with * for a
        prefix match ("μηνι*", "amic*"). This matches inflected forms as written, not lemmata, so search a stem
        with * to catch a paradigm. Type Greek in Unicode; accents are optional.
    lang: restrict to "grc", "lat" or "eng". work: restrict to one work (URN or author/title query).
    phrase: require the words to be adjacent and in order.
    Results come in corpus order with reference, URN and context; use offset to page.
    """
    try:
        raw = re.findall(r"[^\W_]+\*?", normalize(query), re.UNICODE)
        if not raw:
            return "Error: empty query."
        terms = [f'"{t.rstrip("*")}"' + ("*" if t.endswith("*") else "") for t in raw]
        match = '"' + " ".join(t.rstrip("*") for t in raw) + '"' if phrase else " AND ".join(terms)
        where, params = ["fts MATCH ?"], [match]
        if lang:
            where.append("v.lang = ?")
            params.append(lang)
        if work:
            work_urn, version_urn, _ = resolve_work(work)
            where.append("v.urn = ?" if version_urn else "v.work_urn = ?")
            params.append(version_urn or work_urn)
        sql = f"FROM fts JOIN passages p ON p.id = fts.rowid JOIN versions v ON v.id = p.version_id WHERE {' AND '.join(where)}"
        total = db().execute(f"SELECT count(*) {sql}", params).fetchone()[0]
        if not total:
            return f"No passages match {query!r}" + (f" in {lang}" if lang else "") + ". Try a stem with * or a different form."
        limit = max(1, min(limit, 100))
        rows = db().execute(
            f"SELECT v.author, v.title, v.urn, v.lang, p.ref, p.text {sql} ORDER BY v.urn, p.seq LIMIT ? OFFSET ?",
            params + [limit, max(0, offset)],
        ).fetchall()
        out = [f"{total} matching passages; showing {offset + 1}-{offset + len(rows)}."]
        for r in rows:
            out.append(f"\n{r['author']}, {r['title']} {r['ref']}  ({r['urn']}:{r['ref']})")
            out.append(f"  {snippet(r['text'], raw)}")
        return "\n".join(out)
    except LookupFailed as exc:
        return f"Error: {exc}"
    except sqlite3.OperationalError as exc:
        return f"Error: could not run that search ({exc})."


LEXICON_ALIASES = {"lsj": "lsj", "ls": "ls", "lewis-short": "ls", "lewis & short": "ls", "lewis": "lewis", "elementary": "lewis"}


@mcp.tool()
def lookup_lexicon(word: str, lexicon: str = "auto", max_chars: int = 6000) -> str:
    """Look up a dictionary headword (the lemma, not an inflected form).

    lexicon: "lsj" (Liddell-Scott-Jones, Greek), "ls" (Lewis & Short, Latin), "lewis" (Lewis' Elementary Latin
    Dictionary, shorter entries), or "auto" to choose by script. Accents and vowel quantities are ignored when
    matching; Greek may be given in Unicode or Beta Code with lexicon="lsj". If there is no exact headword,
    the nearest headwords by prefix are listed.
    """
    try:
        is_greek = bool(re.search(r"[Ͱ-Ͽἀ-῿]", word))
        lex = LEXICON_ALIASES.get(lexicon.strip().lower()) if lexicon.strip().lower() != "auto" else ("lsj" if is_greek else "ls")
        if lex is None:
            return 'Error: lexicon must be "auto", "lsj", "ls" or "lewis".'
        if lex == "lsj" and not is_greek and looks_like_beta(word):
            word = beta_to_unicode(word)
        norm = re.sub(r"[^\w]", "", normalize(word))
        if not norm:
            return "Error: empty headword."
        title = db().execute("SELECT value FROM meta WHERE key = ?", (f"lexicon:{lex}",)).fetchone()[0]
        rows = db().execute("SELECT key, headword, text FROM lexicon WHERE lex = ? AND norm = ? ORDER BY id", (lex, norm)).fetchall()
        if not rows:
            near = db().execute(
                "SELECT DISTINCT headword FROM lexicon WHERE lex = ? AND norm >= ? AND norm < ? ORDER BY norm LIMIT 30",
                (lex, norm, norm + "￿"),
            ).fetchall()
            hint = "Headwords beginning with it: " + ", ".join(r[0] for r in near) if near else "No headwords begin with it either; check that this is the dictionary form."
            return f'No entry "{word}" in {title}. {hint}'
        max_chars = max(500, min(max_chars, 60000))
        budget = max(500, max_chars // len(rows))
        out = [f"{title}: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'} for {word}"]
        for r in rows:
            text = r["text"]
            if len(text) > budget:
                text = text[:budget].rsplit(" ", 1)[0] + f" … [entry truncated: {len(r['text'])} characters in full; raise max_chars to read on]"
            out.append(f"\n## {r['headword']}  (key {r['key']})\n{text}")
        return "\n".join(out)
    except LookupFailed as exc:
        return f"Error: {exc}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
