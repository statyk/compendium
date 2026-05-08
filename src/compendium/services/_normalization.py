"""String normalization helpers for catalog data.

These encode v1 policy choices (English-only articles, conservative
Last/First detection) rather than domain invariants, so they live in
services rather than domain.
"""

_LEADING_ARTICLES = ("the ", "an ", "a ")

_NAME_SUFFIXES = {
    "jr", "jr.", "sr", "sr.", "ii", "iii", "iv",
    "phd", "ph.d.", "md", "m.d.", "esq", "esq.",
}


def compute_sort_title(title: str) -> str:
    """Return a sort key for *title* that ignores a leading English article.

    "The Great Gatsby" → "Great Gatsby, The"... no — just strips: "Great Gatsby".
    Case-insensitive match; returned value preserves original casing of the rest.
    """
    if not title:
        return title
    lower = title.lower()
    for article in _LEADING_ARTICLES:
        if lower.startswith(article):
            return title[len(article):]
    return title


def normalize_title(title: str) -> str:
    """Convert a trailing-article title form to a leading-article form.

    "Information, The" → "The Information"
    "Tale, A" → "A Tale"
    "Smith, John" → "Smith, John"  (unchanged — not an article)
    Idempotent: "The Information" → "The Information".
    """
    if not title:
        return title
    comma_pos = title.rfind(", ")
    if comma_pos == -1:
        return title
    suffix = title[comma_pos + 2:].strip()
    if suffix.lower() in {"the", "an", "a"}:
        return f"{suffix} {title[:comma_pos]}"
    return title


def normalize_creator_name(name: str) -> str:
    """Convert "Last, First" to "First Last" using a conservative heuristic.

    Rules:
    - Exactly one comma required.
    - Both sides of the comma must be non-empty after stripping.
    - The part after the comma must not be a recognized name suffix
      (Jr., Sr., II, III, IV, PhD, MD, Esq.).
    - Returns *name* unchanged if any rule fails.
    """
    if not name or "," not in name:
        return name
    parts = name.split(",")
    if len(parts) != 2:
        return name
    last = parts[0].strip()
    first = parts[1].strip()
    if not last or not first:
        return name
    if first.lower() in _NAME_SUFFIXES:
        return name
    return f"{first} {last}"
