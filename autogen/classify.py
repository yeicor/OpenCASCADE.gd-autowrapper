"""Classify scanned OCCT classes into wrapping strategies.

Structural rules only (no source-text heuristics).  The kinds mirror the
legacy pipeline so the generated API contract stays identical:

1. Standard_Transient/Standard_Persistent descendants        -> REF_COUNTED
2. BRepBuilderAPI_MakeShape/BRep*API_Command descendants     -> BUILDER
3. classes owning an occ::handle<TopoDS_TShape> field        -> TOPODS_SHAPE
4. no bases, not transient                                   -> VALUE
5. everything else                                           -> OTHER (wrapped as value)

Class-level skips mirror the legacy extraction rules (occast/classes.py):
template classes, protected destructors (non-transient), classes with no
public constructors (non-transient), abstract non-transient classes,
Standard_* exception hierarchies, and internal TopoDS_T* implementations.
"""

from __future__ import annotations

import logging

from .model import ClassDecl, ClassKind, ModuleDecl

log = logging.getLogger("autogen.classify")

_BUILDER_BASES = {"BRepBuilderAPI_MakeShape", "BRepBuilderAPI_Command"}
_BUILDER_PREFIXES = ("BRepPrimAPI_", "BRepBuilderAPI_", "BRepFilletAPI_",
                     "BRepOffsetAPI_", "BRepFeat_", "BRepAlgoAPI_")
_EXCEPTION_ROOT = "Standard_Failure"
_EXCEPTION_BASE_KEYWORDS = ("Failure", "Error", "OutOfRange", "OutOfMemory",
                            "DomainError", "RangeError")


def classify_kind(cls: ClassDecl) -> ClassKind:
    if cls.is_transient_descendant or cls.name in ("Standard_Transient",
                                                   "Standard_Persistent"):
        return ClassKind.REF_COUNTED
    for base in cls.base_classes:
        if base in _BUILDER_BASES or base.startswith(_BUILDER_PREFIXES):
            return ClassKind.BUILDER
    for f in cls.fields:
        if f.type.is_handle and "TopoDS_TShape" in f.type.handle_inner:
            return ClassKind.TOPODS_SHAPE
    if not cls.base_classes:
        return ClassKind.VALUE
    return ClassKind.OTHER


def _is_failure_descendant(cls: ClassDecl, by_name: dict[str, ClassDecl],
                           seen: set[str]) -> bool:
    if cls.name in seen:
        return False
    seen.add(cls.name)
    for base in cls.base_classes:
        if base == _EXCEPTION_ROOT:
            return True
        parent = by_name.get(base)
        if parent is not None and _is_failure_descendant(parent, by_name, seen):
            return True
    return False


def _has_custom_alloc(cls: ClassDecl, by_name: dict[str, ClassDecl],
                      seen: set[str]) -> bool:
    """True if cls or any (module-local) base declares operator new/delete."""
    if cls.name in seen:
        return False
    seen.add(cls.name)
    if cls.has_operator_new_delete:
        return True
    for base in cls.base_classes:
        b = by_name.get(base)
        if b is not None and _has_custom_alloc(b, by_name, seen):
            return True
    return False


def _skip_reason(cls: ClassDecl, by_name: dict[str, ClassDecl]) -> str:
    """Legacy class-level skip rules; "" means the class is wrapped."""
    if cls.name == cls.module_name:
        return ""  # module aggregate host (e.g. Standard, gp): keep it
    if cls.name == _EXCEPTION_ROOT:
        return "root OCCT exception"
    if cls.is_template:
        return "template class"
    if cls.kind != ClassKind.REF_COUNTED:
        if cls.has_protected_dtor:
            return "protected destructor"
        if cls.has_any_nonpublic_ctor and not cls.has_any_public_ctor:
            return "no public constructors"
        if cls.has_pure_virtual:
            return "abstract (pure virtual) class"
        # Non-default-constructible classes are stored via unique_ptr, which
        # needs the global operator new/delete; OCCT collection nodes instead
        # declare placement new/delete (DEFINE_NCOLLECTION_ALLOC) that hide it,
        # and some classes inherit it protectedly so it becomes inaccessible.
        if not cls.has_public_default_ctor and _has_custom_alloc(cls, by_name, set()):
            return "custom allocation (operator new/delete)"
    for base in cls.base_classes:
        if base.startswith("Standard_") and any(
                kw in base for kw in _EXCEPTION_BASE_KEYWORDS):
            return "Standard_* exception base"
        if base.startswith("TopoDS_T") and base != "TopoDS_TShape":
            return "internal TopoDS shape implementation"
    if _is_failure_descendant(cls, by_name, set()):
        return "derives from Standard_Failure (exception)"
    return ""


def classify_module(module: ModuleDecl) -> None:
    """Set kind/wrapper_name/skip for every class in the module, in-place."""
    by_name: dict[str, ClassDecl] = {c.name: c for c in module.classes}

    for cls in module.classes:
        cls.kind = classify_kind(cls)
        cls.wrapper_name = occt_wrapper_name(cls.name, cls.module_name)

    reasons: dict[str, str] = {}
    for cls in module.classes:
        reason = _skip_reason(cls, by_name)
        if reason:
            reasons[cls.name] = reason

    for cls in module.classes:
        if cls.name in reasons:
            cls.skip = True
            cls.skip_reason = reasons[cls.name]
            cls.kind = ClassKind.OTHER
            log.info("skip %s: %s", cls.name, reasons[cls.name])


def occt_wrapper_name(occt_name: str, module_name: str) -> str:
    """Ocg prefix + camelized name (module aggregate keeps its plain name)."""
    if occt_name == module_name:
        return f"Ocg{occt_name}"
    from .model import occt_name_to_wrapper
    return occt_name_to_wrapper(occt_name, module_name)
