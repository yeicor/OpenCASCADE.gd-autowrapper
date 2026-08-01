"""Synthesize real ClassDecl wrappers for NCollection template instantiations.

The opaque OcgCollectionWrappers.hpp approach wraps NCollection containers as
featureless RefCounted holders.  This module instead builds full ClassDecl
models (with element access methods) so the existing header/source generators
produce real, ClassDB-registered, GDScript-usable wrapper classes for every
NCollection instantiation discovered in method signatures.

Element types that are neither primitives, strings, scanned classes, scanned
enums, nor nested NCollection containers (e.g. Graphic3d_Attribute,
SelectMgr_BVHThreadPool::BVHThread) get a minimal synthesized value wrapper so
they can cross the FFI as element values.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from model import ClassDecl, ClassKind, MethodDecl, MethodKind, OCCTType, Parameter, occt_name_to_wrapper
from classify.overloads import dedupe_methods, group_overloads
from generate.type_map import PRIMITIVE_MAP, _vcpkg_occt_dir, COLLECTION_SPELLING_ALIASES

# Family (NCollection_ prefix stripped) -> defining header.
_CONTAINER_HEADERS = {
    "Array1": "NCollection_Array1.hxx",
    "Array2": "NCollection_Array2.hxx",
    "List": "NCollection_List.hxx",
    "Sequence": "NCollection_Sequence.hxx",
    "Map": "NCollection_Map.hxx",
    "DataMap": "NCollection_DataMap.hxx",
    "IndexedMap": "NCollection_IndexedMap.hxx",
    "IndexedDataMap": "NCollection_IndexedDataMap.hxx",
    "DynamicArray": "NCollection_DynamicArray.hxx",
    "PackedMap": "NCollection_PackedMap.hxx",
    "Vec2": "NCollection_Vec2.hxx",
    "Vec3": "NCollection_Vec3.hxx",
    "Vec4": "NCollection_Vec4.hxx",
    "Mat3": "NCollection_Mat3.hxx",
    "Mat4": "NCollection_Mat4.hxx",
}

# Family -> template argument roles: 'e' element, 'k' key, 'v' value, 'h' hasher.
_ARG_ROLES = {
    "Array1": ["e"],
    "Array2": ["e"],
    "List": ["e"],
    "Sequence": ["e"],
    "Map": ["k", "h"],
    "DataMap": ["k", "v", "h"],
    "IndexedMap": ["k", "h"],
    "IndexedDataMap": ["k", "v", "h"],
    "DynamicArray": ["e"],
    "PackedMap": ["e"],
    "Vec2": ["e"],
    "Vec3": ["e"],
    "Vec4": ["e"],
    "Mat3": ["e"],
    "Mat4": ["e"],
}

_NCOLLECTION_PREFIX = "NCollection_"
_SYNTH_MODULE = "autowrapper"


def split_template_args(s: str) -> list[str]:
    """Split a C++ template argument list on top-level commas."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def template_parts(t: str) -> tuple[str, list[str]] | None:
    """'NCollection_List<TopoDS_Shape>' -> ('NCollection_List', ['TopoDS_Shape'])."""
    t = t.strip()
    if "<" not in t or ">" not in t:
        return None
    name, rest = t.split("<", 1)
    if not rest.endswith(">"):
        return None
    return name.strip(), split_template_args(rest[:-1])


def is_ncollection_type(t: str) -> bool:
    parts = template_parts(t)
    return bool(parts) and parts[0].startswith(_NCOLLECTION_PREFIX)


# ---------------------------------------------------------------------------
# OCCTType/Parameter/MethodDecl builders
# ---------------------------------------------------------------------------

def _t_ref(base: str) -> OCCTType:
    return OCCTType(spelling="const {} &".format(base), base_name=base, is_ref=True, is_const=True)


def _t_mref(base: str) -> OCCTType:
    return OCCTType(spelling="{} &".format(base), base_name=base, is_ref=True, is_const=False)


def _t_val(base: str) -> OCCTType:
    return OCCTType(spelling=base, base_name=base)


def _t_int() -> OCCTType:
    return _t_val("int")


def _t_bool() -> OCCTType:
    return _t_val("bool")


