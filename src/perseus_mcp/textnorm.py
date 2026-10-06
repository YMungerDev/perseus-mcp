"""Text normalisation and Beta Code conversion shared by the indexer and the server."""

import re
import unicodedata

_FOLD = str.maketrans({"ς": "σ", "ϲ": "σ", "j": "i", "v": "u"})


def normalize(text: str) -> str:
    """Fold case, diacritics, final sigma and Latin u/v, i/j so forms match however they are typed."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return stripped.lower().translate(_FOLD)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_BETA_LETTERS = dict(zip("abgdezhqiklmncoprstufxyw", "αβγδεζηθικλμνξοπρστυφχψω"))
_BETA_MARKS = {
    ")": "̓",
    "(": "̔",
    "/": "́",
    "\\": "̀",
    "=": "͂",
    "+": "̈",
    "|": "ͅ",
    "_": "̄",
    "^": "̆",
}
_GREEK_RE = re.compile(r"[Ͱ-Ͽἀ-῿]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")


def looks_like_beta(text: str) -> bool:
    return bool(_ASCII_LETTER_RE.search(text)) and not _GREEK_RE.search(text)


def beta_to_unicode(text: str) -> str:
    """Convert Perseus Beta Code (mh=nis, *)axilleu/s) to polytonic Unicode."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        upper = False
        marks = ""
        if c == "*":
            upper = True
            i += 1
            while i < n and text[i] in _BETA_MARKS:
                marks += _BETA_MARKS[text[i]]
                i += 1
            if i >= n:
                break
            c = text[i]
        low = c.lower()
        if low in _BETA_LETTERS:
            i += 1
            if low == "s":
                if i < n and text[i] in "123":
                    letter = {"1": "σ", "2": "ς", "3": "ϲ"}[text[i]]
                    i += 1
                else:
                    j = i
                    while j < n and text[j] in _BETA_MARKS:
                        j += 1
                    at_end = j >= n or not (text[j].lower() in _BETA_LETTERS or text[j] in "*-")
                    letter = "ς" if at_end and not upper else "σ"
            else:
                letter = _BETA_LETTERS[low]
            while i < n and text[i] in _BETA_MARKS:
                marks += _BETA_MARKS[text[i]]
                i += 1
            out.append((letter.upper() if upper else letter) + marks)
        elif c == "'":
            out.append("’")
            i += 1
        elif c == ":":
            out.append("·")
            i += 1
        else:
            out.append(c)
            i += 1
    return unicodedata.normalize("NFC", "".join(out))
