# -*- coding: utf-8 -*-
"""Small pure helpers shared by call projection modules."""

from __future__ import annotations


def text_or_none(value: object) -> str | None:
    """Normalize an optional scalar to stripped text without inventing a value."""

    text = str(value or "").strip()
    return text or None


__all__ = ["text_or_none"]
