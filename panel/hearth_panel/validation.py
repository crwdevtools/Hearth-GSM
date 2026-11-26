"""Small input checks shared by the routers."""

from __future__ import annotations


def clean_name(v: str | None) -> str | None:
    """A name, trimmed; one made only of spaces is as good as none."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        raise ValueError("Give it a name.")
    return v