def _p(name: str, otype: OCCTType) -> Parameter:
    return Parameter(type=otype, name=name)


def _m(name: str, params: list[Parameter], ret: OCCTType | None = None,
       const: bool = False, static: bool = False) -> MethodDecl:
    m = MethodDecl(name=name, kind=MethodKind.STATIC_METHOD if static else MethodKind.METHOD,
                   is_const=const, is_static=static)
    m.parameters = params
    m.return_type = ret
    return m


def _ctor(name: str, params: list[Parameter]) -> MethodDecl:
    m = MethodDecl(name=name, kind=MethodKind.CONSTRUCTOR)
    m.parameters = params
    return m


# ---------------------------------------------------------------------------
# Per-family method tables
# ---------------------------------------------------------------------------

def _specs_for(family: str, self_t: str, elem_t: str,
               key_t: str | None, val_t: str | None) -> list[MethodDecl]:
    """Build the method list for a container family.

    Type codes:
      'e'   element const-ref (param and return)
      'em'  mutable element ref (return) / element const-ref (param)
      'k'   key const-ref
      'v'   value const-ref
      'i'   int
      'b'   bool
      'self'  the container itself (non-const ref, clears source on Append)
      'selfc' the container itself (const ref)
      'scalar' element by value (Vec/Mat accessors)
      'sret'   the container by value (static Identity/Cross returns)
    """
    e, k, v = elem_t, key_t or elem_t, val_t or elem_t
    specs = {
        "Array1": [
            _ctor("Array1", [_p("theLower", _t_int()), _p("theUpper", _t_int())]),
            _ctor("Array1", [_p("theSize", _t_int())]),
            _m("Init", [_p("theValue", _t_ref(e))]),
            _m("Length", [], _t_int(), const=True),
            _m("IsEmpty", [], _t_bool(), const=True),
            _m("Lower", [], _t_int(), const=True),
            _m("Upper", [], _t_int(), const=True),
            _m("Value", [_p("theIndex", _t_int())], _t_ref(e), const=True),
            _m("ChangeValue", [_p("theIndex", _t_int())], _t_mref(e)),
            _m("SetValue", [_p("theIndex", _t_int()), _p("theItem", _t_ref(e))]),
            _m("Resize", [_p("theLower", _t_int()), _p("theUpper", _t_int()), _p("theToCopyData", _t_bool())]),
        ],
        "Array2": [
            _ctor("Array2", [_p("theRowLower", _t_int()), _p("theRowUpper", _t_int()),
                             _p("theColLower", _t_int()), _p("theColUpper", _t_int())]),
            _ctor("Array2", [_p("theNbRows", _t_int()), _p("theNbCols", _t_int())]),
            _m("RowLength", [], _t_int(), const=True),
            _m("ColLength", [], _t_int(), const=True),
            _m("LowerRow", [], _t_int(), const=True),
            _m("UpperRow", [], _t_int(), const=True),
            _m("LowerCol", [], _t_int(), const=True),
            _m("UpperCol", [], _t_int(), const=True),
            _m("Value", [_p("theRow", _t_int()), _p("theCol", _t_int())], _t_ref(e), const=True),
            _m("ChangeValue", [_p("theRow", _t_int()), _p("theCol", _t_int())], _t_mref(e)),
            _m("SetValue", [_p("theRow", _t_int()), _p("theCol", _t_int()), _p("theItem", _t_ref(e))]),
        ],
        "List": [
            _m("Append", [_p("theItem", _t_ref(e))]),
            _m("Append", [_p("theOther", _t_mref(self_t))]),
            _m("Prepend", [_p("theItem", _t_ref(e))]),
            _m("Prepend", [_p("theOther", _t_mref(self_t))]),
            _m("RemoveFirst", []),
            _m("First", [], _t_ref(e), const=True),
            _m("Last", [], _t_ref(e), const=True),
            _m("Extent", [], _t_int(), const=True),
            _m("IsEmpty", [], _t_bool(), const=True),
            _m("Contains", [_p("theObject", _t_ref(e))], _t_bool(), const=True),
            _m("Clear", []),
        ],
        "Sequence": [
            _m("Append", [_p("theItem", _t_ref(e))]),
            _m("Append", [_p("theOther", _t_mref(self_t))]),
            _m("Prepend", [_p("theItem", _t_ref(e))]),
            _m("Prepend", [_p("theOther", _t_mref(self_t))]),
            _m("InsertBefore", [_p("theIndex", _t_int()), _p("theItem", _t_ref(e))]),
            _m("InsertAfter", [_p("theIndex", _t_int()), _p("theItem", _t_ref(e))]),
            _m("Remove", [_p("theIndex", _t_int())]),
            _m("Value", [_p("theIndex", _t_int())], _t_ref(e), const=True),
            _m("SetValue", [_p("theIndex", _t_int()), _p("theItem", _t_ref(e))]),
            _m("First", [], _t_ref(e), const=True),
            _m("Last", [], _t_ref(e), const=True),
            _m("Length", [], _t_int(), const=True),
            _m("IsEmpty", [], _t_bool(), const=True),
            _m("Clear", []),
        ],
        "Map": [
            _m("Add", [_p("theKey", _t_ref(k))], _t_bool()),
            _m("Contains", [_p("theKey", _t_ref(k))], _t_bool(), const=True),
            _m("Remove", [_p("theKey", _t_ref(k))], _t_bool()),
            _m("Clear", [_p("theDoReleaseMemory", _t_bool())]),
            _m("Extent", [], _t_int(), const=True),
            _m("IsEmpty", [], _t_bool(), const=True),
        ],
        "DataMap": [
            _m("Bind", [_p("theKey", _t_ref(k)), _p("theItem", _t_ref(v))], _t_bool()),
            _m("TryBind", [_p("theKey", _t_ref(k)), _p("theItem", _t_ref(v))], _t_bool()),
            _m("IsBound", [_p("theKey", _t_ref(k))], _t_bool(), const=True),
            _m("UnBind", [_p("theKey", _t_ref(k))], _t_bool()),
            _m("Find", [_p("theKey", _t_ref(k))], _t_ref(v), const=True),
            _m("ChangeFind", [_p("theKey", _t_ref(k))], _t_mref(v)),
            _m("Clear", [_p("theDoReleaseMemory", _t_bool())]),
            _m("Extent", [], _t_int(), const=True),
            _m("IsEmpty", [], _t_bool(), const=True),
        ],
        "IndexedMap": [
            _m("Add", [_p("theKey", _t_ref(k))], _t_int()),
            _m("Contains", [_p("theKey", _t_ref(k))], _t_bool(), const=True),
            _m("FindIndex", [_p("theKey", _t_ref(k))], _t_int(), const=True),
            _m("FindKey", [_p("theIndex", _t_int())], _t_ref(k), const=True),
            _m("RemoveKey", [_p("theKey", _t_ref(k))], _t_bool()),
            _m("RemoveLast", []),
            _m("Clear", [_p("theDoReleaseMemory", _t_bool())]),
            _m("Extent", [], _t_int(), const=True),
            _m("IsEmpty", [], _t_bool(), const=True),
        ],
        "IndexedDataMap": [
            _m("Add", [_p("theKey", _t_ref(k)), _p("theItem", _t_ref(v))], _t_int()),
            _m("Contains", [_p("theKey", _t_ref(k))], _t_bool(), const=True),
            _m("FindIndex", [_p("theKey", _t_ref(k))], _t_int(), const=True),
            _m("FindKey", [_p("theIndex", _t_int())], _t_ref(k), const=True),
            _m("FindFromKey", [_p("theKey", _t_ref(k))], _t_ref(v), const=True),
            _m("FindFromIndex", [_p("theIndex", _t_int())], _t_ref(v), const=True),
            _m("RemoveKey", [_p("theKey", _t_ref(k))]),
            _m("RemoveLast", []),
            _m("Clear", [_p("theDoReleaseMemory", _t_bool())]),
            _m("Extent", [], _t_int(), const=True),
            _m("IsEmpty", [], _t_bool(), const=True),
        ],
        "DynamicArray": [
            _ctor("DynamicArray", [_p("theIncrement", _t_int())]),
            _m("Length", [], _t_int(), const=True),
            _m("Lower", [], _t_int(), const=True),
            _m("Upper", [], _t_int(), const=True),
            _m("IsEmpty", [], _t_bool(), const=True),
            _m("Value", [_p("theIndex", _t_int())], _t_ref(e), const=True),
            _m("ChangeValue", [_p("theIndex", _t_int())], _t_mref(e)),
            _m("SetValue", [_p("theIndex", _t_int()), _p("theItem", _t_ref(e))]),
            _m("Append", [_p("theItem", _t_ref(e))]),
            _m("Clear", [_p("theReleaseMemory", _t_bool())]),
        ],
        "PackedMap": [
            _ctor("PackedMap", []),
            _m("Add", [_p("theKey", _t_val(e))], _t_bool()),
            _m("Contains", [_p("theKey", _t_val(e))], _t_bool(), const=True),
            _m("Remove", [_p("theKey", _t_val(e))], _t_bool()),
            _m("Extent", [], _t_int(), const=True),
            _m("Length", [], _t_int(), const=True),
            _m("IsEmpty", [], _t_bool(), const=True),
            _m("Clear", []),
        ],
        "Vec2": [
            _m("SetValues", [_p("theX", _t_val(e)), _p("theY", _t_val(e))]),
            _m("x", [], _t_val(e), const=True),
            _m("y", [], _t_val(e), const=True),
            _m("Dot", [_p("theOther", _t_ref(self_t))], _t_val(e), const=True),
            _m("Modulus", [], _t_val(e), const=True),
            _m("SquareModulus", [], _t_val(e), const=True),
        ],
        "Vec3": [
            _m("SetValues", [_p("theX", _t_val(e)), _p("theY", _t_val(e)), _p("theZ", _t_val(e))]),
            _m("x", [], _t_val(e), const=True),
            _m("y", [], _t_val(e), const=True),
            _m("z", [], _t_val(e), const=True),
            _m("Dot", [_p("theOther", _t_ref(self_t))], _t_val(e), const=True),
            _m("Modulus", [], _t_val(e), const=True),
            _m("SquareModulus", [], _t_val(e), const=True),
            _m("Cross", [_p("theVec1", _t_ref(self_t)), _p("theVec2", _t_ref(self_t))],
                _t_val(self_t), static=True),
        ],
        "Vec4": [
            _m("SetValues", [_p("theX", _t_val(e)), _p("theY", _t_val(e)),
                             _p("theZ", _t_val(e)), _p("theW", _t_val(e))]),
            _m("x", [], _t_val(e), const=True),
            _m("y", [], _t_val(e), const=True),
            _m("z", [], _t_val(e), const=True),
            _m("w", [], _t_val(e), const=True),
            _m("Dot", [_p("theOther", _t_ref(self_t))], _t_val(e), const=True),
        ],
        "Mat3": [
            _m("InitIdentity", []),
            _m("ChangeValue", [_p("theRow", _t_int()), _p("theCol", _t_int())], _t_val(e)),
            _m("SetValue", [_p("theRow", _t_int()), _p("theCol", _t_int()), _p("theValue", _t_val(e))]),
            _m("Identity", [], _t_val(self_t), static=True),
        ],
        "Mat4": [
            _m("InitIdentity", []),
            _m("ChangeValue", [_p("theRow", _t_int()), _p("theCol", _t_int())], _t_val(e)),
            _m("SetValue", [_p("theRow", _t_int()), _p("theCol", _t_int()), _p("theValue", _t_val(e))]),
            _m("Identity", [], _t_val(self_t), static=True),
        ],
    }
    return specs[family]


