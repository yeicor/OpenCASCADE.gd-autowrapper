"""Per-module coverage analysis: what is wrapped and why the rest is skipped.

Replicates the generator's skip decisions *exactly* (same functions, same
order as ``generate_all``), then reports per-module wrapped/skipped counts and
a full per-symbol skip enumeration.  The skip reasons are matched against the
central policy registry (``autogen.policy``) so every exclusion is either a
deliberate, documented ``accepted`` skip or a ``gap`` that a generalization
must close.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

from . import typemap as tm
from .codegen import (build_context, group_overloads, _default_ctor,
                      _method_decl_signature, _method_skip_reason, _params_decl)
from .model import ClassDecl, MethodDecl, MethodKind, ModuleDecl
from .policy import classify_reason, symbol_exception


@dataclass
class SkipEntry:
    module: str
    target: str          # wrapper class or method name
    reason: str
    status: str = ""     # accepted | gap | unclassified (filled later)
    where: str = ""      # class name for method skips, "" for class skips
    signature: str = ""  # parameter list for method skips


@dataclass
class ModuleCoverage:
    name: str
    classes_total: int = 0
    classes_wrapped: int = 0
    classes_skipped: int = 0
    methods_total: int = 0
    methods_wrapped: int = 0
    methods_skipped: int = 0
    enums_total: int = 0
    enum_values: int = 0
    class_skip_reasons: Counter = field(default_factory=Counter)
    method_skip_reasons: Counter = field(default_factory=Counter)


def _method_label(m: MethodDecl) -> str:
    return f"{m.name}({', '.join(p.type.spelling for p in m.parameters)})"


def finalize_skips(modules: list[ModuleDecl],
                   ctx: tm.TypeContext) -> list[SkipEntry]:
    """Mark method/ctor skips the way generate_all does; collect skip entries.

    Mirrors generate_all: build_context already flagged abstract ctors; here
    default ctors are merged into the wrapper default construction and any
    signature that cannot cross the FFI is marked 'unmappable type'.
    """
    entries: list[SkipEntry] = []
    for module in modules:
        for cls in module.classes:
            if cls.skip:
                entries.append(SkipEntry(
                    module=module.name, target=cls.name, reason=cls.skip_reason,
                    status="", where=""))
                continue
            group_overloads(cls)
            for ctor in cls.constructors:
                if _default_ctor(ctor):
                    ctor.skip = True
                    ctor.skip_reason = "default constructor (native default-construction)"
                    continue
                if ctor.skip:
                    continue
                if _params_decl(ctor, ctx, cls) is None:
                    ctor.skip = True
                    ctor.skip_reason = "unmappable type"
                if ctor.skip:
                    entries.append(SkipEntry(
                        module=module.name, target=_method_label(ctor),
                        reason=ctor.skip_reason, status="", where=cls.name,
                        signature="constructor"))
            for m in cls.methods + cls.operators + cls.static_methods:
                if m.skip:
                    continue
                if _method_decl_signature(cls, m, ctx) is None:
                    m.skip = True
                    m.skip_reason = _method_skip_reason(cls, m, ctx)
                if m.skip:
                    entries.append(SkipEntry(
                        module=module.name, target=_method_label(m),
                        reason=m.skip_reason, status="", where=cls.name,
                        signature=", ".join(p.type.spelling for p in m.parameters)))
    for e in entries:
        e.status = classify_reason(e.reason, is_method=bool(e.where))
        key = f"{e.module}:{e.where or e.target}"
        if e.where:
            key += "::" + e.target.split("(")[0]
        ex = symbol_exception(key)
        if ex is not None:
            e.status = ex.status
    return entries


def compute(modules: list[ModuleDecl],
            ctx: tm.TypeContext | None = None) -> list[ModuleCoverage]:
    """Per-module coverage table (call finalize_skips first for method flags)."""
    by_name = {m.name: m for m in modules}
    table: list[ModuleCoverage] = []
    for module in modules:
        cov = ModuleCoverage(name=module.name)
        cov.classes_total = len(module.classes)
        cov.enums_total = len(module.enums)
        cov.enum_values = sum(len(e.values) for e in module.enums)
        for cls in module.classes:
            if cls.skip:
                cov.classes_skipped += 1
                cov.class_skip_reasons[cls.skip_reason] += 1
                continue
            cov.classes_wrapped += 1
            for m in cls.all_methods:
                cov.methods_total += 1
                if m.skip:
                    cov.methods_skipped += 1
                    cov.method_skip_reasons[m.skip_reason] += 1
                else:
                    cov.methods_wrapped += 1
        table.append(cov)
    return table


def _synth_failures() -> list[str]:
    """Most recent synthesis failures, if synthesize_all ran this process."""
    try:
        from .synthesize import synthesize_all
        return list(getattr(synthesize_all, "last_failures", []))
    except ImportError:
        return []


def compute_all(modules: list[ModuleDecl], missing: set[str] | None = None,
                illformed: set[str] | None = None,
                synthesized: list[ClassDecl] | None = None) -> tuple[list[ModuleCoverage],
                                                                     list[SkipEntry],
                                                                     dict]:
    """Full coverage pipeline over in-memory modules.

    `modules` must already be loaded (scan JSON); classification and synthesis
    are handled here so callers get the same ordering as generate_all.
    Returns (table, skip_entries, meta) where meta carries global totals,
    unclassified reasons and synthesis stats.
    """
    from .classify import classify_module

    global_by_name = {cls.name: cls for m in modules for cls in m.classes}
    for m in modules:
        classify_module(m, global_by_name)
    if synthesized:
        from .model import ModuleDecl as MD
        synth_mod = MD(name="NCollection", classes=synthesized)
        classify_module(synth_mod, global_by_name)
        modules.append(synth_mod)
    ctx = build_context(modules)
    if missing:
        from .audit import apply_missing
        apply_missing(modules, missing)
    if illformed:
        from .audit import apply_illformed
        apply_illformed(modules, illformed)
    entries = finalize_skips(modules, ctx)
    table = compute(modules, ctx)

    unclassified: dict[str, list[str]] = {}
    for e in entries:
        if e.status == "unclassified":
            unclassified.setdefault(e.reason, []).append(f"{e.module}:{e.where or e.target}")
    meta = {
        "modules": len(table),
        "classes_total": sum(c.classes_total for c in table),
        "classes_wrapped": sum(c.classes_wrapped for c in table),
        "classes_skipped": sum(c.classes_skipped for c in table),
        "methods_total": sum(c.methods_total for c in table),
        "methods_wrapped": sum(c.methods_wrapped for c in table),
        "methods_skipped": sum(c.methods_skipped for c in table),
        "enums_total": sum(c.enums_total for c in table),
        "synthesized": [c.wrapper_name for c in synthesized] if synthesized else [],
        "synthesis_failures": _synth_failures(),
        "unclassified_reasons": unclassified,
    }
    return table, entries, meta


def format_table(table: list[ModuleCoverage], module_filter: str | None = None) -> str:
    lines = [f"{'Module':<20}{'Cls':>5}{'Wrap':>6}{'Skip':>6} {'Meth':>6}{'Wrap':>6}{'Skip':>6} {'Enums':>6}"]
    lines.append("-" * len(lines[0]))
    for cov in sorted(table, key=lambda c: c.name):
        if module_filter and cov.name != module_filter:
            continue
        lines.append(
            f"{cov.name:<20}{cov.classes_total:>5}{cov.classes_wrapped:>6}"
            f"{cov.classes_skipped:>6} {cov.methods_total:>6}"
            f"{cov.methods_wrapped:>6}{cov.methods_skipped:>6} {cov.enums_total:>6}")
    return "\n".join(lines)


def format_module_detail(cov: ModuleCoverage) -> str:
    lines = [f"== {cov.name}: {cov.classes_wrapped}/{cov.classes_total} classes, "
             f"{cov.methods_wrapped}/{cov.methods_total} methods, "
             f"{cov.enums_total} enums"]
    if cov.class_skip_reasons:
        lines.append("  class skips:")
        for reason, n in cov.class_skip_reasons.most_common():
            lines.append(f"    {n:4d}  {reason}")
    if cov.method_skip_reasons:
        lines.append("  method skips:")
        for reason, n in cov.method_skip_reasons.most_common():
            lines.append(f"    {n:4d}  {reason}")
    return "\n".join(lines)
