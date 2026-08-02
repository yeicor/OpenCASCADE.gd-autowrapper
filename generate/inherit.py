"""Inheritance support for wrapper classes.

Wrappers of OCCT classes that themselves derive from other wrapped OCCT
classes inherit from the corresponding wrapper class (e.g.
OcgGeomBSplineSurface : OcgGeomBoundedSurface).  ClassDB then sees the correct
parent, so GDScript can pass a derived wrapper where a base-typed parameter is
expected (e.g. BRepLib_MakeFace::Init(const handle<Geom_Surface>&)).

Each wrapper keeps its OWN storage member typed to its own OCCT type (a handle
of a subclass is never implicitly convertible to a handle of a base class, so
a single shared member can't serve every OCCT signature).  To make base-typed
parameters work, the derived wrapper's storage is propagated into the base
wrapper's storage member after every assignment, via a generated
_sync_base_storage() method that walks the wrapper base chain.
"""

from __future__ import annotations

from model import ClassDecl, ClassKind
from generate.type_map import TypeMap


def wrapper_base(cls: ClassDecl, type_map: TypeMap) -> tuple[str, str] | None:
    """Return (wrapper_name, occt_name) of the nearest wrapped direct base, or None.

    The derived wrapper inherits from this wrapper class.  The OCCT name is the
    direct base's OCCT class name, used to produce the typed copy in
    _sync_base_storage().
    """
    for base in cls.base_classes:
        clean = base.replace("const ", "").strip()
        if type_map.class_decl(clean) is None:
            continue
        wname = type_map.wrapper_name(clean)
        if wname and wname != cls.wrapper_name:
            return (wname, clean)
    return None


def sync_eligible(cls: ClassDecl, type_map: TypeMap) -> bool:
    """True if this wrapper should emit a _sync_base_storage() method.

    Only classes whose wrapper base stores the same kind of native storage can
    be synced: REF_COUNTED into a REF_COUNTED base (_handle), and
    VALUE/TOPODS_SHAPE/OTHER into a VALUE-like base (_native).  BUILDER and
    mixed-kind chains (which do not occur in OCCT) are left alone.
    """
    wb = wrapper_base(cls, type_map)
    if not wb:
        return False
    _wbase_wname, occt_base = wb
    base_kind = type_map.class_kind(occt_base)
    if base_kind is None:
        return False
    if cls.kind == ClassKind.REF_COUNTED:
        return base_kind == ClassKind.REF_COUNTED
    base_cls = type_map.class_decl(occt_base)
    if not getattr(cls, "has_copy_assignment", True) or not getattr(base_cls, "has_copy_assignment", True):
        return False
    return base_kind in (ClassKind.VALUE, ClassKind.TOPODS_SHAPE, ClassKind.OTHER)


def topo_sort_classes(classes: list[ClassDecl],
                      occt_to_wrapper: dict[str, str]) -> list[ClassDecl]:
    """Return classes ordered base-first (bases registered before derived).

    ClassDB requires a class's parent to be registered before the class itself.
    """
    import bisect
    from collections import defaultdict

    by_wrapper = {c.wrapper_name: c for c in classes}
    deps: dict[str, set[str]] = {c.wrapper_name: set() for c in classes}
    for c in classes:
        for base in c.base_classes:
            clean = base.replace("const ", "").strip()
            w = occt_to_wrapper.get(clean)
            if w and w in by_wrapper and w != c.wrapper_name:
                deps[c.wrapper_name].add(w)

    indeg = {n: len(d) for n, d in deps.items()}
    adj: dict[str, list[str]] = defaultdict(list)
    for n, d in deps.items():
        for b in d:
            adj[b].append(n)

    queue = sorted(n for n in indeg if indeg[n] == 0)
    order: list[str] = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                bisect.insort(queue, m)

    if len(order) != len(classes):
        # Cycle (should be impossible in C++ class hierarchies) — append the
        # leftovers in their original relative order.
        seen = set(order)
        for c in classes:
            if c.wrapper_name not in seen:
                order.append(c.wrapper_name)

    rank = {n: i for i, n in enumerate(order)}
    return sorted(classes, key=lambda c: rank[c.wrapper_name])


def build_occt_to_wrapper(modules) -> dict[str, str]:
    """Map every wrapped OCCT class name to its wrapper name."""
    mapping: dict[str, str] = {}
    for mod in modules:
        for cls in mod.classes:
            mapping[cls.name] = cls.wrapper_name
    return mapping
