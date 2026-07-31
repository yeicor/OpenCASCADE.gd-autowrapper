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
    # SelectBasics_PickResult — simple struct, wrappable via VALUE_TYPE_OVERRIDES
    # Platform-specific GLX frame buffer config (pointer type)
    "Aspect_FBConfig",
    # IMeshData handle types (internal)
    "IMeshData::IEdgeHandle", "IMeshData::IFaceHandle", "IMeshData::IWireHandle",
    "IMeshData::ICurveHandle", "IMeshData::IPCurveHandle",
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
    # Select3D_BndBox3d and Graphic3d_BndBox3d — typedefs for BVH_Box<double, 3>
    # Wrapped as collection types (discovered via discover_type_aliases or VALUE_TYPE_OVERRIDES).
    # StreamBuffer — nested helper class in Message_Messenger for operator<< chaining (like std::cout)
    "StreamBuffer",
    # IMeshData collection/pointer types (internal mesh structures)
    "IMeshData::ListOfPnt2d",
    "IMeshData::VectorOfVertex",
    "IMeshData::MapOfInteger",
    "IMeshData::IFacePtr",
    "IMeshData::MapOfIEdgePtr",
    # IntPolyh array types (internal intersection data)
    "IntPolyh_ArrayOfEdges",
    "IntPolyh_ArrayOfPoints",
    "IntPolyh_ArrayOfTriangles",
    "IntPolyh_ArrayOfPointNormal",
    # BOPAlgo pointer/collection types
    "BOPAlgo_PPaveFiller",
    "BOPAlgo_PBuilder",
    "BOPDS_PDS",
    "BOPDS_PIterator",
    "BOPDS_DS",
    # Graphic3d template types
    "Graphic3d_BndBox4f",
    # Pointer types that can't be wrapped
    "BOPAlgo_PaveFiller",
    "V3d_ViewerPointer",
    "Standard_PCharacter",
    "Standard_PExtCharacter",
    "TDF_LabelNodePtr",
    "TDocStd_XLinkPtr",
    "BRepMesh_DiscretRoot",
    # Nested structs inside classes (not wrapped individually)
    "AxisAspect",
    "BehaviorOnTransform",
    "PeriodicityParams",
    "HalfSizes",
    "Limits",
    "CullingContext",
    "Aspect_XRSession::InfoString",
    # Template/internal NCollection types
    "NCollection_ForwardRangeSentinel",
    "NCollection_String",
    # Function pointer / callback types
    "CallbackOnUpdate_t",
    "Graphic3d_MediaTextureSet::CallbackOnUpdate_t",
    "MinMaxValuesCallback",
    "TPCallBackFunc",
    # System / platform-specific types
    "AVStream",
    "clocale_t",
    "WNT_HIDSpaceMouse",
    "SignalException",
    "NewDerived",
    "NCollection_DefaultHasher",
    # Skipped classes that still appear as parameter types
    "IntPolyh_Triangle",
    "IntPolyh_StartPoint",
    "IntPolyh_Couple",
    "Poly_CoherentTriPtr::Iterator",
    "Poly_CoherentTriangle",
    # IMeshTools
    "IMeshTools_Parameters",
    # Image
    "Image_VideoParams",
    # DE
    "DE_ShapeFixParameters",
    # Interface module types (module not scanned)
    "Interface_Graph",
    "Interface_EntityIterator",
    "IGESToBRep_CurveAndSurface",
    # DESTEP module types (module not scanned)
    "DESTEP_Parameters",
    # BRepSweep module types (module not scanned)
    "BRepSweep_Revol",
    "BRepSweep_Prism",
    # BRepFill module types (module not scanned)
    "BRepFill_Pipe",
    "BRepFill_Evolved",
    # BRepOffset module types (module not scanned)
    "BRepOffset_MakeOffset",
    # BRepAdaptor module types (module not scanned)
    "BRepAdaptor_Curve",
    # Extrema module types (module not scanned)
    "Extrema_ExtSS",
    "Extrema_ExtPS",
    "Extrema_ExtCS",
    "Extrema_ExtCC",
    "Extrema_ExtPC",
    # C array types
    "int[3]",
    "gp_XYZ[3]",
    "gp_XYZ[4]",
    "gp_Pnt[3]",
    "Poly_CoherentTriangle[2]",
    "Poly_CoherentTriangle *[2]",
    # TDF/TColStd internal collection types (in SKIP_CLASSES but also needed here for param use)
    "TDF_IDMap",
    "TColStd_PackedMapOfInteger",
    # OSD module types (module not scanned)
    "OSD_Path",
    # BOPAlgo nested structs
    "BOPAlgo_MakePeriodic::PeriodicityParams",
    # Deprecated NCollection handle types (templates without args — libclang mis-reports them)
    "NCollection_HArray1",
    "NCollection_HArray2",
    "NCollection_HSequence",
    # Unscanned module types that appear as handle inner types
    "Geom2dEval_RepCurveDesc",
    "GeomEval_RepCurveDesc",
    "GeomEval_RepSurfaceDesc",
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
    "Graphic3d_CubeMap",  # abstract base class; subclasses (Packed, Separate) provide concrete implementations
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
    # BRepPrim internal implementation classes (accessed via Make* wrappers)
    "BRepPrim_Wedge",
    "BRepPrim_Cone",
    "BRepPrim_Cylinder",
    "BRepPrim_Revolution",
    "BRepPrim_Sphere",
    "BRepPrim_Torus",
    # IntPolyh internal classes
    "IntPolyh_Couple",
    "IntPolyh_Triangle",
    "IntPolyh_StartPoint",
    "IntPolyh_ArrayOfTangentZones",
    "IntPolyh_ArrayOfSectionLines",
    # Platform/OS-specific
    "OSD_File",
    "OSD_Path",
    "OSD_Environment",
    "OSD_Directory",
    "OSD_Protection",
    "OSD_Signal",
    "OSD_Chronometer",
    "OSD_Thread",
    "OSD_Mutex",
    "OSD_Semaphore",
    "OSD_SharedMemory",
    # TDF internal types
    "TDF_IDMap",
    "TDF_DataSet",
    # IntPolyh: incomplete types cause compilation errors
    "IntPolyh_MaillageAffinage",
    # Internal allocator list-node types using DEFINE_NCOLLECTION_ALLOC —
    # restricted operator new makes std::unique_ptr storage impossible.
    "NCollection_ListNode",
    "Poly_CoherentTriPtr",
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
    "DynamicType", "get_type_descriptor",  # RTTI macros — libclang can't resolve return types (handle<Standard_Type>&)
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
    "EvalD1", "EvalD2", "EvalD3",  # Geom_*: return Geom_{Surface,Curve}::ResD{1,2,3} nested structs — not wrappable across FFI
}


