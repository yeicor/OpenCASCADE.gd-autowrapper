"""Code generator — produces godot-cpp wrapper C++ from the parsed model.

Handles the common patterns:
  - Value types (gp_Pnt, gp_Dir, gp_Vec, ...)
  - TopoDS shape hierarchy
  - Builder classes (BRepPrimAPI_Make*, ...)
  - Handle / ref-counted types
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from model import (
    ClassDecl,
    ClassKind,
    EnumDecl,
    EnumValue,
    FieldDecl,
    MethodDecl,
    MethodKind,
    ModuleDecl,
    OperatorType,
    Parameter,
)


# ---------------------------------------------------------------------------
# Type mapping: OCCT types -> godot-cpp Variant types
# ---------------------------------------------------------------------------

PRIMITIVE_TYPES = {
    'int', 'Standard_Integer', 'int32_t', 'int64_t',
    'double', 'Standard_Real', 'Standard_ShortReal', 'float',
    'bool', 'Standard_Boolean',
    'Standard_CString', 'const char*', 'const char *',
    'Standard_Character',
}

OCCT_TO_GODOT_TYPE = {
    'Standard_Integer': 'int64_t',
    'Standard_Real': 'double',
    'Standard_ShortReal': 'float',
    'Standard_Boolean': 'bool',
    'Standard_CString': 'String',
}

# Types that are passed by value but should be wrapped
VALUE_TYPES = {
    'gp_Pnt', 'gp_Pnt2d', 'gp_Dir', 'gp_Dir2d',
    'gp_Vec', 'gp_Vec2d',
    'gp_XYZ', 'gp_XY',
    'gp_Ax1', 'gp_Ax2', 'gp_Ax3',
    'gp_Trsf', 'gp_GTrsf', 'gp_GTrsf2d',
    'gp_Mat', 'gp_Mat2d',
    'gp_Pln', 'gp_Lin', 'gp_Circ',
    'gp_Quaternion',
    'Quantity_Color', 'Quantity_ColorRGBA',
    'gp_Cone', 'gp_Cylinder', 'gp_Sphere', 'gp_Torus',
    'gp_Elips', 'gp_Hypr', 'gp_Parab',
}

SHAPE_TYPES = {
    'TopoDS_Shape', 'TopoDS_Face', 'TopoDS_Edge', 'TopoDS_Wire',
    'TopoDS_Vertex', 'TopoDS_Shell', 'TopoDS_Solid',
    'TopoDS_Compound', 'TopoDS_CompSolid',
}


def _sanitize_name(name: str) -> str:
    """Make a name safe for godot-cpp (replace special chars)."""
    return name.replace('::', '_').replace('<', '_').replace('>', '_').replace('*', '').replace('&', '').replace(' ', '')


def _gd_type(cpp_type: str) -> str:
    """Map a C++ type to a godot Variant-compatible type name for binding."""
    clean = cpp_type.strip()
    if clean in OCCT_TO_GODOT_TYPE:
        return OCCT_TO_GODOT_TYPE[clean]
    if clean in PRIMITIVE_TYPES:
        if 'int' in clean or 'Integer' in clean:
            return 'int64_t'
        if 'double' in clean or 'Real' in clean:
            return 'double'
        if 'float' in clean or 'ShortReal' in clean:
            return 'float'
        if 'bool' in clean or 'Boolean' in clean:
            return 'bool'
    # String types
    if 'TCollection_AsciiString' in clean or 'Standard_CString' in clean:
        return 'String'
    # OCCT types stay as-is (our wrappers)
    return _sanitize_name(clean)


def _is_primitive(cpp_type: str) -> bool:
    clean = cpp_type.strip().replace('const', '').replace('&', '').replace('*', '').strip()
    return clean in PRIMITIVE_TYPES or clean in ('int', 'double', 'float', 'bool')


def _is_value_type(name: str) -> bool:
    return name in VALUE_TYPES


def _is_shape_type(name: str) -> bool:
    return name in SHAPE_TYPES


def _method_cname(cls_name: str, method: MethodDecl) -> str:
    """Generate the C++ method name for binding."""
    if method.operator_type:
        op_map = {
            OperatorType.PLUS: 'add',
            OperatorType.MINUS: 'subtract',
            OperatorType.MULTIPLY: 'multiply',
            OperatorType.DIVIDE: 'divide',
            OperatorType.CROSS: 'cross',
            OperatorType.EQUALS: 'equal',
            OperatorType.NOT_EQUALS: 'not_equal',
            OperatorType.UNARY_MINUS: 'negate',
        }
        return op_map.get(method.operator_type, f'op_{method.operator_type.value}')
    return method.name


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------

class WrapperGenerator:
    """Generates godot-cpp wrapper C++ code from parsed declarations."""

    def __init__(self, output_dir: str, module_name: str):
        self.output_dir = Path(output_dir)
        self.module_name = module_name
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_module(self, mod: ModuleDecl) -> list[str]:
        """Generate wrapper code for all classes in a module. Returns generated file paths."""
        files = []
        for cls in mod.classes:
            path = self.generate_class(cls)
            if path:
                files.append(path)
        return files

    def generate_class(self, cls: ClassDecl) -> str | None:
        """Generate wrapper for a single class. Returns the .cpp file path."""
        if cls.kind == ClassKind.OTHER and not cls.base_classes:
            return None  # skip un-wrappable forward-declared classes

        wrapper_name = f'OCP_{cls.name}'
        header_path = self.output_dir / f'{cls.name}.hpp'
        source_path = self.output_dir / f'{cls.name}.cpp'

        header_content = self._gen_header(cls, wrapper_name)
        source_content = self._gen_source(cls, wrapper_name)

        header_path.write_text(header_content)
        source_path.write_text(source_content)

        return str(source_path)

    # -------------------------------------------------------------------
    # Header generation
    # -------------------------------------------------------------------

    def _gen_header(self, cls: ClassDecl, wrapper_name: str) -> str:
        lines = []
        lines.append(f'// Auto-generated wrapper for {cls.name} — DO NOT EDIT')
        lines.append(f'#pragma once')
        lines.append(f'')
        lines.append(f'#include <godot_cpp/classes/ref_counted.hpp>')
        lines.append(f'#include <godot_cpp/core/class_db.hpp>')
        lines.append(f'#include <{cls.name}.hxx>')
        lines.append(f'')
        lines.append(f'namespace godot {{')
        lines.append(f'')
        lines.append(f'class {wrapper_name} : public RefCounted {{')
        lines.append(f'    GDCLASS({wrapper_name}, RefCounted)')
        lines.append(f'')
        lines.append(f'public:')

        # Internal storage
        if cls.kind == ClassKind.VALUE:
            lines.append(f'    {cls.name} _native;')
        elif cls.kind == ClassKind.TOPODS_SHAPE:
            lines.append(f'    TopoDS_Shape _native;')
        elif cls.kind == ClassKind.BUILDER:
            lines.append(f'    // Builder stores result after Build()')
            lines.append(f'    TopoDS_Shape _result;')
            # Store builder as unique_ptr to handle polymorphism
            lines.append(f'    std::unique_ptr<{cls.name}> _builder;')
        else:
            lines.append(f'    {cls.name} _native;')
        lines.append(f'')

        lines.append(f'    static void _bind_methods();')
        lines.append(f'')

        # Constructors
        for ctor in cls.constructors:
            params = self._gen_param_list(ctor)
            lines.append(f'    {wrapper_name}({params});')

        # Default constructor if none provided
        if not cls.constructors:
            lines.append(f'    {wrapper_name}() = default;')

        lines.append(f'')

        # Regular methods
        for method in cls.methods:
            self._gen_method_decl(lines, method, wrapper_name)

        # Operators
        for op in cls.operators:
            self._gen_operator_decl(lines, op, wrapper_name)

        # Static methods
        for sm in cls.static_methods:
            self._gen_static_method_decl(lines, sm, wrapper_name)

        # Nested enums as constants (flat)
        for enum in cls.nested_enums:
            for val in enum.values:
                const_name = f'{enum.name}_{val.name}'
                lines.append(f'    enum {const_name} {{ {const_name}_VALUE = static_cast<int>({cls.name}::{enum.name}::{val.name}) }};')

        lines.append(f'}};')
        lines.append(f'')
        lines.append(f'}} // namespace godot')
        return '\n'.join(lines) + '\n'

    # -------------------------------------------------------------------
    # Source generation
    # -------------------------------------------------------------------

    def _gen_source(self, cls: ClassDecl, wrapper_name: str) -> str:
        lines = []
        lines.append(f'// Auto-generated wrapper for {cls.name} — DO NOT EDIT')
        lines.append(f'#include "{cls.name}.hpp"')
        lines.append(f'')
        lines.append(f'#include <godot_cpp/core/error_macros.hpp>')
        lines.append(f'')
        lines.append(f'namespace godot {{')
        lines.append(f'')

        # _bind_methods
        lines.append(f'void {wrapper_name}::_bind_methods() {{')

        # Bind constructors
        for ctor in cls.constructors:
            params = self._bind_param_list(ctor)
            if params:
                lines.append(f'    ClassDB::bind_constructor(D_METHOD({params}), &{wrapper_name}::_new);')
            else:
                lines.append(f'    ClassDB::bind_constructor(D_METHOD(), &{wrapper_name}::_new);')

        # Bind regular methods
        for method in cls.methods:
            self._gen_bind_method(lines, method, wrapper_name)

        # Bind operators as methods
        for op in cls.operators:
            self._gen_bind_operator(lines, op, wrapper_name)

        # Bind static methods
        for sm in cls.static_methods:
            self._gen_bind_static_method(lines, sm, wrapper_name)

        # Bind nested enums
        for enum in cls.nested_enums:
            for val in enum.values:
                const_name = f'{enum.name}_{val.name}'
                lines.append(f'    BIND_ENUM_CONSTANT({const_name}_VALUE);')

        lines.append(f'}}')
        lines.append(f'')

        # Constructor implementations
        for ctor in cls.constructors:
            self._gen_ctor_impl(lines, ctor, cls, wrapper_name)

        # Method implementations
        for method in cls.methods:
            self._gen_method_impl(lines, method, cls, wrapper_name)

        # Operator implementations
        for op in cls.operators:
            self._gen_operator_impl(lines, op, cls, wrapper_name)

        # Static method implementations
        for sm in cls.static_methods:
            self._gen_static_method_impl(lines, sm, cls, wrapper_name)

        lines.append(f'}} // namespace godot')
        return '\n'.join(lines) + '\n'

    # -------------------------------------------------------------------
    # Helpers for header generation
    # -------------------------------------------------------------------

    def _gen_param_list(self, method: MethodDecl) -> str:
        parts = []
        for p in method.parameters:
            # Map OCCT types to godot-friendly types
            type_str = self._gd_param_type(p)
            parts.append(f'{type_str} {p.name}')
        return ', '.join(parts)

    def _gd_param_type(self, param: Parameter) -> str:
        """Generate the godot-facing parameter type."""
        clean = param.type_name.replace('const', '').replace('&', '').replace('*', '').strip()
        if clean in VALUE_TYPES:
            if param.is_const_ref:
                return f'const {clean}&'
            return clean
        if clean in SHAPE_TYPES:
            if param.is_const_ref:
                return f'const {clean}&'
            return clean
        return _gd_type(param.type_name)

    def _gen_method_decl(self, lines: list, method: MethodDecl, wrapper_name: str):
        ret = _gd_type(method.return_type) if method.return_type else 'void'
        params = self._gen_param_list(method)
        const = ' const' if method.is_const else ''
        lines.append(f'    {ret} {method.name}({params}){const};')

    def _gen_operator_decl(self, lines: list, method: MethodDecl, wrapper_name: str):
        name = _method_cname(wrapper_name, method)
        params = self._gen_param_list(method)
        ret = _gd_type(method.operator_return_type or method.return_type) if (method.operator_return_type or method.return_type) else 'void'
        const = ' const' if method.is_const else ''
        lines.append(f'    {ret} {name}({params}){const};')

    def _gen_static_method_decl(self, lines: list, method: MethodDecl, wrapper_name: str):
        ret = _gd_type(method.return_type) if method.return_type else 'void'
        params = self._gen_param_list(method)
        lines.append(f'    static {ret} {method.name}({params});')

    # -------------------------------------------------------------------
    # Helpers for source generation
    # -------------------------------------------------------------------

    def _bind_param_list(self, method: MethodDecl) -> str:
        parts = ['"' + method.name + '"']
        for p in method.parameters:
            parts.append(f'"{p.name}"')
        return ', '.join(parts)

    def _gen_bind_method(self, lines: list, method: MethodDecl, wrapper_name: str):
        params = self._bind_param_list(method)
        lines.append(f'    ClassDB::bind_method(D_METHOD({params}), &{wrapper_name}::{method.name});')

    def _gen_bind_operator(self, lines: list, method: MethodDecl, wrapper_name: str):
        name = _method_cname(wrapper_name, method)
        params = self._bind_param_list(method)
        lines.append(f'    ClassDB::bind_method(D_METHOD({params}), &{wrapper_name}::{name});')

    def _gen_bind_static_method(self, lines: list, method: MethodDecl, wrapper_name: str):
        params = self._bind_param_list(method)
        lines.append(f'    ClassDB::bind_static_method(D_METHOD({params}), &{wrapper_name}::{method.name});')

    def _gen_ctor_impl(self, lines: list, ctor: MethodDecl, cls: ClassDecl, wrapper_name: str):
        params = self._gen_param_list(ctor)
        lines.append(f'{wrapper_name}::{wrapper_name}({params}) : RefCounted() {{')

        if cls.kind == ClassKind.BUILDER:
            # Builder: create the builder
            ctor_args = ', '.join(p.name for p in ctor.parameters)
            if ctor_args:
                lines.append(f'    _builder = std::make_unique<{cls.name}>({ctor_args});')
            else:
                lines.append(f'    _builder = std::make_unique<{cls.name}>();')
            lines.append(f'    _builder->Build();')
            lines.append(f'    _result = _builder->Shape();')
        elif ctor.parameters:
            ctor_args = ', '.join(p.name for p in ctor.parameters)
            lines.append(f'    _native = {cls.name}({ctor_args});')
        else:
            lines.append(f'    _native = {cls.name}();')

        lines.append(f'}}')
        lines.append(f'')

    def _gen_method_impl(self, lines: list, method: MethodDecl, cls: ClassDecl, wrapper_name: str):
        ret = _gd_type(method.return_type) if method.return_type else 'void'
        params = self._gen_param_list(method)
        const = ' const' if method.is_const else ''
        lines.append(f'{ret} {wrapper_name}::{method.name}({params}){const} {{')

        # Build argument list
        args = []
        for p in method.parameters:
            args.append(p.name)

        # Determine what object to call on
        if cls.kind == ClassKind.BUILDER and method.name == 'Shape':
            lines.append(f'    return _result;')
        elif cls.kind == ClassKind.BUILDER:
            args_str = ', '.join(args)
            lines.append(f'    return _builder->{method.name}({args_str});')
        elif cls.kind == ClassKind.TOPODS_SHAPE:
            args_str = ', '.join(args)
            lines.append(f'    return _native.{method.name}({args_str});')
        elif method.return_type and method.return_type.strip() not in ('void',):
            if cls.kind == ClassKind.VALUE:
                args_str = ', '.join(args)
                ret_type = method.return_type.strip()
                if _is_value_type(ret_type.replace('const', '').replace('&', '').strip()):
                    # Return by value — wrap in OCP_ type
                    ret_clean = ret_type.replace('const', '').replace('&', '').strip()
                    lines.append(f'    auto result = _native.{method.name}({args_str});')
                    lines.append(f'    auto wrapper = Ref<{_sanitize_name(ret_clean)}>::create();')
                    lines.append(f'    wrapper->_native = result;')
                    lines.append(f'    return wrapper;')
                else:
                    lines.append(f'    return _native.{method.name}({args_str});')
            else:
                args_str = ', '.join(args)
                lines.append(f'    return _native.{method.name}({args_str});')
        else:
            args_str = ', '.join(args)
            if cls.kind == ClassKind.VALUE:
                lines.append(f'    _native.{method.name}({args_str});')
            else:
                lines.append(f'    _native.{method.name}({args_str});')

        lines.append(f'}}')
        lines.append(f'')

    def _gen_operator_impl(self, lines: list, method: MethodDecl, cls: ClassDecl, wrapper_name: str):
        name = _method_cname(wrapper_name, method)
        params = self._gen_param_list(method)
        const = ' const' if method.is_const else ''
        ret = _gd_type(method.operator_return_type or method.return_type) if (method.operator_return_type or method.return_type) else 'void'

        lines.append(f'{ret} {wrapper_name}::{name}({params}){const} {{')

        args = [p.name for p in method.parameters]
        args_str = ', '.join(args)

        if method.operator_type in (OperatorType.PLUS_ASSIGN, OperatorType.MINUS_ASSIGN,
                                     OperatorType.MULTIPLY_ASSIGN, OperatorType.DIVIDE_ASSIGN):
            # In-place operators: return self
            lines.append(f'    _native.operator{method.operator_type.value}({args_str});')
            lines.append(f'    return this;')
        elif method.operator_type == OperatorType.UNARY_MINUS:
            lines.append(f'    return -_native;')
        else:
            # Binary operators: return new value
            op_char = method.operator_type.value if method.operator_type else method.name.replace('operator', '')
            lines.append(f'    auto result = _native {op_char} {args_str};')
            # Wrap result if it's a value type
            ret_clean = (method.operator_return_type or method.return_type or '').replace('const', '').replace('&', '').strip()
            if _is_value_type(ret_clean):
                lines.append(f'    auto wrapper = Ref<{_sanitize_name(ret_clean)}>::create();')
                lines.append(f'    wrapper->_native = result;')
                lines.append(f'    return wrapper;')
            else:
                lines.append(f'    return result;')

        lines.append(f'}}')
        lines.append(f'')

    def _gen_static_method_impl(self, lines: list, method: MethodDecl, cls: ClassDecl, wrapper_name: str):
        ret = _gd_type(method.return_type) if method.return_type else 'void'
        params = self._gen_param_list(method)
        lines.append(f'{ret} {wrapper_name}::{method.name}({params}) {{')

        args = [p.name for p in method.parameters]
        args_str = ', '.join(args)

        if method.return_type and method.return_type.strip() not in ('void',):
            ret_clean = method.return_type.strip().replace('const', '').replace('&', '').strip()
            if _is_value_type(ret_clean):
                lines.append(f'    auto result = {cls.name}::{method.name}({args_str});')
                lines.append(f'    auto wrapper = Ref<{_sanitize_name(ret_clean)}>::create();')
                lines.append(f'    wrapper->_native = result;')
                lines.append(f'    return wrapper;')
            else:
                lines.append(f'    return {cls.name}::{method.name}({args_str});')
        else:
            lines.append(f'    {cls.name}::{method.name}({args_str});')

        lines.append(f'}}')
        lines.append(f'')


# ---------------------------------------------------------------------------
# Module-level generation
# ---------------------------------------------------------------------------

def generate_module_code(mod: ModuleDecl, output_dir: str) -> dict[str, list[str]]:
    """Generate all wrapper code for a module. Returns {header_path: [source_paths]}."""
    gen = WrapperGenerator(output_dir, mod.name)
    source_files = gen.generate_module(mod)
    return source_files
