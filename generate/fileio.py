"""Idempotent file writing helpers for the autowrapper generator.

Writing only when content changes keeps file mtimes stable, so downstream
build systems (cmake/ninja via vcpkg --editable) can skip recompiling
unchanged generated sources.
"""

from __future__ import annotations

from pathlib import Path


def write_if_changed(path: Path, content: str) -> bool:
    """Write content to path only if it differs from what is already there.

    Returns True if the file was written, False if it was left untouched
    (content identical, mtime preserved).
    """
    path = Path(path)
    if path.exists():
        try:
            old = path.read_text()
        except OSError:
            old = None
        if old == content:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return True