# ---------------------------------------------------------------------------
# Element wrapper synthesis
# ---------------------------------------------------------------------------

def _find_header_for_type(t: str, inc_dir: Path | None) -> str | None:
    """Find the OCCT header that defines a (non-scanned) type."""
    if not inc_dir or not inc_dir.is_dir():
        return None
    if "::" in t:
        parent = t.split("::")[0]
        if (inc_dir / "{}.hxx".format(parent)).exists():
            return "{}.hxx".format(parent)
    if (inc_dir / "{}.hxx".format(t)).exists():
        return "{}.hxx".format(t)
    bare = t.split("::")[-1]
    pat = re.compile(r"\b(?:class|struct)\s+{}\b".format(re.escape(bare)))
    for h in sorted(inc_dir.glob("*.hxx")):
        try:
            with open(h, encoding="utf-8", errors="ignore") as f:
                head = f.read(4096)
        except OSError:
            continue
        if pat.search(head):
            return h.name
    return None


def _is_wrappable_element(elem: str, scanned_names: set[str], enum_names: set[str]) -> bool:
    if elem in PRIMITIVE_MAP:
        return True
    if elem in scanned_names or elem in enum_names:
        return True
    return is_ncollection_type(elem)


# Element types for which NCollection_List::Contains can be instantiated: it
# uses `operator==` on the element type, which nested NCollection containers
# and several OCCT value classes (Message_Msg, BOPAlgo_CheckResult, Poly_Triangle,
# …) do not define.  Element access methods are dropped for unsupported types.
_LIST_CONTAINS_COMPARABLE: frozenset[str] = frozenset({
    "Standard_Real", "Standard_Integer", "Standard_Boolean", "Standard_ShortReal",
    "Standard_Byte", "Standard_Character", "Standard_ExtendedString",
    "gp_Pnt", "gp_Pnt2d", "gp_XYZ", "gp_XY", "gp_Dir", "gp_Dir2d", "gp_Vec", "gp_Vec2d",
    "gp_Quaternion", "gp_Ax1", "gp_Ax2", "gp_Ax3", "gp_Ax2d",
    "gp_Lin", "gp_Lin2d", "gp_Plane", "gp_Circ", "gp_Circ2d", "gp_Elips", "gp_Hypr", "gp_Parab",
    "TopoDS_Shape", "TopoDS_Edge", "TopoDS_Face", "TopoDS_Vertex", "TopoDS_Wire",
    "TopoDS_Shell", "TopoDS_Solid", "TopoDS_Compound",
    "TCollection_AsciiString", "TCollection_ExtendedString",
    "Standard_GUID", "TDF_Label", "Bnd_Range", "Quantity_Color",
})


