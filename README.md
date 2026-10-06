# perseus-mcp

An MCP server that gives Claude direct, citable access to the [Perseus Digital Library](https://www.perseus.tufts.edu/hopper/):
Greek and Latin texts, their English translations, and the LSJ and Lewis & Short lexica.

It does not scrape the Hopper website. Perseus publishes the same texts as open TEI XML on GitHub
([PerseusDL](https://github.com/PerseusDL)); this server indexes a local clone into SQLite, so lookups are
instant, work offline, and return the exact text with its CTS URN.

## Tools

| Tool | What it does |
|---|---|
| `find_work` | Find a work by author/title (prefixes allowed: "Hom. Il."); lists editions, translations and citation scheme |
| `get_passage` | Exact text for a reference ("1.1-1.10", "2.43", "514a"), optionally with the English translation |
| `list_refs` | How a work is divided (books, chapters, ...) |
| `search_corpus` | Word-form and phrase search, insensitive to accents, case, u/v and i/j; `*` for prefixes |
| `lookup_lexicon` | Headword lookup in LSJ, Lewis & Short, or Lewis' Elementary Latin Dictionary |

## Setup

```sh
cd data
git clone --depth 1 https://github.com/PerseusDL/canonical-greekLit.git
git clone --depth 1 https://github.com/PerseusDL/canonical-latinLit.git
git clone --depth 1 https://github.com/PerseusDL/lexica.git
cd ..
uv run perseus-mcp-index          # about 5 minutes; writes data/perseus.db
claude mcp add --scope user perseus -- uv run --quiet --directory "$PWD" perseus-mcp
```

To pick up corrections from Perseus later: `git -C data/<repo> pull` in each repo, then re-run `uv run perseus-mcp-index`.

## Limits

- Search matches inflected forms as written; there is no lemmatiser or morphological parser (the Hopper's
  Morpheus service is not part of the open data).
- References follow each edition's own scheme, which is sometimes coarser than the scholarly convention
  (Plato is divided by Stephanus page, not page and letter). `get_passage` returns the enclosing passage and says so.
- Version identifiers are the current GitHub ones (`perseus-grc2`), which can differ from older Hopper URNs
  (`perseus-grc1`); old identifiers are mapped to the current version of the same language.

## Licence of the data

Texts and lexica are provided by the Perseus Digital Library under CC BY-SA 4.0, with funding from the
National Endowment for the Humanities. Credit Perseus when reusing them.
