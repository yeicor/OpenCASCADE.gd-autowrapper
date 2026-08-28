#!/usr/bin/env python3
"""Print one-per-line libclang parse args for the current target triplet.

Used by the local act verification workflow so shell steps can rebuild the
exact argument set the generator scans with.  Honors VCPKG_DEFAULT_TRIPLET
(target selection/data model) and OCCT_INCLUDE_DIR (header source).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autogen import compile_db as cdb  # noqa: E402
from autogen.occt import find_occt_install  # noqa: E402
from autogen.cli import PROJECT_ROOT  # noqa: E402

install = find_occt_install(PROJECT_ROOT)
args = cdb.ensure_occt_args(cdb.CompileArgs._fallback_args(), install.include_dir)
for a in args:
    print(a)
