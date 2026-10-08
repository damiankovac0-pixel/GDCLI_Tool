"""Search the bundled GMDKit Geometry Dash object catalog.

The unmodified source CSV is distributed under the MIT license in
``data/GMDKIT_LICENSE``.  It is pinned to GMDKit commit
``1f5315cbc49efff5d05b6d2e17380ad88f95b241``:
https://github.com/UHDanke/gmdkit/blob/1f5315cbc49efff5d05b6d2e17380ad88f95b241/data/csv/object_table.csv
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from importlib import resources
from typing import TypedDict


CATALOG_SOURCE_REVISION = "1f5315cbc49efff5d05b6d2e17380ad88f95b241"
CATALOG_SOURCE_URL = (
    "https://github.com/UHDanke/gmdkit/blob/"
    f"{CATALOG_SOURCE_REVISION}/data/csv/object_table.csv"
)


CatalogObject = TypedDict(
    "CatalogObject",
    {"id": int, "name": str, "alias": str, "class": str},
)


def _catalog_file():
    return resources.files(__package__).joinpath("data", "object_catalog.csv")


@lru_cache(maxsize=1)
def _catalog() -> tuple[CatalogObject, ...]:
    rows: list[CatalogObject] = []
    seen: set[int] = set()
    with _catalog_file().open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        expected = [
            "object id",
            "gd class",
            "community alias",
            "gmdkit alias",
            "editor tab",
            "tab index",
        ]
        if reader.fieldnames != expected:
            raise RuntimeError("bundled object catalog has an unexpected schema")
        for line_number, source in enumerate(reader, start=2):
            try:
                object_id = int(source["object id"])
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    f"bundled object catalog has an invalid ID on line {line_number}"
                ) from error
            if object_id in seen:
                raise RuntimeError(f"bundled object catalog repeats object ID {object_id}")
            seen.add(object_id)
            rows.append(
                CatalogObject(
                    id=object_id,
                    name=(source["community alias"] or "").strip(),
                    alias=(source["gmdkit alias"] or "").strip(),
                    **{"class": (source["gd class"] or "").strip()},
                )
            )
    return tuple(rows)


def _copy(row: CatalogObject) -> CatalogObject:
    return CatalogObject(**row)


def get_object(object_id: int) -> CatalogObject | None:
    """Return one catalog entry by numeric object ID, or ``None``."""

    if isinstance(object_id, bool) or not isinstance(object_id, int):
        raise TypeError("object_id must be an integer")
    for row in _catalog():
        if row["id"] == object_id:
            return _copy(row)
    return None


def _normalize(value: str) -> str:
    return " ".join(re.sub(r"[._-]+", " ", value.casefold()).split())


def _match_score(row: CatalogObject, query: str) -> int | None:
    if query.isdecimal() and row["id"] == int(query):
        return -1

    fields = tuple(
        _normalize(str(row[key])) for key in ("name", "alias", "class") if row[key]
    )
    if not fields:
        return None
    if query in fields:
        return 0
    if any(field.startswith(query) for field in fields):
        return 1
    if any(any(word.startswith(query) for word in field.split()) for field in fields):
        return 2
    if any(query in field for field in fields):
        return 3

    query_words = query.split()
    if len(query_words) > 1 and any(
        all(word in field for word in query_words) for field in fields
    ):
        return 4
    return None


def find_objects(query: str, limit: int = 20) -> list[CatalogObject]:
    """Find catalog objects by community name, GMDKit alias, or GD class.

    Matching is case-insensitive and treats dots, underscores, and hyphens as
    word separators, so searches such as ``"yellow orb"`` and ``"orb.yellow"``
    both locate object 36.  Results are ranked by match quality then object ID.
    """

    if not isinstance(query, str):
        raise TypeError("query must be a string")
    normalized_query = _normalize(query)
    if not normalized_query:
        raise ValueError("query must not be empty")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if limit < 0:
        raise ValueError("limit must not be negative")
    if limit == 0:
        return []

    matches: list[tuple[int, int, CatalogObject]] = []
    for row in _catalog():
        score = _match_score(row, normalized_query)
        if score is not None:
            matches.append((score, int(row["id"]), row))
    matches.sort(key=lambda match: (match[0], match[1]))
    return [_copy(row) for _, _, row in matches[:limit]]
