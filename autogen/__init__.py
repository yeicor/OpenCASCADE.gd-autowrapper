"""OpenCASCADE.gd autowrapper generator (clean rewrite).

Module-scoped pipeline: OCCT 8.0.1 headers -> libclang AST -> IR -> wrapper code.
Parsing is done with a correct clang resource dir so handle<T>/collection template
types resolve natively; no source-text mis-resolution heuristics are needed.
"""

import os

__version__ = "0.1.0"

# libclang discovery order:
#   1. $LIBCLANG_LIBRARY_FILE (explicit override, e.g. an LLVM install path)
#   2. a system libclang found by the OS loader (clang.cindex default), so the
#      pipeline uses the same libclang + -resource-dir pair as the system clang
#   3. the libclang bundled by `pip install libclang` (clang/native/) as a
#      fallback for runners with no system libclang.
# The system copy is preferred over the (often older) pip-bundled one: template
# parsing needs the resource dir supplied by the matching system clang, and
# newer bindings refuse to run against an outdated bundled libclang anyway.
try:
    import clang
    import clang.cindex as _cindex
except ImportError:
    pass
else:
    _override = os.environ.get("LIBCLANG_LIBRARY_FILE")
    if _override:
        _cindex.Config.set_library_file(_override)
    else:
        import ctypes

        def _system_libclang() -> bool:
            for _lib in ("libclang.so", "libclang.dylib", "libclang.dll"):
                try:
                    ctypes.cdll.LoadLibrary(_lib)
                except OSError:
                    continue
                return True
            return False

        if not _system_libclang():
            _native = os.path.join(os.path.dirname(clang.__file__), "native")
            for _lib in ("libclang.so", "libclang.dylib", "libclang.dll"):
                _path = os.path.join(_native, _lib)
                if os.path.exists(_path):
                    _cindex.Config.set_library_file(_path)
                    break
