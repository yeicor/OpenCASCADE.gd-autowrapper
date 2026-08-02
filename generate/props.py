"""Plan GDScript-visible properties from public OCCT fields.

OCCT value structs (gp_XYZ, gp_XY, gp_Dir, ...) expose their coordinate
members as public fields.  Surfaces them as real Godot properties so GDScript
can do `pt.x = 3.0` instead of `pt.SetX(3.0)`.

Conservative by design: only public fields of value classes with inline
storage (`_native` is a value, not a unique_ptr) are exposed, and only when
the field type is directly bindable (scalars, enums, strings, nested value
structs).  Fields whose name collides with an existing bound method (case
insensitively) are skipped — Godot method/property names share one namespace.
"""

from __future__ import annotations

from dataclasses import dataclass

from model import ClassDecl, ClassKind, FieldDecl
from generate.type_map import PRIMITIVE_MAP

# Property accessor method prefix.  Distinctive enough that collisions with
# OCCT methods are essentially impossible, but checked anyway.
_ACCESSOR_PREFIX = "_ocg_field_get_"
_SETTER_PREFIX = "_ocg_field_set_"

# Godot Object methods that OCCT fields must never shadow.
_RESERVED_NAMES = {
    "get_class", "is_class", "set", "get", "free", "queue_free", "_init",
    "notification", "connect", "disconnect", "emit_signal", "has_method",
    "call", "call_deferred", "set_script", "get_script", "set_name",
    "get_name", "get_parent", "duplicate", "to_string", "get_instance_id",
    "get_meta", "set_meta", "remove_meta", "get_signal_list",
    "get_method_list", "get_property_list", "property_list_changed_notify",
    "has_signal", "is_connected", "is_queued_for_deletion", "add_user_signal",
    "set_message_translation", "can_translate_messages", "tr", "tr_n",
    "get_rid", "get_tree", "is_inside_tree", "get_index",
}


@dataclass
class PropertyPlan:
    """Everything needed to emit one property (declarations, impls, bindings)."""
    name: str                 # GDScript property name (the OCCT field name)
    field_base: str           # OCCT field type base name (for wrapper include)
    getter: str               # bound getter method name
    setter: str               # bound setter method name
    getter_ret: str           # C++ getter return type
    setter_param: str         # C++ setter parameter type
    getter_body: list[str]    # C++ getter body lines (between braces)
    setter_body: list[str]    # C++ setter body lines (between braces)
    prop_type: str            # Variant::* type for PropertyInfo
    hint: str = "PROPERTY_HINT_NONE"
    hint_string: str = '""'   # C++ hint string literal


def plan_properties(cls: ClassDecl, type_map) -> list[PropertyPlan]:
    """Compute the property plan for a class (empty unless it's a data struct)."""
    if cls.kind != ClassKind.VALUE or not cls.has_public_default_ctor:
        return []
    reserved = _collect_exposed_names(cls, type_map)
    plans = []
    for f in cls.fields:
        plan = _plan_field(cls, f, type_map, reserved)
        if plan is not None:
            plans.append(plan)
    return plans


def _collect_exposed_names(cls: ClassDecl, type_map, acc: set[str] | None = None) -> set[str]:
    """Case-folded names of all bound methods in the OCCT inheritance chain."""
    if acc is None:
        acc = set()
    from classify.overloads import get_method_unique_name
    for m in cls.all_wrappable_methods:
        acc.add(get_method_unique_name(m).lower())
    for base in cls.base_classes:
        bcls = type_map.class_decl(base)
        if bcls is not None:
            _collect_exposed_names(bcls, type_map, acc)
    return acc


