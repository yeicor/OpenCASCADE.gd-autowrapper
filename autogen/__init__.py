"""OpenCASCADE.gd autowrapper generator (clean rewrite).

Module-scoped pipeline: OCCT 8.0.1 headers -> libclang AST -> IR -> wrapper code.
Parsing is done with a correct clang resource dir so handle<T>/collection template
types resolve natively; no source-text mis-resolution heuristics are needed.
"""

import os

__version__ = "0.1.0"

# Prefer the libclang shipped by `pip install clang libclang` (found under
# site-packages/clang/native/): bare CI runners have no system libclang for the
# loader to find.  Falls back to the OS search when the bundled one is absent.
try:
    import clang
    import clang.cindex as _cindex
except ImportError:
    pass
else:
    _native = os.path.join(os.path.dirname(clang.__file__), "native")
    for _lib in ("libclang.so", "libclang.dylib", "libclang.dll"):
        _path = os.path.join(_native, _lib)
        if os.path.exists(_path):
            _cindex.Config.set_library_file(_path)
            break
