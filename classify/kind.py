"""Classify classes into wrapping categories based on AST properties."""

from __future__ import annotations

from model import ClassDecl, ClassKind


def classify_class(cls: ClassDecl) -> ClassKind:
    """Determine the wrapping strategy for a class based on its AST properties.

    Classification heuristics (structural, not name-based):
    1. is_transient_descendant == True -> REF_COUNTED
    2. Base is BRepBuilderAPI_MakeShape or BRepBuilderAPI_Command -> BUILDER
    3. Has occ::handle<TopoDS_TShape> field -> TOPODS_SHAPE
    4. No base classes, not transient -> VALUE
    5. Everything else -> OTHER
    """
    # 1. Standard_Transient descendant -> REF_COUNTED
    if cls.is_transient_descendant:
        return ClassKind.REF_COUNTED

    # 2. Builder classes
    builder_bases = {"BRepBuilderAPI_MakeShape", "BRepBuilderAPI_Command"}
    for base in cls.base_classes:
        if base in builder_bases:
            return ClassKind.BUILDER
        for prefix in ("BRepPrimAPI_", "BRepBuilderAPI_", "BRepFilletAPI_",
                       "BRepOffsetAPI_", "BRepFeat_", "BRepAlgoAPI_"):
            if base.startswith(prefix):
                return ClassKind.BUILDER

    # 3. TopoDS shape types (check for handle<TopoDS_TShape> field)
    for field in cls.fields:
        if field.type.is_handle and "TopoDS_TShape" in field.type.handle_inner:
            return ClassKind.TOPODS_SHAPE

    # 4. No base classes, not transient -> VALUE
    if not cls.base_classes and not cls.is_transient_descendant:
        return ClassKind.VALUE

    # 5. Has some base but not transient -> OTHER
    return ClassKind.OTHER


def classify_all(classes: list[ClassDecl]) -> None:
    """Classify all classes in-place (sets cls.kind and cls.wrapper_name)."""
    for cls in classes:
        cls.kind = classify_class(cls)
