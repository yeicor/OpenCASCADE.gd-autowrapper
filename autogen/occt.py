"""OCCT install discovery, module registry, header listing, include graph."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Module definitions: (module_name, header_prefixes).  Order is irrelevant;
# modules are topologically sorted from the include DAG when a full build runs.
OCCT_MODULES: list[tuple[str, list[str]]] = [
    ("Standard", ["Standard_"]),
    ("TCollection", ["TCollection_"]),
    ("TColStd", ["TColStd_"]),
    ("NCollection", ["NCollection_"]),
    ("TopTools", ["TopTools_"]),
    ("gp", ["gp_"]),
    ("GeomAbs", ["GeomAbs_"]),
    ("TopAbs", ["TopAbs_"]),
    ("Geom", ["Geom_"]),
    ("Geom2d", ["Geom2d_"]),
    ("Bnd", ["Bnd_"]),
    ("TopLoc", ["TopLoc_"]),
    ("TopoDS", ["TopoDS_"]),
    ("BRep", ["BRep_"]),
    ("TopExp", ["TopExp_"]),
    ("BRepTools", ["BRepTools_"]),
    ("TDF", ["TDF_"]),
    ("Message", ["Message_"]),
    ("IntRes2d", ["IntRes2d_"]),
    ("Image", ["Image_"]),
    ("Poly", ["Poly_"]),
    ("Adaptor3d", ["Adaptor3d_"]),
    ("Adaptor2d", ["Adaptor2d_"]),
    ("ShapeBuild", ["ShapeBuild_"]),
    ("ShapeExtend", ["ShapeExtend_"]),
    ("Quantity", ["Quantity_"]),
    ("PrsMgr", ["PrsMgr_"]),
    ("Prs3d", ["Prs3d_"]),
    ("Graphic3d", ["Graphic3d_"]),
    ("V3d", ["V3d_"]),
    ("SelectMgr", ["SelectMgr_"]),
    ("BOPAlgo", ["BOPAlgo_"]),
    ("AIS", ["AIS_"]),
    ("BRepBuilderAPI", ["BRepBuilderAPI_"]),
    ("BRepPrimAPI", ["BRepPrimAPI_"]),
    ("BRepAlgoAPI", ["BRepAlgoAPI_"]),
    ("BRepAlgo", ["BRepAlgo_"]),
    ("IntTools", ["IntTools_"]),
    ("BRepMesh", ["BRepMesh_"]),
    ("StlAPI", ["StlAPI_"]),
    ("STEPControl", ["STEPControl_"]),
    ("IGESControl", ["IGESControl_"]),
    ("XCAFDoc", ["XCAFDoc_"]),
    ("XCAFPrs", ["XCAFPrs_"]),
    ("TDocStd", ["TDocStd_"]),
    ("gce", ["gce_"]),
    ("GC", ["GC_"]),
    ("BRepFilletAPI", ["BRepFilletAPI_"]),
    ("BRepOffsetAPI", ["BRepOffsetAPI_"]),
    ("BRepFeat", ["BRepFeat_"]),
    ("ShapeFix", ["ShapeFix_"]),
    ("ShapeAnalysis", ["ShapeAnalysis_"]),
    ("ShapeUpgrade", ["ShapeUpgrade_"]),
    ("BRepCheck", ["BRepCheck_"]),
    ("GeomAPI", ["GeomAPI_"]),
    ("IntPolyh", ["IntPolyh_"]),
    ("Law", ["Law_"]),
    ("Aspect", ["Aspect_"]),
    ("StdSelect", ["StdSelect_"]),
    ("TDataStd", ["TDataStd_"]),
    ("IMeshTools", ["IMeshTools_"]),
    ("IMeshData", ["IMeshData_"]),
    ("XCAFView", ["XCAFView_"]),
    ("XCAFNoteObjects", ["XCAFNoteObjects_"]),
    ("Media", ["Media_"]),
    ("PrsDim", ["PrsDim_"]),
    ("Transfer", ["Transfer_"]),
    ("XSControl", ["XSControl_"]),
    ("Select3D", ["Select3D_"]),
    ("Extrema", ["Extrema_"]),
    ("GeomAdaptor", ["GeomAdaptor_"]),
    ("Geom2dAdaptor", ["Geom2dAdaptor_"]),
    ("BRepAdaptor", ["BRepAdaptor_"]),
    ("BRepSweep", ["BRepSweep_"]),
    ("Sweep", ["Sweep_"]),
    ("BRepFill", ["BRepFill_"]),
    ("BRepOffset", ["BRepOffset_"]),
    ("Interface", ["Interface_"]),
    ("IGESToBRep", ["IGESToBRep_"]),
    ("BOPDS", ["BOPDS_"]),
    ("BOPTools", ["BOPTools_"]),
    ("SelectBasics", ["SelectBasics_"]),
    ("DESTEP", ["DESTEP_"]),
    ("DE", ["DE_"]),
    ("math", ["math_"]),
    ("MathInteg", ["MathInteg_"]),
    ("MathLin", ["MathLin_"]),
    ("MathOpt", ["MathOpt_"]),
    ("MathPoly", ["MathPoly_"]),
    ("MathRoot", ["MathRoot_"]),
    ("MathSys", ["MathSys_"]),
    ("MathUtils", ["MathUtils_"]),
    ("AdvApprox", ["AdvApprox_"]),
    ("AdvApp2Var", ["AdvApp2Var_"]),
    ("AppParCurves", ["AppParCurves_"]),
    ("AppCont", ["AppCont_"]),
    ("AppDef", ["AppDef_"]),
    ("Approx", ["Approx_"]),
    ("ApproxInt", ["ApproxInt_"]),
    ("FairCurve", ["FairCurve_"]),
    ("BSplCLib", ["BSplCLib_"]),
    ("BSplSLib", ["BSplSLib_"]),
    ("Convert", ["Convert_"]),
    ("CSLib", ["CSLib_"]),
    ("CPnts", ["CPnts_"]),
    ("Plate", ["Plate_"]),
    ("NLPlate", ["NLPlate_"]),
    ("GeomPlate", ["GeomPlate_"]),
    ("GCPnts", ["GCPnts_"]),
    ("GeomConvert", ["GeomConvert_"]),
    ("Geom2dConvert", ["Geom2dConvert_"]),
    ("GeomLib", ["GeomLib_"]),
    ("GeomLProp", ["GeomLProp_"]),
    ("GeomFill", ["GeomFill_"]),
    ("GeomTools", ["GeomTools_"]),
    ("Geom2dAPI", ["Geom2dAPI_"]),
    ("Geom2dEval", ["Geom2dEval_"]),
    ("Geom2dGcc", ["Geom2dGcc_"]),
    ("GeomBndLib", ["GeomBndLib_"]),
    ("GeomEval", ["GeomEval_"]),
    ("Geom2dHash", ["Geom2dHash_"]),
    ("GeomHash", ["GeomHash_"]),
    ("ProjLib", ["ProjLib_"]),
    ("GProp", ["GProp_"]),
    ("ExtremaPC", ["ExtremaPC_"]),
    ("GccAna", ["GccAna_"]),
    ("GccEnt", ["GccEnt_"]),
    ("GccInt", ["GccInt_"]),
    ("GCE2d", ["GCE2d_"]),
    ("OSD", ["OSD_"]),
    ("TShort", ["TShort_"]),
    ("Units", ["Units_"]),
    ("UnitsAPI", ["UnitsAPI_"]),
    ("UnitsMethods", ["UnitsMethods_"]),
    ("Resource", ["Resource_"]),
]

MODULE_BY_NAME = {name: prefixes for name, prefixes in OCCT_MODULES}


@dataclass
class OCCTInstall:
    """A resolved OCCT install (vcpkg or system)."""
    include_dir: Path
    source: str  # "vcpkg" | "system"
    version: str = ""

    def header(self, name: str) -> Path:
        return self.include_dir / name


def _read_version(include_dir: Path) -> str:
    vh = include_dir / "Standard_Version.hxx"
    if not vh.exists():
        return ""
    try:
        text = vh.read_text(errors="replace")
    except OSError:
        return ""
    major = re.search(r"#define\s+OCC_VERSION_MAJOR\s+(\d+)", text)
    minor = re.search(r"#define\s+OCC_VERSION_MINOR\s+(\d+)", text)
    maint = re.search(r"#define\s+OCC_VERSION_MAINTENANCE\s+(\d+)", text)
    if major and minor:
        return f"{major.group(1)}.{minor.group(1)}.{maint.group(1) if maint else '0'}"
    return ""


def find_occt_install(project_root: Path | None = None) -> OCCTInstall:
    """Locate the OCCT include dir, preferring the project's vcpkg install."""
    candidates: list[tuple[Path, str]] = []
    if project_root is not None:
        candidates.append(
            (project_root / "vcpkg" / "installed" / "x64-linux" / "include" / "opencascade", "vcpkg"))
    candidates.append((Path("/usr/include/opencascade"), "system"))
    candidates.append((Path("/usr/local/include/opencascade"), "system"))
    for include_dir, source in candidates:
        if (include_dir / "Standard_Version.hxx").exists():
            return OCCTInstall(include_dir=include_dir, source=source,
                               version=_read_version(include_dir))
    raise FileNotFoundError(
        "No OCCT install found; set OCCT_INCLUDE_DIR or install via vcpkg.")

