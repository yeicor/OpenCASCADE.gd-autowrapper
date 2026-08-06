"""libclang translation-unit parsing for OCCT headers.

Each header is parsed through a temporary .cpp wrapper written next to the
header so relative includes resolve, with optional pre-includes (the header's
include closure) to make headers that are not self-contained parse cleanly.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from clang.cindex import Diagnostic, Index, TranslationUnit


class ParseError(RuntimeError):
    def __init__(self, message: str, loc_file: str | None = None):
        super().__init__(message)
        self.loc_file = loc_file


def parse_header(header_path: Path, args: list[str],
                 pre_headers: list[str] | None = None,
                 ) -> TranslationUnit:
    """Parse an OCCT header, returning the translation unit.

    Raises ParseError when the parse produced hard errors (degraded parse)."""
    header_path = Path(header_path)
    header_dir = str(header_path.parent)
    header_name = header_path.name
    pre_lines = "".join(f"#include <{h}>\n" for h in (pre_headers or []))
    index = Index.create()

    with tempfile.NamedTemporaryFile(suffix=".cpp", mode="w", delete=False,
                                     dir=header_dir, prefix="_aw_parse_") as f:
        f.write(pre_lines + f'#include "{header_name}"\n')
        tmp_path = f.name
    try:
        tu = index.parse(tmp_path, args=args + ["-x", "c++"])
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    hard = [d for d in tu.diagnostics if d.severity >= Diagnostic.Error]
    if hard:
        loc = hard[0].location
        raise ParseError(
            f"{header_name}: {len(hard)} parse errors; first: {hard[0].spelling}",
            loc_file=loc.file.name if loc.file else None)
    return tu