STREAM_TYPES = {"Standard_OStream", "Standard_IStream"}


def check_type_wrappable(param_type: OCCTType, context: str,
                         wrapped_names: set[str] | None = None,
                         enum_names: set[str] | None = None,
                         for_param: bool = False) -> bool:
    """Check if a parameter type can be wrapped. Prints WARNING if not."""
    from generate.type_map import PRIMITIVE_MAP

    # Check unwrappable base types
    # For ref-to-pointer types (e.g. BRepMesh_DiscretRoot*&), base_name retains
    # the trailing " *" — also check the canonical stripped name.
    if param_type.base_name in UNWRAPPABLE_TYPES:
        print(f"  WARNING: skipping '{context}' — parameter type '{param_type.base_name}' is not wrappable",
              file=sys.stderr)
        return False
    if param_type.canonical_spelling:
        canon_clean = param_type.canonical_spelling.replace("const ", "").strip()
        canon_base = canon_clean.rstrip("&").rstrip("*").strip()
        if canon_base != param_type.base_name and canon_base in UNWRAPPABLE_TYPES:
            print(f"  WARNING: skipping '{context}' — parameter type '{param_type.spelling}' (base '{canon_base}') is not wrappable",
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
            # Mutable char* (output buffer, e.g. std::streambuf::xsgetn) can't be
            # mapped to a godot String input — only const char* is wrappable.
            # (As a return type, char* can be read as a C-string, so allow it.)
            if base == "char" and not param_type.is_const and for_param:
                print(f"  WARNING: skipping '{context}' — mutable char* param '{param_type.spelling}' is not wrappable",
                      file=sys.stderr)
                return False
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
            # Check if canonical spelling resolves to a known wrapper
            if param_type.canonical_spelling:
                canon_clean = param_type.canonical_spelling.replace("const ", "").strip()
                canon_base = canon_clean.rstrip("&").rstrip("*").strip()
                if wrapped_names is not None and canon_base in wrapped_names:
                    pass  # use existing wrapper
                else:
                    print(f"  WARNING: skipping '{context}' — non-const reference output param '{param_type.spelling}' has no wrapper",
                          file=sys.stderr)
                    return False
            else:
                print(f"  WARNING: skipping '{context}' — non-const reference output param '{param_type.spelling}' has no wrapper",
                      file=sys.stderr)
                return False

    # Skip template types (containing <>) — they're class templates we can't wrap generically
    # But NOT handle types (opencascade::handle<T>, occ::handle<T>) which are wrappable
    # Also allow if the type is already in wrapped_names (NCollection template instantiations
    # auto-registered as collection types via discover_type_aliases).
    has_template_chars = ("<" in param_type.spelling and ">" in param_type.spelling) or \
                         ("<" in param_type.base_name and ">" in param_type.base_name)
    if has_template_chars and not param_type.is_handle:
        if wrapped_names is not None and param_type.base_name in wrapped_names:
            pass  # auto-registered collection type
        elif wrapped_names is not None:
            # Fallback: check if the raw spelling (minus const/ref/ptr) is in wrapped_names.
            # Handles typedef aliases whose base_name gets canonicalized to a template type
            # (e.g. Select3D_BndBox3d → BVH_Box<double, 3>).
            raw_spelling = param_type.spelling.replace("const ", "").replace("&", "").replace("*", "").strip()
            if raw_spelling in wrapped_names:
                pass
            else:
                print(f"  WARNING: skipping '{context}' — template type '{param_type.spelling}' is not wrappable",
                      file=sys.stderr)
                return False
        else:
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
        # Check if the canonical spelling resolves to a known type
        # (handles typedefs in template class scopes like "Point" → "gp_XYZ")
        can_accept = False
        if param_type.canonical_spelling:
            from clang.cindex import TypeKind
            import clang.cindex as cl
            canon_clean = param_type.canonical_spelling.replace("const ", "").strip()
            canon_base = canon_clean.rstrip("&").rstrip("*").strip()
            # Don't accept canonical types that are raw pointers (e.g.
            # BOPAlgo_PPaveFiller → BOPAlgo_PaveFiller* — opaque pointer)
            is_canon_pointer = " *" in param_type.canonical_spelling or param_type.canonical_spelling.endswith("*")
            if not is_canon_pointer and (canon_base in wrapped_names
                    or canon_base in PRIMITIVE_MAP
                    or (enum_names is not None and canon_base in enum_names)):
                can_accept = True
        # Also check raw spelling (minus const/ref/ptr) — catches typedefs whose base_name
        # is the canonical template type (e.g. Select3D_BndBox3d → BVH_Box<double, 3>).
        if not can_accept:
            raw_clean = param_type.spelling.replace("const ", "").replace("&", "").replace("*", "").strip()
            if wrapped_names is not None and raw_clean in wrapped_names:
                can_accept = True
        if can_accept:
            return True
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
                                        wrapped_names, enum_names, for_param=True):
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