def _list_contains_supported(elem: str, enum_names: set[str]) -> bool:
    if elem in PRIMITIVE_MAP or elem in enum_names:
        return True
    return elem in _LIST_CONTAINS_COMPARABLE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def synthesize_collections(collection_types: dict[str, tuple[str, str]],
                           scanned_names: set[str],
                           enum_names: set[str],
                           inc_dir: Path | None = None) -> tuple[list[ClassDecl], set[str]]:
    """Build ClassDecl wrappers for every NCollection instantiation.

    Returns (classes, synthesized_names):
      classes           — ClassDecls for the synthesized collection wrappers and
                           any element wrappers they require.
      synthesized_names — every OCCT name that now has a real generated wrapper:
                          canonical cpp_type names, libclang-spelling/typedef
                          aliases, and synthesized element wrappers.  Used to
                          (a) exclude names from the opaque OcgCollectionWrappers.hpp
                          and (b) register them in TypeMap.
    """
    if inc_dir is None:
        inc_dir = _vcpkg_occt_dir()
    classes: list[ClassDecl] = []
    synthesized: set[str] = set()
    attempted: set[str] = set()
    element_wrappers: dict[str, ClassDecl] = {}

    def _build_container(cpp_type: str) -> ClassDecl | None:
        parts = template_parts(cpp_type)
        if not parts:
            return None
        tname, args = parts
        if not tname.startswith(_NCOLLECTION_PREFIX):
            return None
        family = tname[len(_NCOLLECTION_PREFIX):]
        if family not in _CONTAINER_HEADERS:
            return None
        roles = _ARG_ROLES[family]
        if len(args) != len(roles):
            return None
        elem = args[roles.index("e")] if "e" in roles else None
        key = args[roles.index("k")] if "k" in roles else None
        val = args[roles.index("v")] if "v" in roles else None

        for role, arg in zip(roles, args):
            if role == "h":
                continue  # hasher — internal, never marshalled
            if is_ncollection_type(arg):
                _ensure_container(arg)
            elif not _is_wrappable_element(arg, scanned_names, enum_names):
                _ensure_element_wrapper(arg)

        methods = _specs_for(family, cpp_type, elem, key, val)
        if family == "List" and not _list_contains_supported(elem, enum_names):
            methods = [m for m in methods if m.name != "Contains"]
        cls = ClassDecl(name=cpp_type, wrapper_name=occt_name_to_wrapper(cpp_type, ""),
                        module_name=_SYNTH_MODULE, kind=ClassKind.VALUE,
                        header_file=_CONTAINER_HEADERS[family],
                        has_public_default_ctor=True, has_copy_assignment=True)
        for m in methods:
            if m.kind == MethodKind.CONSTRUCTOR:
                cls.constructors.append(m)
            elif m.kind == MethodKind.STATIC_METHOD:
                cls.static_methods.append(m)
            else:
                cls.methods.append(m)
        group_overloads(cls)
        dedupe_methods(cls)
        return cls

    def _ensure_container(cpp_type: str) -> None:
        if cpp_type in attempted or not is_ncollection_type(cpp_type):
            return
        attempted.add(cpp_type)
        cls = _build_container(cpp_type)
        if cls:
            classes.append(cls)
            synthesized.add(cpp_type)

    def _ensure_element_wrapper(elem: str) -> None:
        if elem in element_wrappers or elem in scanned_names or elem in enum_names:
            return
        if elem in PRIMITIVE_MAP or is_ncollection_type(elem):
            return
        if elem in attempted:
            return
        attempted.add(elem)
        hdr = _find_header_for_type(elem, inc_dir)
        if not hdr:
            print("  WARNING: cannot synthesize element wrapper for '{}' "
                  "(header not found) — element access methods omitted".format(elem),
                  file=sys.stderr)
            return
        cls = ClassDecl(name=elem, wrapper_name=occt_name_to_wrapper(elem, ""),
                        module_name=_SYNTH_MODULE, kind=ClassKind.VALUE,
                        header_file=hdr, has_public_default_ctor=True, has_copy_assignment=True)
        element_wrappers[elem] = cls
        classes.append(cls)
        synthesized.add(elem)

    for occt_name, (cpp_type, include) in collection_types.items():
        if not is_ncollection_type(cpp_type):
            continue  # value overrides / handle types stay in the opaque header
        _ensure_container(cpp_type)
        if occt_name != cpp_type:
            base = next((c for c in classes if c.name == cpp_type), None)
            if base is None:
                continue
            if is_ncollection_type(occt_name):
                # Spelling variant of the SAME instantiation (libclang reports the
                # unqualified element, e.g. NCollection_Array1<BVHThread> for the
                # real NCollection_Array1<SelectMgr_BVHThreadPool::BVHThread>, or
                # NCollection_List<unsigned char> for NCollection_List<uint8_t>).
                # No separate class: register the spelling as a lookup alias for
                # the canonical wrapper so signature spellings resolve to it.
                COLLECTION_SPELLING_ALIASES[occt_name] = cpp_type
            else:
                # Typedef alias (e.g. TopTools_ListOfShape): a real OCCT type name
                # that resolves to the canonical container.  Give it its own wrapper
                # class (distinct ClassDecl name) so GDScript can address the typedef
                # directly without colliding with the canonical wrapper's TypeMap entry.
                alias = ClassDecl(name=occt_name, wrapper_name=occt_name_to_wrapper(occt_name, ""),
                                  module_name=_SYNTH_MODULE, kind=ClassKind.VALUE,
                                  header_file=base.header_file,
                                  has_public_default_ctor=True, has_copy_assignment=True)
                alias.constructors = [MethodDecl(name=m.name, kind=MethodKind.CONSTRUCTOR, parameters=list(m.parameters))
                                      for m in base.constructors]
                alias.methods = [MethodDecl(name=m.name, kind=MethodKind.METHOD, is_const=m.is_const,
                                            is_static=m.is_static, parameters=list(m.parameters),
                                            return_type=m.return_type)
                                 for m in base.methods]
                alias.static_methods = [MethodDecl(name=m.name, kind=MethodKind.STATIC_METHOD, is_const=m.is_const,
                                                   is_static=True, parameters=list(m.parameters),
                                                   return_type=m.return_type)
                                        for m in base.static_methods]
                group_overloads(alias)
                dedupe_methods(alias)
                classes.append(alias)
            synthesized.add(occt_name)

    return classes, synthesized
