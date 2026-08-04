"""Reconstruct the dataclass IR from scan JSON (out/ir/*.json)."""

from __future__ import annotations

import json
from pathlib import Path

from . import model as M


def _otype(d: dict | None) -> M.OCCTType | None:
    if not d:
        return None
    return M.OCCTType(**d)


def _param(d: dict) -> M.Parameter:
    return M.Parameter(type=_otype(d.get("type")), name=d.get("name", ""),
                       default_value=d.get("default_value"))


def _mdecl(d: dict) -> M.MethodDecl:
    return M.MethodDecl(
        name=d["name"],
        return_type=_otype(d.get("return_type")),
        parameters=[_param(p) for p in d.get("parameters", [])],
        kind=M.MethodKind(d["kind"]),
        is_const=d.get("is_const", False),
        is_virtual=d.get("is_virtual", False),
        is_static=d.get("is_static", False),
        is_default=d.get("is_default", False),
        is_deleted=d.get("is_deleted", False),
        is_pure_virtual=d.get("is_pure_virtual", False),
        is_overload=d.get("is_overload", False),
        overload_index=d.get("overload_index", 0),
        overload_suffix=d.get("overload_suffix", ""),
        operator_type=(M.OperatorType(d["operator_type"])
                       if d.get("operator_type") else None),
        doc=M.DocBlock(**(d.get("doc") or {})),
        skip=d.get("skip", False),
        skip_reason=d.get("skip_reason", ""),
    )


def _enum_value(d: dict) -> M.EnumValue:
    return M.EnumValue(name=d["name"], value=d.get("value"))


def _enum(d: dict) -> M.EnumDecl:
    return M.EnumDecl(
        name=d["name"],
        values=[_enum_value(v) for v in d.get("values", [])],
        is_scoped=d.get("is_scoped", False),
        is_nested=d.get("is_nested", False),
        parent_class=d.get("parent_class", ""),
        is_public=d.get("is_public", True),
        header_file=d.get("header_file", ""),
        doc=M.DocBlock(**(d.get("doc") or {})),
    )


def _field(d: dict) -> M.FieldDecl:
    return M.FieldDecl(name=d["name"], type=_otype(d.get("type")),
                       doc=M.DocBlock(**(d.get("doc") or {})),
                       is_public=d.get("is_public", True),
                       is_const=d.get("is_const", False))


def _class(d: dict) -> M.ClassDecl:
    return M.ClassDecl(
        name=d["name"],
        wrapper_name=d.get("wrapper_name", ""),
        module_name=d.get("module_name", ""),
        base_classes=list(d.get("base_classes", [])),
        kind=M.ClassKind(d.get("kind", "other")),
        is_transient_descendant=d.get("is_transient_descendant", False),
        constructors=[_mdecl(c) for c in d.get("constructors", [])],
        methods=[_mdecl(c) for c in d.get("methods", [])],
        operators=[_mdecl(c) for c in d.get("operators", [])],
        static_methods=[_mdecl(c) for c in d.get("static_methods", [])],
        fields=[_field(c) for c in d.get("fields", [])],
        static_constants=list(d.get("static_constants", [])),
        nested_enums=[_enum(c) for c in d.get("nested_enums", [])],
        header_file=d.get("header_file", ""),
        doc=M.DocBlock(**(d.get("doc") or {})),
        extra_occt_includes=list(d.get("extra_occt_includes", [])),
        has_public_default_ctor=d.get("has_public_default_ctor", False),
        has_any_ctor=d.get("has_any_ctor", False),
        has_any_public_ctor=d.get("has_any_public_ctor", False),
        has_any_nonpublic_ctor=d.get("has_any_nonpublic_ctor", False),
        has_protected_dtor=d.get("has_protected_dtor", False),
        is_template=d.get("is_template", False),
        has_pure_virtual=d.get("has_pure_virtual", False),
        is_abstract=d.get("is_abstract", False),
        has_copy_assignment=d.get("has_copy_assignment", True),
        has_operator_new_delete=d.get("has_operator_new_delete", False),
        skip=d.get("skip", False),
        skip_reason=d.get("skip_reason", ""),
    )


def load_module(path: Path) -> M.ModuleDecl:
    data = json.loads(path.read_text())
    return M.ModuleDecl(
        name=data.get("module", path.stem),
        classes=[_class(c) for c in data.get("classes", [])],
        enums=[_enum(e) for e in data.get("enums", [])],
    )
