# OpenCASCADE.gd-autowrapper

Generates the godot-cpp wrapper for OpenCASCADE (OCCT) used by
[OpenCASCADE.gd](https://github.com/yeicor/OpenCASCADE.gd).

## Pipeline

Module-scoped, clean libclang AST extraction.  OCCT headers are parsed with the
correct clang resource dir so `occ::handle<T>` / `opencascade::handle<T>` and
collection templates resolve natively — no source-text mis-resolution
heuristics.

```
autogen/
  cli.py         CLI entry point
  compile_db.py  compile_commands.json args + clang resource-dir (parse fix)
  occt.py        OCCT install discovery, module registry, include closure
  parser.py      seeded libclang TU parsing
  types.py       Type-API-only type mapper (handles, templates, enums)
  extract.py     class/enum/typedef/method/field extraction
  scanner.py     module-scoped parallel scan -> IR JSON
  model.py       IR dataclasses shared by scanner and codegen
```

## Usage

```sh
python3 -m autogen scan --module Standard
```

Requires `../.build-autowrapper/compile_commands.json` (or pass `--compile-db`)
and a vcpkg OCCT install at `../vcpkg/installed/x64-linux`.