_install: OCCTInstall | None = None


def get_install(project_root: Path | None = None) -> OCCTInstall:
    global _install
    if _install is None:
        _install = find_occt_install(project_root)
    return _install


def module_headers(module: str, install: OCCTInstall) -> list[Path]:
    """All *.hxx headers in the install belonging to the given module."""
    prefixes = MODULE_BY_NAME.get(module)
    if prefixes is None:
        raise KeyError(f"unknown module: {module}")
    out: list[Path] = []
    for h in sorted(install.include_dir.glob("*.hxx")):
        if any(h.name.startswith(p) for p in prefixes):
            out.append(h)
    return out


def module_for_header(header_name: str) -> str | None:
    """Map an OCCT header basename to its module name."""
    for name, prefixes in OCCT_MODULES:
        if any(header_name.startswith(p) for p in prefixes):
            return name
    return None


# ---------------------------------------------------------------------------
# Include graph
# ---------------------------------------------------------------------------

_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')


def _direct_includes(header: Path) -> list[str]:
    """Directly-#included OCCT header basenames from a header file."""
    try:
        text = header.read_text(errors="replace")
    except OSError:
        return []
    names: list[str] = []
    for line in text.splitlines():
        m = _INCLUDE_RE.match(line)
        if m:
            names.append(m.group(1))
    return names


def include_closure(headers: list[Path], install: OCCTInstall,
                    include_self: bool = True) -> list[Path]:
    """BFS closure over OCCT #includes, topologically (deps first)."""
    by_name = {h.name: h for h in install.include_dir.glob("*.hxx")}
    order: list[Path] = []
    seen: set[str] = set()
    queue: list[Path] = list(headers)
    while queue:
        h = queue.pop(0)
        if h.name in seen:
            continue
        seen.add(h.name)
        for dep in _direct_includes(h):
            dep_path = by_name.get(dep)
            if dep_path is not None:
                order.append(dep_path)
                queue.append(dep_path)
        if include_self:
            order.append(h)
    # de-dup preserving order
    result: list[Path] = []
    for h in order:
        if h.name not in {r.name for r in result}:
            result.append(h)
    return result


def transitive_closure_for_header(header: Path, install: OCCTInstall) -> list[Path]:
    """Ordered OCCT headers to pre-include before `header` for self-containment."""
    return include_closure([header], install, include_self=False)
