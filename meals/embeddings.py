"""What text gets embedded for a catalog row (PRD §7.2, §7.6).

Pure string building, no I/O and no Django import beyond none at all - the
same posture `nutrition`/`engine` take - because both sides of a similarity
comparison have to agree on it. The backfill command embeds the *catalog*
side through these functions; Chunk 9b's request path embeds the *query*
side and must build its text the same way, or every comparison is between
two differently-shaped strings and the similarity floor means nothing.
"""
from __future__ import annotations

from typing import Optional, Sequence


def food_embedding_text(name: str, brand: Optional[str] = None) -> str:
    """A `Food` row as the phrase a user would plausibly type for it.

    Brand goes first when present, because that is the order people say it
    ("Amul butter", not "butter Amul") and only Open Food Facts rows have one
    - generic USDA/INDB/curated rows embed their name alone, which is what a
    plain "hummus" should be matching against anyway (`_SOURCE_PRIORITY`
    already encodes that preference on the lexical side).

    Catalog names lead with the food and qualify afterwards ("Milk, whole,
    3.25% milkfat"). The commas are left in: they carry real structure that
    the embedding model reads, and stripping them would merge the head with
    its qualifiers into one run-on phrase.
    """
    cleaned_name = " ".join(name.split())
    cleaned_brand = " ".join(brand.split()) if brand else ""
    if cleaned_brand:
        return "{} {}".format(cleaned_brand, cleaned_name)
    return cleaned_name


def composite_embedding_text(name: str, aliases: Sequence[str] = ()) -> str:
    """A `CompositeFood` as its name plus every curated alias.

    Aliases are included deliberately: §7.6 has a curator add real shorthand
    ("biryani" for "Chicken Biryani") rather than letting the system guess
    from fragments, so they are the highest-signal text there is for what a
    user actually types. Duplicates and case variants are collapsed, and the
    name always leads.
    """
    cleaned_name = " ".join(name.split())
    seen = {cleaned_name.lower()}
    parts = [cleaned_name]
    for alias in aliases:
        cleaned = " ".join(alias.split())
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            parts.append(cleaned)
    return ", ".join(parts)
