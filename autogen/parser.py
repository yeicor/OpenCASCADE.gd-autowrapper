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
    pass


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

    if header_dir.startswith("/usr/"):
        tmp_dir = tempfile.mkdtemp(prefix="_aw_parse_")
        try:
            tmp_path = os.path.join(tmp_dir, "_aw_parse.cpp")
            with open(tmp_path, "w") as f:
                f.write(pre_lines + f"#include <{header_name}>\n")
            tu = index.parse(tmp_path, args=args + ["-x", "c++", "-I", header_dir])
        finally:
            try:
                os.unlink(tmp_path)
                os.rmdir(tmp_dir)
            except OSError:
                pass
    else:
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
        raise ParseError(
            f"{header_name}: {len(hard)} parse errors; first: {hard[0].spelling}")
    return tu
