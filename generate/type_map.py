"""Dynamic type mapping from OCCT types to wrapper/godot-cpp types."""

from __future__ import annotations

from model import ClassDecl, ClassKind, EnumDecl, OCCTType, occt_name_to_wrapper


# Primitive type mappings (OCCT -> godot-cpp)
PRIMITIVE_MAP = {
    "void": "void",
    "bool": "bool",
    "Standard_Boolean": "bool",
    "char": "char",
    "Standard_Character": "char",
    "unsigned char": "uint8_t",
    "Standard_Byte": "uint8_t",
    "short": "int16_t",
    "unsigned short": "uint16_t",
    "int": "int32_t",
    "Standard_Integer": "int32_t",
    "unsigned int": "uint32_t",
    "long": "int64_t",
    "long long": "int64_t",
    "unsigned long long": "uint64_t",
    "float": "float",
    "Standard_ShortReal": "float",
    "double": "double",
    "Standard_Real": "double",
    "long double": "double",
    "Standard_CString": "String",
}


class TypeMap:
    """Builds and queries the type mapping for a set of classified classes."""

    def __init__(self, classes: list[ClassDecl], enums: list[EnumDecl] | None = None):
        self._wrapper_names: dict[str, str] = {}  # OCCT name -> wrapper name
        self._classes: dict[str, ClassDecl] = {}  # OCCT name -> ClassDecl
        self._enum_names: set[str] = set()        # known enum type names
        self._build(classes, enums or [])

    def _build(self, classes: list[ClassDecl], enums: list[EnumDecl]):
        for cls in classes:
            self._classes[cls.name] = cls
            self._wrapper_names[cls.name] = cls.wrapper_name
            # Collect nested enum full names (e.g. "gp_Dir::D")
            for ne in cls.nested_enums:
                self._enum_names.add(f"{cls.name}::{ne.name}")
        # Collect standalone enum names
        for e in enums:
            self._enum_names.add(e.name)

    def wrapper_name(self, occt_name: str) -> str | None:
        """Get the wrapper name for an OCCT type, or None if not wrapped."""
        return self._wrapper_names.get(occt_name)

    def is_wrapped(self, occt_name: str) -> bool:
        return occt_name in self._wrapper_names

    def cpp_type_for_param(self, otype: OCCTType) -> str:
        """Get the C++ parameter type for the wrapper method signature."""
        base = otype.base_name

        # Primitives
        if base in PRIMITIVE_MAP:
            return PRIMITIVE_MAP[base]

        # Enum types (nested or standalone) → int32_t
        if self._is_enum(base):
            return "int32_t"

        # Handle types -> Ref<T>
        if otype.is_handle:
            inner = otype.handle_inner
            wname = self._wrapper_names.get(inner, inner)
            return f"Ref<{wname}>"

        # Wrappable types with wrappers -> Ref<T> for godot-cpp PtrToArg
        wname = self._wrapper_names.get(base)
        if wname:
            return f"Ref<{wname}>"

        # Unwrapped OCCT types: pass through as-is
        if otype.is_const and otype.is_ref:
            return f"const {base}&"
        if otype.is_ref:
            return f"{base}&"
        return base

    def cpp_type_for_return(self, otype: OCCTType | None) -> str:
        """Get the C++ return type for the wrapper method.

        Value type returns always become Ref<T> (the method wraps in a new object).
        Reference returns to value types also become Ref<T> (copy into wrapper).
        """
        if otype is None or otype.is_void:
            return "void"

        base = otype.base_name

        if base in PRIMITIVE_MAP:
            return PRIMITIVE_MAP[base]

        # Enum types (nested or standalone) → int32_t
        if self._is_enum(base):
            return "int32_t"

        if otype.is_handle:
            inner = otype.handle_inner
            wname = self._wrapper_names.get(inner, inner)
            return "Ref<{}>".format(wname)

        wname = self._wrapper_names.get(base)
        if wname:
            # All wrapped type returns become Ref<T> (value types wrapped in new object)
            if self.is_value_type(base):
                return "Ref<{}>".format(wname)
            # Non-value wrapped types (handles, etc.)
            if otype.is_ref and otype.is_const:
                return "const {}&".format(wname)
            if otype.is_ref:
                return "{}&".format(wname)
            return wname

        # Unwrapped types: pass through
        if otype.is_ref and otype.is_const:
            return "const {}&".format(base)
        if otype.is_ref:
            return "{}&".format(base)
        return base

    def gd_type_for_param(self, otype: OCCTType) -> str:
        """Get the GDScript-facing parameter type (for D_METHOD)."""
        base = otype.base_name

        if base in PRIMITIVE_MAP:
            gtype = PRIMITIVE_MAP[base]
            if gtype in ("int32_t", "int64_t", "uint32_t", "uint64_t", "int16_t", "uint16_t"):
                return "int"
            if gtype in ("float", "double"):
                return "float"
            if gtype == "bool":
                return "bool"
            if gtype == "String":
                return "String"
            return gtype

        # Enum types → int
        if self._is_enum(base):
            return "int"

        if otype.is_handle:
            inner = otype.handle_inner
            wname = self._wrapper_names.get(inner, inner)
            return wname

        wname = self._wrapper_names.get(base)
        if wname:
            return wname

        return base

    def gd_type_for_return(self, otype: OCCTType | None) -> str:
        """Get the GDScript-facing return type."""
        if otype is None or otype.is_void:
            return "void"
        return self.gd_type_for_param(otype)

    def is_value_type(self, occt_name: str) -> bool:
        """Check if an OCCT type is a value type (stored inline, not by handle).

        All non-REF_COUNTED wrapped types (VALUE, TOPODS_SHAPE, BUILDER, OTHER)
        store native data and need Ref<T> wrapping for godot-cpp.
        """
        cls = self._classes.get(occt_name)
        if cls:
            return cls.kind != ClassKind.REF_COUNTED
        # Default: if it's a primitive, it's a value
        return occt_name in PRIMITIVE_MAP

    def is_refcounted(self, occt_name: str) -> bool:
        """Check if an OCCT type is REF_COUNTED (uses _handle, not _native)."""
        cls = self._classes.get(occt_name)
        if cls:
            return cls.kind == ClassKind.REF_COUNTED
        return False

    def _is_enum(self, base_name: str) -> bool:
        """Check if a type name is an enum (nested or standalone)."""
        if "::" in base_name:
            return True
        return base_name in self._enum_names
