"""OpenCASCADE.gd autowrapper generator (clean rewrite).

Module-scoped pipeline: OCCT 8.0.1 headers -> libclang AST -> IR -> wrapper code.
Parsing is done with a correct clang resource dir so handle<T>/collection template
types resolve natively; no source-text mis-resolution heuristics are needed.
"""

__version__ = "0.1.0"
