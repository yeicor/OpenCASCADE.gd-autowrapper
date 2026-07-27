"""Detect unwrappable types and mark methods for skipping.

Prints a WARNING for every skipped method — NEVER silently ignores anything.
"""

from __future__ import annotations

import sys

from model import ClassDecl, MethodDecl, OCCTType

# Types that cannot be wrapped across the FFI boundary
UNWRAPPABLE_TYPES = {
    "Standard_OStream", "Standard_IStream", "Standard_SStream",
    "Standard_ProgramAddress",
    "opencascade::signal_handler",
}

# Methods that should always be skipped
SKIP_METHODS = {
    "DumpJson", "InitFromJson",  # JSON streaming
    "ShallowCopy", "ShallowDump",  # Internal OCCT
    "Dump", "Destroy",  # Internal/debug methods
    "operator new", "operator delete",
    "operator new[]", "operator delete[]",
    "DynamicType", "get_type_descriptor",  # RTTI macros — libclang can't resolve return types
    "TransformShapeFU",  # OCCT packaging bug: symbol only exists in BRepFeat_Form, not MakeLinearForm
    "Transforms",  # System-only static method: exists in system headers but removed in vcpkg OCCT
}


def check_type_wrappable(param_type: OCCTType, context: str,
                         wrapped_names: set[str] | None = None,
                         enum_names: set[str] | None = None) -> bool:
    """Check if a parameter type can be wrapped. Prints WARNING if not."""
    from generate.type_map import PRIMITIVE_MAP

    # Check unwrappable base types
    if param_type.base_name in UNWRAPPABLE_TYPES:
        print(f"  WARNING: skipping '{context}' — parameter type '{param_type.base_name}' is not wrappable",
              file=sys.stderr)
        return False

    base = param_type.base_name

    # Enum types are always wrappable (mapped to int32_t with static_cast)
    is_enum = (enum_names is not None and base in enum_names)

    # Check raw pointer types (not handles) — exception: const char* returns as String
    if param_type.is_pointer and not param_type.is_handle:
        # Allow char* / const char* for return types (converted to String)
        if base in ("char", "Standard_CString") or base == "char":
            pass  # handled by the type map
        else:
            print(f"  WARNING: skipping '{context}' — raw pointer type '{param_type.spelling}' is not wrappable",
                  file=sys.stderr)
            return False

    # Non-const reference output parameters: wrappable if the base type is a
    # wrapped class, or a primitive that has a wrapper class
    if param_type.is_ref and not param_type.is_const and not param_type.is_handle:
        if is_enum:
            # Non-const ref of enum: needs enum output wrapper (not yet implemented)
            print(f"  WARNING: skipping '{context}' — non-const ref enum output param '{param_type.spelling}' needs wrapper",
                  file=sys.stderr)
            return False
        elif base in PRIMITIVE_MAP:
            # Check that a primitive wrapper class exists for this type
            from generate.type_map import _PRIMITIVE_WRAPPER_MAP
            if base not in _PRIMITIVE_WRAPPER_MAP:
                print(f"  WARNING: skipping '{context}' — primitive type '{base}' has no output wrapper class",
                      file=sys.stderr)
                return False
            pass  # primitives get Ocg* wrapper classes
        elif wrapped_names is not None and base in wrapped_names:
            pass  # wrapped classes use existing wrapper
        else:
            print(f"  WARNING: skipping '{context}' — non-const reference output param '{param_type.spelling}' has no wrapper",
                  file=sys.stderr)
            return False

    # Skip template types (containing <>) — they're class templates we can't wrap generically
    # But NOT handle types (opencascade::handle<T>) which are wrappable
    if ("<" in param_type.spelling and ">" in param_type.spelling
            and not param_type.is_handle):
        print(f"  WARNING: skipping '{context}' — template type '{param_type.spelling}' is not wrappable",
              file=sys.stderr)
        return False

    # Handle types with unwrapped inner type cannot be passed across FFI
    if param_type.is_handle:
        inner = param_type.handle_inner
        # Skip handle types with unresolvable inner types (contains <> or is 'int')
        if "<" in inner or ">" in inner:
            print(f"  WARNING: skipping '{context}' — handle inner type '{inner}' is unresolvable template",
                  file=sys.stderr)
            return False
        if inner == "int":
            print(f"  WARNING: skipping '{context}' — handle inner type is 'int' (unresolved by libclang)",
                  file=sys.stderr)
            return False
        if wrapped_names is not None and inner not in wrapped_names:
            print(f"  WARNING: skipping '{context}' — handle inner type '{inner}' has no wrapper",
                  file=sys.stderr)
            return False

    # Skip non-primitive, non-handle, non-wrapped, non-enum OCCT types
    if (wrapped_names is not None
            and base not in PRIMITIVE_MAP
            and not param_type.is_handle
            and not is_enum
            and base not in wrapped_names):
        print(f"  WARNING: skipping '{context}' — OCCT type '{base}' has no wrapper",
              file=sys.stderr)
        return False

    return True


def mark_skippable_methods(cls: ClassDecl, wrapped_names: set[str] | None = None,
                           enum_names: set[str] | None = None) -> None:
    """Mark methods that cannot be wrapped and print warnings.

    Sets method.skip = True for each un-wrappable method.
    """
    for method in cls.all_methods:
        context = f"{cls.name}::{method.name}"

        # Skip always-unwrappable methods
        if method.name in SKIP_METHODS:
            method.skip = True
            method.skip_reason = f"method '{method.name}' is not wrappable"
            print(f"  WARNING: skipping {context} — {method.skip_reason}", file=sys.stderr)
            continue

        # Skip deleted methods
        if method.is_deleted:
            method.skip = True
            method.skip_reason = "deleted method"
            continue

        # Skip pure virtual methods
        if method.is_pure_virtual:
            method.skip = True
            method.skip_reason = "pure virtual method"
            print(f"  WARNING: skipping {context} — pure virtual", file=sys.stderr)
            continue

        # Check return type
        if method.return_type and not method.return_type.is_void:
            if not check_type_wrappable(method.return_type, f"{context} (return type)",
                                        wrapped_names, enum_names):
                method.skip = True
                method.skip_reason = f"unwrappable return type '{method.return_type.base_name}'"
                continue

            # Also check handle return types for unresolvable inner types
            if method.return_type.is_handle:
                inner = method.return_type.handle_inner
                if "<" in inner or ">" in inner or inner == "int":
                    method.skip = True
                    method.skip_reason = f"unresolvable handle return type '{inner}'"
                    print(f"  WARNING: skipping {context} — {method.skip_reason}", file=sys.stderr)
                    continue

        # Check parameter types
        skip = False
        for param in method.parameters:
            if not check_type_wrappable(param.type, f"{context} (param '{param.name}')",
                                        wrapped_names, enum_names):
                method.skip = True
                method.skip_reason = f"unwrappable parameter type '{param.type.base_name}'"
                skip = True
                break

        if skip:
            continue

        # Skip methods with function pointer parameters
        for param in method.parameters:
            if "(" in param.type.spelling and ("(*)" in param.type.spelling or "()" in param.type.spelling):
                method.skip = True
                method.skip_reason = f"function pointer parameter '{param.type.spelling}'"
                print(f"  WARNING: skipping {context} — {method.skip_reason}", file=sys.stderr)
                break
