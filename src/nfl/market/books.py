"""Frozen Phase 4 book groups (configs/phase4_books.yaml, Amendment A1)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

BOOKS_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "phase4_books.yaml"
NY_ACTIONABLE = "ny_actionable"
REFERENCE_SHARP = "reference_sharp"
OTHER_OFFSHORE = "other_offshore"
UNCLASSIFIED = "unclassified"
SCOPES = ("ny_actionable", "all_observed")   # line-shopping / CLV scopes; reference reported separately


@lru_cache(maxsize=1)
def load_books(path: str | None = None) -> dict:
    cfg = yaml.safe_load(Path(path or BOOKS_CONFIG).read_text())
    groups = cfg["groups"]
    seen: dict[str, str] = {}
    for g, books in groups.items():
        for b in books:
            if b in seen:
                raise ValueError(f"book {b} is in two groups ({seen[b]}, {g})")
            seen[b] = g
    if len(cfg["request_bookmakers"]) > 10:
        raise ValueError("more than 10 request books would cost more than 1 credit per pull")
    return cfg


def request_bookmakers() -> list[str]:
    return list(load_books()["request_bookmakers"])


def group_of(book: str) -> str:
    for g, books in load_books()["groups"].items():
        if book in books:
            return g
    return UNCLASSIFIED


def in_scope(book: str, scope: str) -> bool:
    if scope == "all_observed":
        return True
    if scope == NY_ACTIONABLE:
        return group_of(book) == NY_ACTIONABLE
    if scope == REFERENCE_SHARP:
        return group_of(book) == REFERENCE_SHARP
    raise ValueError(scope)


def reference_books() -> list[str]:
    return list(load_books()["groups"][REFERENCE_SHARP])
