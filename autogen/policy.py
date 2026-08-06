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
    "exception diagnostic method (no native storage)": SkipPolicy(
        "accepted",
        "An exception instance method with no diagnostics mapping (the "
        "standard set is what/GetMessageString/GetStackString/ExceptionType/"
        "Print). Exception wrappers carry no native object by design."),
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

    # --- Gaps: must be closed by generalizing the generator ----------------
    "abstract (pure virtual) class": SkipPolicy(
        "gap",
        "Abstract non-transient classes cannot be instantiated, so the whole "
        "class is dropped. Close by wrapping them as non-instantiable base "
        "type-tags sharing the concrete subclass's storage via a downcast "
        "_native_ref() (generalizing the TopoDS_Shape inherited-value "
        "mechanism to arbitrary hierarchies), plus dropping parameterized "
        "constructors as today."),
    "protected destructor": SkipPolicy(
        "gap",
        "Non-transient classes with a protected destructor need a generated "
        "friend deleter before unique_ptr storage can own them."),
    "custom allocation (operator new/delete)": SkipPolicy(
        "gap",
        "Classes that declare (or inherit) custom operator new/delete are "
        "dropped when they lack a public default constructor. Close by: (a) "
        "using in-place _native storage whenever the class is "
        "default-constructible, (b) generated custom deleters for unique_ptr "
        "storage, or (c) wrapping the type via handle storage when it is "
        "transient-like."),
}


# ---------------------------------------------------------------------------
# Method-level skip reasons
# ---------------------------------------------------------------------------

METHOD_SKIP_POLICIES: dict[str, SkipPolicy] = {
    "unmappable type": SkipPolicy(
        "gap",
        "The signature crosses the FFI with a type that has no wrapper "
        "mapping. Close by extending typemap (raw buffers, std::string_view, "
        "NCollection by-ref crossings, handle<>& out-params, nested "
        "typedefs) and by API-driven template synthesis."),
    "abstract class (not instantiable)": SkipPolicy(
        "gap",
        "Parameterized constructors of abstract classes are dropped because "
        "the class cannot be constructed; closes together with abstract-class "
        "support (see CLASS_SKIP_POLICIES)."),
}


def classify_reason(reason: str, is_method: bool) -> str:
    """Policy status for a skip reason: 'accepted', 'gap' or 'unclassified'."""
    table = METHOD_SKIP_POLICIES if is_method else CLASS_SKIP_POLICIES
    policy = table.get(reason)
    if policy is None:
        return "unclassified"
    return policy.status
