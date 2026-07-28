"""Data model for parsed OpenCASCADE API declarations.

Every declaration extracted by the AST parser is represented as a dataclass here.
The generator consumes these models to produce godot-cpp wrapper code.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ClassKind(enum.Enum):
    """How the OCCT class should be wrapped."""
    VALUE = "value"              # Plain C++ value type (gp_Pnt, gp_Dir, ...)
    REF_COUNTED = "ref_counted"  # Standard_Transient + Handle<T>
    TOPODS_SHAPE = "topods_shape"  # TopoDS_Shape and subtypes
    BUILDER = "builder"          # BRepBuilderAPI_MakeShape descendants
    OTHER = "other"              # Anything else (opaque, skipped or wrapped minimally)


class MethodKind(enum.Enum):
    CONSTRUCTOR = "constructor"
    METHOD = "method"
    STATIC_METHOD = "static_method"
    OPERATOR = "operator"


class OperatorType(enum.Enum):
    EQUALS = "=="
    NOT_EQUALS = "!="
    LESS = "<"
    GREATER = ">"
    PLUS = "+"
    MINUS = "-"
    MULTIPLY = "*"
    DIVIDE = "/"
    MODULO = "%"
    CROSS = "^"
    PLUS_ASSIGN = "+="
    MINUS_ASSIGN = "-="
    MULTIPLY_ASSIGN = "*="
    DIVIDE_ASSIGN = "/="
    CROSS_ASSIGN = "^="
    UNARY_MINUS = "unary_minus"
    UNARY_PLUS = "unary_plus"
    DEREFERENCE = "*deref"
    CALL = "call"


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------

@dataclass
class DocBlock:
    """Extracted documentation from an OCCT declaration."""
    brief: str = ""                 # From cursor.brief_comment
    raw: str = ""                   # From cursor.raw_comment (full OCCT doc)
    params: dict[str, str] = field(default_factory=dict)  # @param name desc
    returns: str = ""               # @return description
    notes: list[str] = field(default_factory=list)  # @note, warnings, etc.


# ---------------------------------------------------------------------------
# Type representation
# ---------------------------------------------------------------------------

@dataclass
class OCCTType:
    """Represents an OCCT C++ type with full qualification info."""
    spelling: str                   # Raw clang spelling (e.g. "const gp_Pnt &")
    base_name: str                  # Clean base name (e.g. "gp_Pnt")
    is_const: bool = False
    is_ref: bool = False            # &
    is_pointer: bool = False        # *
    is_handle: bool = False         # opencascade::handle<T>
    handle_inner: str = ""          # T if is_handle
    is_transient_descendant: bool = False  # Inherits from Standard_Transient

    @property
    def is_void(self) -> bool:
        return self.base_name == "void"

    @property
    def is_primitive(self) -> bool:
        return self.base_name in (
            "void", "bool", "char", "unsigned char", "signed char",
            "short", "unsigned short", "int", "unsigned int",
            "long", "unsigned long", "long long", "unsigned long long",
            "int16_t", "uint16_t", "int32_t", "uint32_t", "int64_t", "uint64_t",
            "float", "double", "long double",
            "Standard_Boolean", "Standard_Character", "Standard_Byte",
            "Standard_Integer", "Standard_Real", "Standard_ShortReal",
            "Standard_CString",
        )

    @property
    def is_string(self) -> bool:
        return self.base_name in ("Standard_CString", "TCollection_AsciiString",
                                   "TCollection_ExtendedString", "std::string")

    @property
    def unwrappable(self) -> bool:
        """Type that cannot be wrapped in GDScript."""
        return self.base_name in (
            "Standard_OStream", "Standard_IStream", "Standard_SStream",
            "Standard_ProgramAddress",
        ) or self.is_pointer and not self.is_handle


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------

@dataclass
class EnumValue:
    name: str
    value: int | None = None


@dataclass
class EnumDecl:
    name: str
    values: list[EnumValue] = field(default_factory=list)
    is_scoped: bool = False        # enum class vs enum
    is_nested: bool = False        # declared inside a class
    parent_class: str = ""
    header_file: str = ""
    doc: DocBlock = field(default_factory=DocBlock)


@dataclass
class Parameter:
    type: OCCTType
    name: str
    default_value: str | None = None


@dataclass
class MethodDecl:
    name: str
    return_type: OCCTType | None = None  # None = void
    parameters: list[Parameter] = field(default_factory=list)
    kind: MethodKind = MethodKind.METHOD
    is_const: bool = False
    is_virtual: bool = False
    is_static: bool = False
    is_default: bool = False       # = default
    is_deleted: bool = False       # = delete
    is_pure_virtual: bool = False  # = 0
    is_overload: bool = False      # has same-name sibling
    overload_index: int = 0        # 0-based index among overloads
    operator_type: OperatorType | None = None
    doc: DocBlock = field(default_factory=DocBlock)
    skip: bool = False             # True if unwrappable
    skip_reason: str = ""


@dataclass
class FieldDecl:
    name: str
    type: OCCTType
    doc: DocBlock = field(default_factory=DocBlock)


@dataclass
class ClassDecl:
    name: str
    wrapper_name: str = ""         # e.g. "OcgGpPnt" (set during classification)
    module_name: str = ""          # e.g. "gp" (set during scanning)
    base_classes: list[str] = field(default_factory=list)
    kind: ClassKind = ClassKind.OTHER
    is_transient_descendant: bool = False  # anywhere in hierarchy
    constructors: list[MethodDecl] = field(default_factory=list)
    methods: list[MethodDecl] = field(default_factory=list)
    operators: list[MethodDecl] = field(default_factory=list)
    static_methods: list[MethodDecl] = field(default_factory=list)
    fields: list[FieldDecl] = field(default_factory=list)
    nested_enums: list[EnumDecl] = field(default_factory=list)
    header_file: str = ""
    doc: DocBlock = field(default_factory=DocBlock)
    has_public_default_ctor: bool = False
    transitive_occt_includes: list[str] = field(default_factory=list)  # OCCT headers transitively included by this header

    @property
    def all_methods(self) -> list[MethodDecl]:
        return self.constructors + self.methods + self.operators + self.static_methods

    @property
    def all_wrappable_methods(self) -> list[MethodDecl]:
        return [m for m in self.all_methods if not m.skip]


@dataclass
class ModuleDecl:
    """All declarations extracted from a set of headers belonging to one OCCT module."""
    name: str                      # e.g. "gp", "TopoDS", "BRepPrimAPI"
    classes: list[ClassDecl] = field(default_factory=list)
    enums: list[EnumDecl] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Naming utilities
# ---------------------------------------------------------------------------

def occt_name_to_wrapper(occt_name: str, module_name: str) -> str:
    """Convert an OCCT class name to a wrapper name with Ocg prefix.

    Examples:
        gp_Pnt, gp       -> OcgGpPnt
        TopoDS_Shape, TopoDS -> OcgTopoDSShape
        BRepPrimAPI_MakeBox, BRepPrimAPI -> OcgBRepPrimAPIMakeBox
    """
    # Convert underscores and special chars to camel case
    parts = occt_name.replace("::", "_").split("_")
    camel = "".join(p.capitalize() if not p.isupper() else p for p in parts)
    return f"Ocg{camel}"


def wrapper_name_for_enum(enum_name: str, parent_class: str) -> str:
    """Generate a unique enum name for binding (avoids collision with nested enum names)."""
    if parent_class:
        return f"{parent_class}_{enum_name}"
    return enum_name
