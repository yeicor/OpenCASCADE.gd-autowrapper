"""Generate the OcgEnums host class for standalone OCCT enums.

Standalone (global-scope) OCCT enums like GeomAbs_Shape have no OCCT class to
attach to, so they are all hosted on a single OcgEnums wrapper class.  GDScript
accesses them as OcgEnums.EnumName.VALUE (e.g. OcgEnums.GeomAbs_Shape.C0), and
method argument/return types reference them as OcgEnums.EnumName.
"""

from __future__ import annotations

from model import EnumDecl


def generate_enums_host_header(enums: list[EnumDecl]) -> str:
    """Generate OcgEnums.hpp — a RefCounted class hosting all standalone enums."""
    enums = sorted([e for e in enums if e.name and not e.name.startswith("(unnamed")],
                   key=lambda e: e.name)
    lines = []
    lines.append("// Auto-generated host class for standalone OCCT enums -- DO NOT EDIT")
    lines.append("#pragma once")
    lines.append("")
    lines.append("#include <godot_cpp/classes/ref_counted.hpp>")
    lines.append("#include <godot_cpp/core/class_db.hpp>")
    lines.append("")
    lines.append("#ifdef __GNUC__")
    lines.append("#pragma GCC diagnostic ignored \"-Wdeprecated-declarations\"")
    lines.append("#pragma GCC diagnostic ignored \"-Wunused-parameter\"")
    lines.append("#endif")
    lines.append("")
    headers = sorted({e.header_file.rsplit("/", 1)[-1] for e in enums if e.header_file})
    for hdr in headers:
        lines.append("#include <{}>".format(hdr))
    lines.append("")
    lines.append("namespace godot {")
    lines.append("")
    lines.append("class OcgEnums : public RefCounted {")
    lines.append("    GDCLASS(OcgEnums, RefCounted)")
    lines.append("")
    lines.append("public:")
    lines.append("    OcgEnums() = default;")
    lines.append("")
    used_enum_names: set[str] = set()
    for e in enums:
        lines.append("    enum {} : int64_t {{".format(e.name))
        for v in e.values:
            cpp_name = "{}_{}".format(e.name, v.name)
            while cpp_name in used_enum_names:
                cpp_name += "_"
            used_enum_names.add(cpp_name)
            lines.append("        {} = static_cast<int64_t>(::{}::{}),".format(cpp_name, e.name, v.name))
        lines.append("    };")
        lines.append("")
    lines.append("    static void _bind_methods();")
    lines.append("};")
    lines.append("")
    lines.append("} // namespace godot")
    lines.append("")
    for e in enums:
        lines.append("VARIANT_ENUM_CAST(OcgEnums::{});".format(e.name))
    return "\n".join(lines) + "\n"


def generate_enums_host_source(enums: list[EnumDecl]) -> str:
    """Generate OcgEnums.cpp — binds each enum value as a GDScript enum constant."""
    enums = sorted([e for e in enums if e.name and not e.name.startswith("(unnamed")],
                   key=lambda e: e.name)
    lines = []
    lines.append("// Auto-generated host class for standalone OCCT enums -- DO NOT EDIT")
    lines.append('#include "OcgEnums.hpp"')
    lines.append("")
    lines.append("#include <godot_cpp/core/error_macros.hpp>")
    lines.append("")
    lines.append("namespace godot {")
    lines.append("")
    lines.append("void OcgEnums::_bind_methods() {")
    used_enum_names: set[str] = set()
    for e in enums:
        for v in e.values:
            cpp_name = "{}_{}".format(e.name, v.name)
            while cpp_name in used_enum_names:
                cpp_name += "_"
            used_enum_names.add(cpp_name)
            lines.append('    ClassDB::bind_integer_constant(get_class_static(), "{}", "{}", static_cast<int64_t>(OcgEnums::{}));'.format(
                e.name, v.name, cpp_name))
    lines.append("}")
    lines.append("")
    lines.append("} // namespace godot")
    return "\n".join(lines) + "\n"
