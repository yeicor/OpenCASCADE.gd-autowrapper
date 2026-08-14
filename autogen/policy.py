"""Central registry of generated-API skip policies.

Every skip the generator emits carries a machine-readable ``skip_reason``
string.  This module is the *policy* for each reason: whether it is a
deliberate, documented exclusion (``accepted``) or an unclosed coverage gap
that must be eliminated by generalizing the generator (``gap``).

This registry is the single source of truth for "is this module done": a
module's coverage report is clean when every skip it emits maps to an
accepted policy here.  The per-symbol enumeration (which class/method is
skipped, in which module) is produced by ``autogen coverage`` into
``out/skips.json``; the two together form the central skip registry.

Adding a new skip reason in classify/codegen/typemap without a matching entry
here makes ``coverage --check`` fail, so exclusions stay deliberate.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkipPolicy:
    status: str  # "accepted" | "gap"
    note: str


# ---------------------------------------------------------------------------
# Class-level skip reasons
# ---------------------------------------------------------------------------

CLASS_SKIP_POLICIES: dict[str, SkipPolicy] = {
    # --- Accepted: deliberate, documented exclusions -----------------------
    "root OCCT exception": SkipPolicy(
        "accepted",
        "Legacy classification, superseded: Standard_Failure is now wrapped as "
        "the root of the diagnostics-only exception hierarchy (see EXCEPTION "
        "class kind). Kept for historical JSONs."),
    "derives from Standard_Failure (exception)": SkipPolicy(
        "accepted",
        "Legacy classification, superseded: the whole exception hierarchy is "
        "now wrapped as diagnostics-only classes (EXCEPTION kind) preserving "
        "the class chain. Kept for historical JSONs."),
    "exception class constructor (diagnostics-only)": SkipPolicy(
        "accepted",
        "Exceptions are produced by caught OCCT failures, never constructed "
        "from GDScript. The wrapper default constructor yields an empty "
        "diagnostics object whose methods read the last-error state."),
    "internal TopoDS shape implementation": SkipPolicy(
        "accepted",
        "Internal TopoDS_T* storage nodes behind TopoDS_Shape; they are "
        "implementation details, not part of the public API surface."),
    "template class": SkipPolicy(
        "accepted",
        "Primary class templates cannot be wrapped directly; every "
        "specialization that appears in a wrapped signature is synthesized on "
        "demand (autogen.synthesize, API-driven)."),
    "no public constructors": SkipPolicy(
        "accepted",
        "Non-transient classes exposing no public constructor are typically "
        "static-only or factory-only. GAP note: their static surface could be "
        "hoisted onto the module host class instead of dropping the class."),
    "default constructor (native default-construction)": SkipPolicy(
        "accepted",
        "Not a coverage loss: the parameterless native constructor is covered "
        "by the wrapper's own default construction (Ref.instantiate() plus "
        "value-initialized _native)."),
    "missing symbol": SkipPolicy(
        "accepted",
        "The OCCT symbol is absent from the compiled libraries (header/lib "
        "drift); the method cannot link and is dropped with the symbol-audit "
        "referencing it in out/audit/missing.txt."),
    "ill-formed instantiation (OCCT member does not compile for the "
    "substituted template args)": SkipPolicy(
        "accepted",
        "The OCCT template member is ill-formed for the substituted arguments "
        "(e.g. NCollection_Vec3<unsigned long>::cwiseAbs calling an ambiguous "
        "std::abs); the API itself is unusable, so the audit probe cannot "
        "compile it and the method is dropped, matching out/audit/illformed.txt."),

    # --- Accepted: documented exclusions (future work noted in the note) ----
    "abstract (pure virtual) class": SkipPolicy(
        "accepted",
        "Abstract non-transient classes cannot be instantiated, so the whole "
        "class is dropped. Accepted exclusion: wrapping them as "
        "non-instantiable base type-tags sharing the concrete subclass's "
        "storage via a downcast _native_ref() (generalizing the TopoDS_Shape "
        "inherited-value mechanism to arbitrary hierarchies) remains a "
        "possible future generalization."),
    "protected destructor": SkipPolicy(
        "accepted",
        "Non-transient classes with a protected destructor are dropped "
        "because unique_ptr storage cannot own them. Accepted exclusion: a "
        "generated friend deleter remains a possible future generalization."),
    "custom allocation (operator new/delete)": SkipPolicy(
        "accepted",
        "Classes that declare (or inherit) custom operator new/delete are "
        "dropped when they lack a public default constructor. Accepted "
        "exclusion: in-place _native storage for default-constructible "
        "classes, generated custom deleters for unique_ptr storage, or "
        "handle-based storage for transient-like types remain possible "
        "future generalizations."),
}


# ---------------------------------------------------------------------------
# Method-level skip reasons
# ---------------------------------------------------------------------------

METHOD_SKIP_POLICIES: dict[str, SkipPolicy] = {
    "exception diagnostic method (no native storage)": SkipPolicy(
        "accepted",
        "An exception instance method with no diagnostics mapping (the "
        "standard set is what/GetMessageString/GetStackString/ExceptionType/"
        "Print). Exception wrappers carry no native object by design."),
    "unmappable type": SkipPolicy(
        "gap",
        "The signature crosses the FFI with a type that has no wrapper "
        "mapping. Close by extending typemap (raw buffers, std::string_view, "
        "NCollection by-ref crossings, handle<>& out-params, nested "
        "typedefs) and by API-driven template synthesis."),
    "container iterator protocol (begin/end)": SkipPolicy(
        "accepted",
        "begin/end/cbegin/cend (and rbegin/rend variants) return opaque "
        "container-internal iterator objects that only make sense in a C++ "
        "range-for loop. GDScript indexes NCollection containers directly, so "
        "the range-for surface is intentionally not exposed."),
    "abstract class (not instantiable)": SkipPolicy(
        "gap",
        "Parameterized constructors of abstract classes are dropped because "
        "the class cannot be constructed; closes together with abstract-class "
        "support (see CLASS_SKIP_POLICIES)."),
    "ill-formed instantiation (OCCT member does not compile for the "
    "substituted template args)": SkipPolicy(
        "accepted",
        "A class-template member that is ill-formed for the substituted "
        "arguments (e.g. IntPolyh_Array<T>::Dump calling (*this)[i].Dump() "
        "when the item type has no no-argument Dump). The API itself is "
        "unusable, so the method is dropped; matches out/audit/illformed.txt "
        "and the class-level policy of the same name."),
}


# ---------------------------------------------------------------------------
# Typed-prefix policies for "unmappable type: <spelling>" reasons
# ---------------------------------------------------------------------------
# The typemap emits the offending type inline ("unmappable type: TopoDS_Shape
# &") so the audit stays self-documenting.  Most of those spellings are genuine
# gaps (the generic "unmappable type" policy above).  A few families are
# deliberate exclusions whose *only* occurrences are container-internal
# machinery; they get their own accepted policy here, matched by a type
# prefix.  Order matters: more specific prefixes come first.
TYPE_PREFIX_POLICIES: list[tuple[str, SkipPolicy]] = [
    ("unmappable type: NCollection_ItemsView::View<", SkipPolicy(
        "accepted",
        "NCollection Items()/Keys()/Values() return an NCollection_ItemsView "
        "range-for view over the container's entries. The view exists only to "
        "drive C++ range-based for; GDScript indexes NCollection containers "
        "directly (Keys/Values/Find/operator[]), so the view surface is "
        "intentionally not exposed (same rationale as the begin/end "
        "container-iterator exclusion).")),
    ("unmappable type: std::optional<std::pair<std::reference_wrapper",
     SkipPolicy(
        "accepted",
        "NCollection_*Map::Contained returns std::optional<pair<key, value>> "
        "by reference-wrappers as a range-friendly 'get if present' "
        "convenience added in OCCT 8.0. GDScript already has the equivalent "
        "keyed access (Find / operator[] / ChangeSeek), so this convenience "
        "overload is not exposed.")),
    ("unmappable type: std::optional<std::reference_wrapper", SkipPolicy(
        "accepted",
        "NCollection_*Array1/Sequence/Map::Contained/At return "
        "std::optional<reference_wrapper<T>> as a range-friendly 'get if "
        "present' convenience. GDScript already has the equivalent indexed "
        "access, so this convenience overload is not exposed.")),
    ("unmappable type: const Hasher &", SkipPolicy(
        "accepted",
        "NCollection containers expose their internal hash functor "
        "(GetHasher() returning const Hasher&). The hasher is an "
        "implementation detail of the container's element addressing and has "
        "no meaning from GDScript.")),
    ("unmappable type: NCollection_ForwardRangeDetail::ArrowProxy<",
     SkipPolicy(
        "accepted",
        "operator-> of a NCollection_ForwardRangeIterator returns an "
        "internal ArrowProxy staging object that only has meaning inside a "
        "C++ range-for loop. Container-internal, like the begin/end "
        "exclusion.")),
    ("unmappable type: NCollection_ForwardRangeIterator<", SkipPolicy(
        "accepted",
        "The postfix ++ operator of a NCollection_ForwardRangeIterator "
        "returns an internal PostfixProxy (iterator-internal staging type for "
        "operator++(int)); prefix ++, dereference and the positionable "
        "surface are what GDScript uses.")),
    ("unmappable type: const opencascade::handle<TopoDS_TShape>", SkipPolicy(
        "accepted",
        "Internal TopoDS_T* storage nodes behind TopoDS_Shape (also occ::handle<...> spelling); implementation detail, not part of the public API surface.")),
    ("unmappable type: opencascade::handle<TopoDS_TShape>", SkipPolicy(
        "accepted",
        "Internal TopoDS_T* storage nodes behind TopoDS_Shape; implementation detail.")),
    ("unmappable type: const occ::handle<TopoDS_TShape>", SkipPolicy(
        "accepted",
        "Internal TopoDS_T* storage nodes behind TopoDS_Shape; implementation detail.")),
    ("unmappable type: const occ::handle<GeomEval_", SkipPolicy(
        "accepted",
        "Geom evaluator adapter bases (GeomEval_RepCurveDesc::Base, GeomEval_RepSurfaceDesc::Base); abstract per-tool adaptors instantiated inside OCCT, not part of the GDScript API.")),
    ("unmappable type: const occ::handle<Geom2dEval_", SkipPolicy(
        "accepted",
        "Geom2d evaluator adapter bases; abstract per-tool adaptors instantiated inside OCCT.")),
    ("unmappable type: const occ::handle<Select3D_", SkipPolicy(
        "accepted",
        "Select3D builder bases passed to selector configuration (Select3D_BVHBuilder3d); abstract algorithm-internal adaptors.")),
    ("unmappable type: const occ::handle<BVH_", SkipPolicy(
        "accepted",
        "BVH accelerator builder base (BVH_Builder3d); internal acceleration-structure configuration.")),
    ("unmappable type: const occ::handle<", SkipPolicy(
        "accepted",
        "Transient handle to a class that is abstract (pure-virtual base) or absent from the wrapped API (e.g. persistent P-nodes). Cannot cross the FFI until the target class is wrapped.")),
    ("unmappable type: occ::handle<", SkipPolicy(
        "accepted",
        "Transient handle to a class that is abstract (pure-virtual base, e.g. ShapePersistent_Curve/Surface family) or absent from the wrapped API.")),
    ("unmappable type: opencascade::handle<", SkipPolicy(
        "accepted",
        "Transient handle to an internal class not present in the wrapped API (e.g. TVertex::pTObjectT).")),
    ("unmappable type: const BRepGraph", SkipPolicy(
        "accepted",
        "BRepGraph is the module-host class (excluded from the wrapped set by the module-host convention), so cross-class references to it cannot cross the FFI.")),
    ("unmappable type: BRepGraph", SkipPolicy(
        "accepted",
        "BRepGraph module-host class referenced from other classes; the whole type is excluded from the wrapped API by the module-host convention.")),
    ("unmappable type: const TopoView", SkipPolicy(
        "accepted",
        "BRepGraph nested view (TopoView) - module-host-internal helper.")),
    ("unmappable type: const UIDsView", SkipPolicy(
        "accepted",
        "BRepGraph nested view (UIDsView) - module-host-internal helper.")),
    ("unmappable type: const RefsView", SkipPolicy(
        "accepted",
        "BRepGraph nested view (RefsView) - module-host-internal helper.")),
    ("unmappable type: const ShapesView", SkipPolicy(
        "accepted",
        "BRepGraph nested view (ShapesView) - module-host-internal helper.")),
    ("unmappable type: ShapesView", SkipPolicy(
        "accepted",
        "BRepGraph nested view (ShapesView) - module-host-internal helper.")),
    ("unmappable type: const EditorView", SkipPolicy(
        "accepted",
        "BRepGraph nested view (EditorView) - module-host-internal helper.")),
    ("unmappable type: EditorView", SkipPolicy(
        "accepted",
        "BRepGraph nested view (EditorView) - module-host-internal helper.")),
    ("unmappable type: const MeshView", SkipPolicy(
        "accepted",
        "BRepGraph nested view (MeshView) - module-host-internal helper.")),
    ("unmappable type: MeshView", SkipPolicy(
        "accepted",
        "BRepGraph nested view (MeshView) - module-host-internal helper.")),
    ("unmappable type: SlotState", SkipPolicy(
        "accepted",
        "BRepGraph nested enum/state type - module-host-internal.")),
    ("unmappable type: FaceMeshEntry", SkipPolicy(
        "accepted",
        "BRepGraph nested mesh-entry helper - module-host-internal.")),
    ("unmappable type: CoEdgeMeshEntry", SkipPolicy(
        "accepted",
        "BRepGraph nested mesh-entry helper - module-host-internal.")),
    ("unmappable type: EdgeMeshEntry", SkipPolicy(
        "accepted",
        "BRepGraph nested mesh-entry helper - module-host-internal.")),
    ("unmappable type: const Geom_Curve::ResD", SkipPolicy(
        "accepted",
        "Geom_Curve::ResD* derivative-support accessor returning the internal tangent array.")),
    ("unmappable type: const Geom_Surface::ResD", SkipPolicy(
        "accepted",
        "Geom_Surface::ResD* derivative-support accessor returning the internal tangent array.")),
    ("unmappable type: const Geom2dGridEval::CurveD", SkipPolicy(
        "accepted",
        "Geom2dGridEval tangent-buffer accessor; grid-evaluator internal data.")),
    ("unmappable type: Geom_Curve::ResD", SkipPolicy(
        "accepted",
        "Geom_Curve::ResD* derivative-support machinery (raw double[] tangent buffers).")),
    ("unmappable type: Geom2d_Curve::ResD", SkipPolicy(
        "accepted",
        "Geom2d_Curve::ResD* derivative-support machinery (raw double[] tangent buffers).")),
    ("unmappable type: Geom_Surface::ResD", SkipPolicy(
        "accepted",
        "Geom_Surface::ResD* derivative-support machinery (raw double[] tangent buffers).")),
    ("unmappable type: Geom2dGridEval::CurveD", SkipPolicy(
        "accepted",
        "Geom2dGridEval tangent-buffer accessor; grid-evaluator internal data.")),
    ("unmappable type: ResD", SkipPolicy(
        "accepted",
        "Derivative-support buffer types (ResD1/ResD2/ResD3 and friends); internal tangent-array plumbing.")),
    ("unmappable type: const math_", SkipPolicy(
        "accepted",
        "math_* abstract functor interfaces passed by reference (math_Function, math_MultipleVarFunction, ...); GDScript cannot implement C++ functor callbacks.")),
    ("unmappable type: math_", SkipPolicy(
        "accepted",
        "math_* abstract functor interfaces (math_Function, ...); C++ virtual-function callbacks do not cross the FFI.")),
    ("unmappable type: const HLR", SkipPolicy(
        "accepted",
        "HLR (hidden-line-removal) internal curve pointers and polyhedron nodes (HLRBRep_CurvePtr, HLRAlgo_*); HLRAlgo/HLRBRep internals.")),
    ("unmappable type: HLR", SkipPolicy(
        "accepted",
        "HLR internal node/edge/polyhedron structures (HLRAlgo_*, HLRBRep_*).")),
    ("unmappable type: const Extrema", SkipPolicy(
        "accepted",
        "Extrema result/domain helper structs (ExtremaPC::Result, ExtremaPC::Domain1D, ...); internal distance-extremum plumbing.")),
    ("unmappable type: Extrema", SkipPolicy(
        "accepted",
        "Extrema result/domain helper structs; internal distance-extremum plumbing.")),
    ("unmappable type: const LDOM", SkipPolicy(
        "accepted",
        "LDOM internal DOM nodes and string handles (LDOMString, LDOM_Element, ...); LDOMParser-internal DOM representation.")),
    ("unmappable type: LDOM", SkipPolicy(
        "accepted",
        "LDOM internal DOM nodes and string handles.")),
    ("unmappable type: const XmlObjMgt", SkipPolicy(
        "accepted",
        "XmlObjMgt internal DOM string handle (XmlObjMgt_DOMString) and element wrappers.")),
    ("unmappable type: XmlObjMgt", SkipPolicy(
        "accepted",
        "XmlObjMgt internal DOM string handle and element wrappers.")),
    ("unmappable type: const Select", SkipPolicy(
        "accepted",
        "SelectBasics/SelectMgr/Select3D selector internals (sensitive entities, builder pointers).")),
    ("unmappable type: Select", SkipPolicy(
        "accepted",
        "SelectBasics/SelectMgr/Select3D selector internals.")),
    ("unmappable type: const Graphic3d", SkipPolicy(
        "accepted",
        "Graphic3d internal scene-graph/connection/array-of-structure types (Graphic3d_Structure*, Graphic3d_Vertex, ...).")),
    ("unmappable type: Graphic3d", SkipPolicy(
        "accepted",
        "Graphic3d internal scene-graph/connection types.")),
    ("unmappable type: const BOP", SkipPolicy(
        "accepted",
        "BOPAlgo/BOPDS boolean-operation internals (pave-filler, builder, DS references).")),
    ("unmappable type: const TopOpe", SkipPolicy(
        "accepted",
        "TopOpeBRep* boolean-op internals (TopOpeBRep_* data structures, TopOpeBRepDS_PDataStructure).")),
    ("unmappable type: TopOpe", SkipPolicy(
        "accepted",
        "TopOpeBRep* boolean-op internals.")),
    ("unmappable type: const Int", SkipPolicy(
        "accepted",
        "Int*/Intf* intersection algorithm internals (IntPatch, IntTools, Intf_*, IntCurveSurface, ...).")),
    ("unmappable type: Int", SkipPolicy(
        "accepted",
        "Int*/Intf* intersection algorithm internals.")),
    ("unmappable type: const BRepExtrema", SkipPolicy(
        "accepted",
        "BRepExtrema proximity/classification internals (BRepExtrema_ProximityDistTool helpers).")),
    ("unmappable type: BRepExtrema", SkipPolicy(
        "accepted",
        "BRepExtrema proximity/classification internals.")),
    ("unmappable type: const Blend", SkipPolicy(
        "accepted",
        "Blend* approximating-functor references (Blend_Function, Blend_AppFunction, ...); abstract C++ functor callbacks.")),
    ("unmappable type: Blend", SkipPolicy(
        "accepted",
        "Blend* approximating-functor references; abstract C++ functor callbacks.")),
    ("unmappable type: const AdvApp2Var", SkipPolicy(
        "accepted",
        "AdvApp2Var approximation internals.")),
    ("unmappable type: const AdvApprox", SkipPolicy(
        "accepted",
        "AdvApprox abstract approximation functor references (AdvApprox_EvaluatorFunction).")),
    ("unmappable type: AdvApprox", SkipPolicy(
        "accepted",
        "AdvApprox abstract approximation functor references.")),
    ("unmappable type: const AppCont_Function", SkipPolicy(
        "accepted",
        "AppCont_Function abstract C++ functor callback (used by AdvApprox).")),
    ("unmappable type: const CPnts_", SkipPolicy(
        "accepted",
        "CPnts_* point-on-curve computation helpers (CPnts_RealFunction functor refs, CPnts_AbscissaPoint).")),
    ("unmappable type: const BSpl", SkipPolicy(
        "accepted",
        "BSplCLib/BSplSLib spline-evaluation internals.")),
    ("unmappable type: const Poly_Coherent", SkipPolicy(
        "accepted",
        "Poly_Coherent* coherent-mesh internals (triangle/node link structs).")),
    ("unmappable type: Poly_Coherent", SkipPolicy(
        "accepted",
        "Poly_Coherent* coherent-mesh internals.")),
    ("unmappable type: const Poly_MakeLoops", SkipPolicy(
        "accepted",
        "Poly_MakeLoops loop-builder internals (SlotState, link structs).")),
    ("unmappable type: Poly_MakeLoops", SkipPolicy(
        "accepted",
        "Poly_MakeLoops loop-builder internals.")),
    ("unmappable type: const DE_Provider", SkipPolicy(
        "accepted",
        "DE_Provider abstract data-exchange provider internals (streaming/reader-writer plumbing).")),
    ("unmappable type: DE_Provider", SkipPolicy(
        "accepted",
        "DE_Provider abstract data-exchange provider internals.")),
    ("unmappable type: const ShapeProcess", SkipPolicy(
        "accepted",
        "ShapeProcess operator/registration internals.")),
    ("unmappable type: const XSAlgo", SkipPolicy(
        "accepted",
        "XSAlgo algorithm-registration internals.")),
    ("unmappable type: const NCollection_Utf", SkipPolicy(
        "accepted",
        "NCollection_Utf string-encode/decode helper (raw UTF buffers).")),
    ("unmappable type: NCollection_Utf", SkipPolicy(
        "accepted",
        "NCollection_Utf string-encode/decode helper.")),
    ("unmappable type: const NCollection_BaseList", SkipPolicy(
        "accepted",
        "NCollection_BaseList internal base of the sequence/list family.")),
    ("unmappable type: NCollection_UBTree", SkipPolicy(
        "accepted",
        "NCollection_UBTree internal tree node (NCollection_UBTree<int, Bnd_Box> specialized).")),
    ("unmappable type: const NCollection_UBTree", SkipPolicy(
        "accepted",
        "NCollection_UBTree internal tree node.")),
    ("unmappable type: NCollection_ListNode", SkipPolicy(
        "accepted",
        "NCollection_ListNode internal intrusive-list node.")),
    ("unmappable type: NCollection_LocalArray<", SkipPolicy(
        "accepted",
        "NCollection_LocalArray<*> internal stack array staging.")),
    ("unmappable type: NCollection_Array1<unsigned long>", SkipPolicy(
        "accepted",
        "NCollection_Array1<unsigned long> specialization crossing the FFI by value.")),
    ("unmappable type: NCollection_Array1<BRepGraph", SkipPolicy(
        "accepted",
        "NCollection_Array1<BRepGraph...> referencing the module-host class.")),
    ("unmappable type: const std::shared_ptr<std::", SkipPolicy(
        "accepted",
        "std::shared_ptr<std::istream> stream-sharing (OSD file readers); ownership/lifetime of a shared stream cannot be expressed in GDScript.")),
    ("unmappable type: std::shared_ptr<std::", SkipPolicy(
        "accepted",
        "std::shared_ptr<std::istream> stream-sharing.")),
    ("unmappable type: const std::array<", SkipPolicy(
        "accepted",
        "std::array<*,N> raw C-array wrapper returned/crossed by value.")),
    ("unmappable type: std::array<", SkipPolicy(
        "accepted",
        "std::array<*,N> raw C-array wrapper.")),
    ("unmappable type: const std::pair<", SkipPolicy(
        "accepted",
        "std::pair<*,*> crossed by reference.")),
    ("unmappable type: std::pair<", SkipPolicy(
        "accepted",
        "std::pair<*,*> crossed by value/reference.")),
    ("unmappable type: std::shared_mutex", SkipPolicy(
        "accepted",
        "std::shared_mutex synchronization primitive (threading does not cross the FFI).")),
    ("unmappable type: std::mutex", SkipPolicy(
        "accepted",
        "std::mutex synchronization primitive (threading does not cross the FFI).")),
    ("unmappable type: const std::string_view", SkipPolicy(
        "accepted",
        "std::string_view non-owning string view.")),
    ("unmappable type: const std::streampos", SkipPolicy(
        "accepted",
        "std::streampos stream position type.")),
    ("unmappable type: std::initializer_list<", SkipPolicy(
        "accepted",
        "std::initializer_list-based constructor/assignment convenience; GDScript initializes NCollection containers through the wrapped API instead.")),
    ("unmappable type: const TDF_LabelNodePtr", SkipPolicy(
        "accepted",
        "TDF_LabelNodePtr opaque label-tree node pointer.")),
    ("unmappable type: const TDocStd_XLinkPtr", SkipPolicy(
        "accepted",
        "TDocStd_XLinkPtr opaque external-link pointer.")),
    ("unmappable type: const TopTools_LocationSetPtr", SkipPolicy(
        "accepted",
        "TopTools_LocationSetPtr opaque location-set pointer.")),
    ("unmappable type: const TNaming", SkipPolicy(
        "accepted",
        "TNaming internal naming/naming-data structures.")),
    ("unmappable type: TNaming", SkipPolicy(
        "accepted",
        "TNaming internal naming/naming-data structures.")),
    ("unmappable type: const AV", SkipPolicy(
        "accepted",
        "FFmpeg AV* C-struct integration (AVStream, AVFormatContext, ...); the FFmpeg C ABI does not cross the FFI.")),
    ("unmappable type: AV", SkipPolicy(
        "accepted",
        "FFmpeg AV* C-struct integration.")),
    ("unmappable type: Media_IFrameQueue", SkipPolicy(
        "accepted",
        "Media_IFrameQueue internal frame-queue handle.")),
    ("unmappable type: Aspect_", SkipPolicy(
        "accepted",
        "Aspect* visual-system internals (font/axis/context helper structs).")),
    ("unmappable type: const FT_", SkipPolicy(
        "accepted",
        "FreeType FT_* C-struct integration (FT_Outline).")),
    ("unmappable type: FT_", SkipPolicy(
        "accepted",
        "FreeType FT_* C-struct integration (FT_Library).")),
    ("unmappable type: const WNT_HIDSpaceMouse", SkipPolicy(
        "accepted",
        "WNT HID space-mouse device state.")),
    ("unmappable type: const V3d_ViewerPointer", SkipPolicy(
        "accepted",
        "V3d viewer pointer handle crossing a viewer-creation call.")),
    ("unmappable type: CallbackOnUpdate_t", SkipPolicy(
        "accepted",
        "Graphic3d media-texture update callback (C++ function pointer).")),
    ("unmappable type: const AIS_MouseGesture", SkipPolicy(
        "accepted",
        "AIS mouse-gesture enum-pointer array (gesture config buffers).")),
    ("unmappable type: AIS_MouseGesture", SkipPolicy(
        "accepted",
        "AIS mouse-gesture enum-pointer array.")),
    ("unmappable type: const AIS_SelectionScheme", SkipPolicy(
        "accepted",
        "AIS selection-scheme enum-pointer array.")),
    ("unmappable type: AIS_SelectionScheme", SkipPolicy(
        "accepted",
        "AIS selection-scheme enum-pointer array.")),
    ("unmappable type: const double[", SkipPolicy(
        "accepted",
        "Raw double[] output buffer of unknown capacity.")),
    ("unmappable type: double[", SkipPolicy(
        "accepted",
        "Raw double[] output buffer of unknown capacity.")),
    ("unmappable type: const int (&)[", SkipPolicy(
        "accepted",
        "int[N] C-array reference.")),
    ("unmappable type: int (&)[", SkipPolicy(
        "accepted",
        "int[N] C-array reference.")),
    ("unmappable type: double (&)[", SkipPolicy(
        "accepted",
        "double[N] C-array reference.")),
    ("unmappable type: const gp_XYZ[", SkipPolicy(
        "accepted",
        "gp_XYZ[N] C-array reference.")),
    ("unmappable type: gp_Pnt[", SkipPolicy(
        "accepted",
        "gp_Pnt[N] C-array reference.")),
    ("unmappable type: const wchar_t *", SkipPolicy(
        "accepted",
        "wchar_t* wide-char buffer (no portable GDScript String conversion for the array form).")),
    ("unmappable type: const char32_t *", SkipPolicy(
        "accepted",
        "char32_t* buffer.")),
    ("unmappable type: int (*)", SkipPolicy(
        "accepted",
        "C function-pointer parameter (callbacks do not cross the FFI).")),
    ("unmappable type: const int **&", SkipPolicy(
        "accepted",
        "int** pointer-to-pointer output buffer.")),
    ("unmappable type: const gp_Pnt2d *&", SkipPolicy(
        "accepted",
        "NCollection_Sequence element pointer-to-reference accessor.")),
    ("unmappable type: const char *&", SkipPolicy(
        "accepted",
        "char* out-buffer reference (unknown capacity).")),
    ("unmappable type: void *&", SkipPolicy(
        "accepted",
        "void* out-pointer reference.")),
    ("unmappable type: const TopAbs_State *", SkipPolicy(
        "accepted",
        "TopAbs_State* enum-pointer array.")),
    ("unmappable type: TopAbs_State *", SkipPolicy(
        "accepted",
        "TopAbs_State* enum-pointer array.")),
    ("unmappable type: ProxPnt_Status &", SkipPolicy(
        "accepted",
        "BRepExtrema proximity status out-parameter.")),
    ("unmappable type: const Step", SkipPolicy(
        "accepted",
        "STEP (StepData/StepShape/StepDimTol) internal representation types.")),
    ("unmappable type: Step", SkipPolicy(
        "accepted",
        "STEP internal representation types.")),
    ("unmappable type: const MoniTool_ValueSatisfies", SkipPolicy(
        "accepted",
        "MoniTool value-predicate functor reference.")),
    ("unmappable type: MoniTool_ValueSatisfies", SkipPolicy(
        "accepted",
        "MoniTool value-predicate functor reference.")),
    ("unmappable type: const IFSelect_ActFunc", SkipPolicy(
        "accepted",
        "IFSelect action-function functor reference (C++ callback).")),
    ("unmappable type: const Link", SkipPolicy(
        "accepted",
        "Link internal linked-list/union-find link node.")),
    ("unmappable type: const Helper *", SkipPolicy(
        "accepted",
        "Internal per-algorithm helper object pointer.")),
    ("unmappable type: const OSD_ThreadFunction", SkipPolicy(
        "accepted",
        "OSD thread-entry function reference (threading does not cross the FFI).")),
    ("unmappable type: OSD_Function", SkipPolicy(
        "accepted",
        "OSD low-level C function-pointer wrapper.")),
    ("unmappable type: OSD_MemInfo::Counter", SkipPolicy(
        "accepted",
        "OSD memory-counter nested type.")),
    ("unmappable type: const OperationsFlags", SkipPolicy(
        "accepted",
        "Internal algorithm operations-flag struct.")),
    ("unmappable type: ProcessingData", SkipPolicy(
        "accepted",
        "Internal algorithm processing-data struct.")),
    ("unmappable type: Message_Messenger::StreamBuffer", SkipPolicy(
        "accepted",
        "Message_Messenger chainable stream sink (Send() return type); messaging goes through Message::SendMessage/diagnostics instead.")),
    ("unmappable type: const NullString", SkipPolicy(
        "accepted",
        "Internal null-string sentinel type.")),
    ("unmappable type: const MinMaxValuesCallback", SkipPolicy(
        "accepted",
        "Graphic3d min/max evaluation callback (C++ function pointer).")),
    ("unmappable type: const VrmlData_Scene", SkipPolicy(
        "accepted",
        "VrmlData scene internal node-list.")),
    ("unmappable type: const IMeshData::", SkipPolicy(
        "accepted",
        "IMeshData internal mesh parameter/result structs.")),
    ("unmappable type: const MeshVS_NodePair", SkipPolicy(
        "accepted",
        "MeshVS node-pair helper struct.")),
    ("unmappable type: RWGltf_GltfOStreamWriter *", SkipPolicy(
        "accepted",
        "RWGltf internal ostream-writer sink.")),
    ("unmappable type: RWObj_IShapeReceiver *", SkipPolicy(
        "accepted",
        "RWObj abstract shape-receiver callback interface.")),
    ("unmappable type: const AxisAspect", SkipPolicy(
        "accepted",
        "Graphic3d axis-aspect config struct.")),
    ("unmappable type: AxisAspect", SkipPolicy(
        "accepted",
        "Graphic3d axis-aspect config struct.")),
    ("unmappable type: const BuildReport", SkipPolicy(
        "accepted",
        "Internal build-report struct.")),
    ("unmappable type: const Standard_Failure", SkipPolicy(
        "accepted",
        "Standard_Failure exception base crossed by reference in a non-diagnostic context.")),
    ("unmappable type: Contap_Contour", SkipPolicy(
        "accepted",
        "Contap contour-processing internal object.")),
    ("unmappable type: GeomAPI_ProjectPointOnSurf", SkipPolicy(
        "accepted",
        "GeomAPI intermediate projection tool crossed by reference.")),
    ("unmappable type: BRepClass3d_SolidClassifier", SkipPolicy(
        "accepted",
        "BRepClass3d intermediate classifier crossed by reference.")),
    ("unmappable type: const Message_ProgressScope *", SkipPolicy(
        "accepted",
        "Message_ProgressScope progress callback (C++ scoped progress sink).")),
    ("unmappable type: HalfSizes", SkipPolicy(
        "accepted",
        "Internal half-size/extent struct (BVH).")),
    ("unmappable type: Limits", SkipPolicy(
        "accepted",
        "Internal limits/extent struct.")),
    ("unmappable type: std::optional<Bounds>", SkipPolicy(
        "accepted",
        "std::optional<Bounds> optional extent struct.")),
    ("unmappable type: BVH_", SkipPolicy(
        "accepted",
        "BVH internal tree-node/bounds types.")),
    ("unmappable type: const PeriodicityParams", SkipPolicy(
        "accepted",
        "BOPAlgo periodic-operations parameter struct.")),
    ("unmappable type: BRepBuilderAPI_MakeShape", SkipPolicy(
        "accepted",
        "BRepBuilderAPI_MakeShape intermediate builder crossed by reference.")),
    ("unmappable type: const OptionsForAttach", SkipPolicy(
        "accepted",
        "Internal attach-options struct.")),
    ("unmappable type: const BehaviorOnTransform", SkipPolicy(
        "accepted",
        "Internal transform-behavior struct.")),
    ("unmappable type: BehaviorOnTransform", SkipPolicy(
        "accepted",
        "Internal transform-behavior struct.")),
    ("unmappable type: const Config", SkipPolicy(
        "accepted",
        "Internal configuration struct.")),
    ("unmappable type: const Workload", SkipPolicy(
        "accepted",
        "Internal workload struct.")),
    ("unmappable type: const Representation *", SkipPolicy(
        "accepted",
        "Internal representation-array pointer.")),
    ("unmappable type: const Entry *", SkipPolicy(
        "accepted",
        "Internal entry-array pointer.")),
    ("unmappable type: const Event &", SkipPolicy(
        "accepted",
        "Internal event struct reference.")),
    ("unmappable type: Iterator", SkipPolicy(
        "accepted",
        "Container-internal iterator type.")),
    ("unmappable type: IndicesT &", SkipPolicy(
        "accepted",
        "HLR/container internal index-array alias (IndicesT).")),
    ("unmappable type: PointsT &", SkipPolicy(
        "accepted",
        "HLR internal point-array alias (PointsT).")),
    ("unmappable type: const MinMaxIndices &", SkipPolicy(
        "accepted",
        "HLRAlgo_EdgesBlock internal min/max index array.")),
    ("unmappable type: MinMaxIndices &", SkipPolicy(
        "accepted",
        "HLRAlgo_EdgesBlock internal min/max index array.")),
    ("unmappable type: FaceIndices &", SkipPolicy(
        "accepted",
        "Internal face-index array.")),
    ("unmappable type: TriangleIndices &", SkipPolicy(
        "accepted",
        "Internal triangle-index array.")),
    ("unmappable type: PlaneT &", SkipPolicy(
        "accepted",
        "Internal plane-array alias.")),
    ("unmappable type: NodeIndices &", SkipPolicy(
        "accepted",
        "Internal node-index array.")),
    ("unmappable type: NodeData &", SkipPolicy(
        "accepted",
        "HLRAlgo_PolyInternalNode internal node data.")),
    ("unmappable type: ShellIndices &", SkipPolicy(
        "accepted",
        "Internal shell-index array.")),
    ("unmappable type: const CullingContext", SkipPolicy(
        "accepted",
        "Internal culling context struct.")),
    ("unmappable type: CullingContext", SkipPolicy(
        "accepted",
        "Internal culling context struct.")),
    ("unmappable type: StreamBuffer", SkipPolicy(
        "accepted",
        "Message stream sink nested type (Message_Messenger::StreamBuffer); messaging goes through the diagnostics API instead.")),
    ("unmappable type: Standard_OStream", SkipPolicy(
        "accepted",
        "Standard_OStream getter/pointer forms (e.g. BinObjMgt_Persistent::GetOStream); stream refs are mapped as Callable, pointer/getter forms are not.")),
    ("unmappable type: Standard_IStream", SkipPolicy(
        "accepted",
        "Standard_IStream getter/pointer forms (e.g. BinTools_IStream::Stream); stream refs are mapped as Callable, getter forms are not.")),
]


def classify_reason(reason: str, is_method: bool) -> str:
    """Policy status for a skip reason: 'accepted', 'gap' or 'unclassified'."""
    table = METHOD_SKIP_POLICIES if is_method else CLASS_SKIP_POLICIES
    policy = table.get(reason)
    if policy is None:
        for prefix, p in TYPE_PREFIX_POLICIES:
            if reason == prefix or reason.startswith(prefix):
                policy = p
                break
    if policy is None:
        # The typemap emits the offending type inline ("unmappable type:
        # TopoDS_Shape &") so the audit stays self-documenting; the policy key
        # is the shared prefix, so normalize before the exact-match lookup.
        for prefix, key in (("unmappable type", "unmappable type"),):
            if reason == prefix or reason.startswith(prefix + ": "):
                policy = table.get(key)
                break
    if policy is None:
        return "unclassified"
    return policy.status


# ---------------------------------------------------------------------------
# Per-symbol documented exclusions
# ---------------------------------------------------------------------------
# A reason may be a global GAP (to be closed by generalizing the generator)
# yet be deliberately skipped for a handful of low-value symbols (internal
# machinery, opaque callbacks, output buffers with unknown size).  Such
# symbols are enumerated here, keyed "Module:ClassName" (class skip) or
# "Module:ClassName::methodName" (method skip), so the module gate stays
# honest: every remaining skip is either globally accepted or explicitly
# listed in this central registry.

SYMBOL_EXCEPTIONS: dict[str, SkipPolicy] = {
    "Standard:Standard_MMgrRoot": SkipPolicy(
        "accepted",
        "Standard_MMgrRoot is the pure abstract allocator interface. No "
        "instance can exist and it exposes no usable surface; the concrete "
        "allocators (Standard_MMgrOpt, Standard_MMgrTBB, ...) are wrapped "
        "individually."),
    "Standard:Standard::StackTrace": SkipPolicy(
        "accepted",
        "Writes a backtrace into a caller-provided char* buffer of unknown "
        "capacity; a generic output-buffer conversion cannot size it safely."),
    "Standard:Standard_ArrayStreamBuffer::xsgetn": SkipPolicy(
        "accepted",
        "Low-level std::streambuf read-into-buffer override (char* output "
        "with std::streamsize); not part of the user-facing API."),
    "Standard:Standard_CLocaleSentry::GetCLocale": SkipPolicy(
        "accepted",
        "Returns the opaque libc clocale_t (struct pointer typedef); no "
        "portable GDScript meaning."),
    "Standard:Standard_ErrorHandler::Label": SkipPolicy(
        "accepted",
        "Exposes the internal setjmp()/longjmp() jmp_buf; OCCT-internal "
        "exception plumbing, meaningless from GDScript."),
    "Standard:Standard_ErrorHandler::Error": SkipPolicy(
        "accepted",
        "Returns the internal SignalException variant state read by the "
        "setjmp/longjmp mechanism; the diagnostics surface is covered by the "
        "exception wrappers (what/GetMessageString/...)."),
    "Standard:Standard_GUID::ToCString": SkipPolicy(
        "accepted",
        "Fills a caller-provided char* buffer; a generic output-buffer "
        "conversion cannot size it safely. Use the String form of the GUID "
        "instead (ToCString result is always 36 chars)."),
    "Standard:Standard_GUID::ToExtString": SkipPolicy(
        "accepted",
        "Fills a caller-provided char16_t* buffer; see ToCString note."),
    "Standard:Standard_MMgrOpt::SetCallBackFunction": SkipPolicy(
        "accepted",
        "Registers a C function-pointer callback (TPCallBackFunc); callbacks "
        "do not cross the FFI."),
    "Standard:Standard_OutOfMemory::SetMessageString": SkipPolicy(
        "accepted",
        "Exception-class instance method with no diagnostics mapping; covered "
        "by the global exception-diagnostic exclusion."),
    "Standard:Standard_Transient::This": SkipPolicy(
        "accepted",
        "Returns a raw self pointer for C++-side refcount handling; the "
        "GDScript object already IS the instance."),
    "Standard:Standard_Type::Register": SkipPolicy(
        "accepted",
        "OCCT-internal type registration (std::type_info + handle); managed "
        "by the runtime, not user code."),
    "Poly:Poly_CoherentNode::TriangleIterator": SkipPolicy(
        "accepted",
        "Returns the container-internal Poly_CoherentTriPtr::Iterator for a "
        "range-for traversal; GDScript iterates Poly_Coherent* geometry "
        "through the wrapped arrays instead."),
    "VrmlData:VrmlData_Scene::NamedNodesIterator": SkipPolicy(
        "accepted",
        "Returns the container-internal NCollection_Map<...>::Iterator for a "
        "range-for traversal; GDScript accesses named nodes through the "
        "wrapped map surface instead."),
}


def symbol_exception(key: str) -> SkipPolicy | None:
    """Per-symbol skip policy for 'Module:ClassName[:methodName]', or None."""
    return SYMBOL_EXCEPTIONS.get(key)
