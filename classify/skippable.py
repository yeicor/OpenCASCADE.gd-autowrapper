"""Detect unwrappable types and mark methods for skipping.

Prints a WARNING for every skipped method — NEVER silently ignores anything.
"""

from __future__ import annotations

import sys

from model import ClassDecl, MethodDecl, OCCTType

# Types that cannot be wrapped across the FFI boundary
UNWRAPPABLE_TYPES = {
    "Standard_ProgramAddress", "Standard_Address",
    "opencascade::signal_handler",
    "void*",
    # Template aliases (NCollection_Vec2<T>, etc.) — can't wrap across FFI
    "Graphic3d_Vec2i", "Graphic3d_Vec2", "Graphic3d_Vec3", "Graphic3d_Vec4",
    # BOPAlgo types
    "BOPAlgo_PaveFiller",
    # SelectBasics types
    "SelectBasics_PickResult",
    # Platform-specific GLX frame buffer config (pointer type)
    "Aspect_FBConfig",
    # IMeshData handle types (internal)
    "IMeshData::IEdgeHandle", "IMeshData::IFaceHandle",
    # BVH tree template types (BVH_Tree<double, 3> etc.) — scanner misidentifies return type
    "BVH_Tree",
    # NCollection_DefaultHasher — template class; libclang resolves it incorrectly
    "NCollection_DefaultHasher",
    # ValidatedCubeMapOrder — restricted class with private ctor; not constructible from GDScript
    "Graphic3d_ValidatedCubeMapOrder",
    # ShapeProcess types that libclang resolves to int32_t but are actually std::bitset/std::pair
    "ShapeProcess::OperationsFlags",
    "XSAlgo_ShapeProcessor::ProcessingFlags",
    # AVRational from FFmpeg — struct available only as forward declaration (incomplete type)
    "AVRational",
    # Standard_SStream — no special absorption in type_map (unlike Standard_OStream/Standard_IStream);
    # methods using it as param or return can't pass through godot-cpp FFI.
    "Standard_SStream",
}

# Handle inner type aliases: typedef'd handle names → the real wrapper class name.
# e.g. Prs3d_Presentation is typedef'd to Graphic3d_Structure, so
# handle<Prs3d_Presentation> is actually handle<Graphic3d_Structure>.
HANDLE_ALIASES: dict[str, str] = {
    "Prs3d_Presentation": "Graphic3d_Structure",
    "PrsMgr_Presentation3d": "PrsMgr_Presentation",
    "PrsMgr_PresentationManager3d": "PrsMgr_PresentationManager",
    "V3d_Light": "Graphic3d_CLight",
    "PrsMgr_PresentableObject": "PrsMgr_PresentableObject",
}

def _resolve_handle_inner(name: str) -> str:
    """Resolve handle inner type aliases to their real wrapper class names."""
    return HANDLE_ALIASES.get(name, name)

# Classes that should never be wrapped (e.g. protected inheritance issues)
SKIP_CLASSES = {
    "Message_LazyProgressScope",  # protected inheritance from Message_ProgressScope makes operator new inaccessible to unique_ptr
    "Standard_Failure",  # root of OCCT exception hierarchy — skipped explicitly
    # Template struct — can't be wrapped without concrete template arguments.
    # libclang doesn't report it as CLASS_TEMPLATE in this environment.
    "NCollection_DefaultHasher",
    # System-vcpkg interface mismatches: libclang parses system headers with
    # different signatures than vcpkg headers used for actual compilation.
    "TCollection_AsciiString",
    "TCollection_ExtendedString",
    "TCollection_HExtendedString",
    "TColStd_PackedMapOfInteger",
    "TDataStd_HLabelArray1",
    "TDF_HAttributeArray1",
}

# Methods that should always be skipped
SKIP_METHODS = {
    "InitFromJson",  # JSON streaming
    "ShallowCopy", "ShallowDump",  # Internal OCCT
    "Destroy",  # Internal/debug methods
    "Dump",  # Debug dump (may be detected incorrectly by libclang)
    "Statistics",  # May be confused with Dump by libclang
    "BVH",  # Returns BVH_Tree<double, 3> handle that scanner misidentifies as int32_t
    "operator new", "operator delete",
    "operator new[]", "operator delete[]",
    "DynamicType", "get_type_descriptor", "get_type_name",  # RTTI macros — libclang can't resolve return types
    "TransformShapeFU",  # OCCT packaging bug: symbol only exists in BRepFeat_Form, not MakeLinearForm
    "Transforms",  # System-only static method: exists in system headers but removed in vcpkg OCCT
    "Reset",  # Returns Type& on abstract REF_COUNTED classes — chaining not useful in GDScript
    "GetImage",  # Usually protected or internal; GetImage(const handle<>&) is driver-internal
    "GetStream",  # Returns Standard_OStream& — not useful across FFI
    "createNewEntity",  # Protected virtual method (AccessSpecifier.INVALID detected as public)
    "DumpExtent",  # Returns Standard_OStream& — unboundable return type
    "GetPoints",  # AIS_PointCloud: returns handle<Graphic3d_ArrayOfPoints> — libclang mis-resolves to int32_t
    "PerformCommonBlocks",  # BOPAlgo_Tools: libclang can't resolve overloaded template types → wrong param count
    "EntitySetBuilder",  # SelectMgr_ViewerSelector: returns handle<BVH_Builder<...>> — libclang mis-resolves to int32_t
    "SetAllContext",  # XSControl_WorkSession: param XSControl_WorkSessionMap (templated) mis-resolved to int32_t
}


STREAM_TYPES = {"Standard_OStream", "Standard_IStream"}


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

    # Stream types are wrappable with special handling (absorbed or mapped to String)
    if base in STREAM_TYPES:
        return True

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
        inner = _resolve_handle_inner(param_type.handle_inner)
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
                inner = _resolve_handle_inner(method.return_type.handle_inner)
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