def _plan_field(cls: ClassDecl, field: FieldDecl, type_map, reserved: set[str]) -> PropertyPlan | None:
    from classify.overloads import to_snake_case
    native = field.name
    name = to_snake_case(field.name)
    base = field.type.base_name
    if not field.is_public or not name or name.startswith("_") or name.lower() in reserved or name.lower() in _RESERVED_NAMES:
        return None
    t = field.type
    if t.is_pointer or t.is_ref or t.is_handle or t.unwrappable or "[" in base:
        return None
    if base in ("void", "Standard_OStream", "Standard_IStream", "Standard_SStream",
                "Standard_CString", "Standard_ProgramAddress"):
        return None

    getter = _ACCESSOR_PREFIX + name
    setter = _SETTER_PREFIX + name
    if getter.lower() in reserved or setter.lower() in reserved:
        return None

    def _mk(prop_type, getter_ret, setter_param, getter_body, setter_body,
            hint="PROPERTY_HINT_NONE", hint_string='""'):
        return PropertyPlan(
            name=name, field_base=base, getter=getter, setter=setter,
            getter_ret=getter_ret, setter_param=setter_param,
            getter_body=getter_body, setter_body=setter_body,
            prop_type=prop_type, hint=hint, hint_string=hint_string,
        )

    if base in ("bool", "Standard_Boolean"):
        return _mk("Variant::BOOL", "bool", "bool",
                   ["return _native.{};".format(native)],
                   ["_native.{} = value;".format(native)])

    if base in ("double", "Standard_Real", "long double"):
        return _mk("Variant::FLOAT", "double", "double",
                   ["return _native.{};".format(native)],
                   ["_native.{} = value;".format(native)])
    if base in ("float", "Standard_ShortReal"):
        return _mk("Variant::FLOAT", "float", "float",
                   ["return _native.{};".format(native)],
                   ["_native.{} = value;".format(native)])

    if base == "TCollection_AsciiString":
        return _mk("Variant::STRING", "String", "const ::godot::String&",
                   ["return ::godot::String::utf8(_native.{}.ToCString());".format(native)],
                   ["_native.{} = TCollection_AsciiString(value.utf8().get_data());".format(native)])
    if base == "TCollection_ExtendedString":
        return _mk("Variant::STRING", "String", "const ::godot::String&",
                   ["auto ocg_s = _native.{};".format(native),
                    "Standard_Integer ocg_len = ocg_s.LengthOfCString();",
                    "char* ocg_buf = new char[ocg_len + 1];",
                    "ocg_s.ToUTF8CString(ocg_buf);",
                    "ocg_buf[ocg_len] = '\\0';",
                    "::godot::String ocg_ret = ::godot::String::utf8(ocg_buf);",
                    "delete[] ocg_buf;",
                    "return ocg_ret;"],
                   ["_native.{} = TCollection_ExtendedString(value.utf8().get_data());".format(native)])

    if base in PRIMITIVE_MAP:
        wrapper_type = PRIMITIVE_MAP[base]
        if wrapper_type in ("bool", "String", "double", "float"):
            pass  # handled above
        else:
            # Integer-flavoured field (int32_t / uint64_t / ... bind as INT).
            if base in ("char", "Standard_Character", "char16_t", "Standard_ExtCharacter"):
                occt_cast = {"char": "char", "Standard_Character": "Standard_Character",
                             "char16_t": "char16_t", "Standard_ExtCharacter": "Standard_ExtCharacter"}[base]
                return _mk("Variant::INT", "int32_t", "int32_t",
                           ["return static_cast<int32_t>(_native.{});".format(native)],
                           ["_native.{} = static_cast<{}>(value);".format(native, occt_cast)])
            return _mk("Variant::INT", wrapper_type, wrapper_type,
                       ["return _native.{};".format(native)],
                       ["_native.{} = value;".format(native)])

    if type_map._is_enum(base):
        hint_string = type_map.enum_hint_string(base)
        qname = type_map.qualified_enum_name(base, cls.name)
        hint = "PROPERTY_HINT_ENUM" if hint_string else "PROPERTY_HINT_NONE"
        return _mk("Variant::INT", "int64_t", "int64_t",
                   ["return static_cast<int64_t>(_native.{});".format(native)],
                   ["_native.{} = static_cast<{}>(value);".format(native, qname)],
                   hint=hint,
                   hint_string='"{}"'.format(hint_string) if hint_string else '""')

    wname = type_map.wrapper_name(base)
    if (wname and type_map.class_kind(base) == ClassKind.VALUE
            and type_map.has_public_default_ctor(base) and type_map.is_copyable(base)):
        return _mk("Variant::OBJECT", "Ref<{}>".format(wname), "Ref<{}>".format(wname),
                   ["Ref<{}> w; w.instantiate();".format(wname),
                    "w->_native = _native.{};".format(native),
                    "return w;"],
                   ["ERR_FAIL_NULL(value);",
                    "_native.{} = value->_native;".format(native)],
                   hint="PROPERTY_HINT_RESOURCE_TYPE",
                   hint_string='"{}"'.format(wname))

    return None
