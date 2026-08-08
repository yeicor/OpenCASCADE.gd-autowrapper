"""OCCT install discovery, module registry, header listing, include graph."""

from __future__ import annotations

import os
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
    # --- Step 2 additions: high-value geometry/mesh/STEP/IGES/OCAF modules ---
    ("BRepExtrema", ["BRepExtrema_"]),
    ("BRepLib", ["BRepLib_"]),
    ("BRepGProp", ["BRepGProp_"]),
    ("BRepIntCurveSurface", ["BRepIntCurveSurface_"]),
    ("BRepClass", ["BRepClass_"]),
    ("BRepClass3d", ["BRepClass3d_"]),
    ("BRepTopAdaptor", ["BRepTopAdaptor_"]),
    ("BRepLProp", ["BRepLProp_"]),
    ("BRepProj", ["BRepProj_"]),
    ("BRepApprox", ["BRepApprox_"]),
    ("BRepMeshData", ["BRepMeshData_"]),
    ("BVH", ["BVH_"]),
    ("BndLib", ["BndLib_"]),
    ("IntAna", ["IntAna_"]),
    ("IntAna2d", ["IntAna2d_"]),
    ("IntCurveSurface", ["IntCurveSurface_"]),
    ("IntCurvesFace", ["IntCurvesFace_"]),
    ("IntPatch", ["IntPatch_"]),
    ("IntSurf", ["IntSurf_"]),
    ("IntWalk", ["IntWalk_"]),
    ("IntCurve", ["IntCurve_"]),
    ("Intf", ["Intf_"]),
    ("Intrv", ["Intrv_"]),
    ("GeomInt", ["GeomInt_"]),
    ("Geom2dInt", ["Geom2dInt_"]),
    ("Contap", ["Contap_"]),
    ("ChFi2d", ["ChFi2d_"]),
    ("ChFi3d", ["ChFi3d_"]),
    ("ChFiDS", ["ChFiDS_"]),
    ("FilletSurf", ["FilletSurf_"]),
    ("Bisector", ["Bisector_"]),
    ("Draft", ["Draft_"]),
    ("Font", ["Font_"]),
    ("RWGltf", ["RWGltf_"]),
    ("RWMesh", ["RWMesh_"]),
    ("RWObj", ["RWObj_"]),
    ("RWStl", ["RWStl_"]),
    ("RWPly", ["RWPly_"]),
    ("MeshVS", ["MeshVS_"]),
    ("IFSelect", ["IFSelect_"]),
    ("IFGraph", ["IFGraph_"]),
    ("IGESData", ["IGESData_"]),
    ("IGESGeom", ["IGESGeom_"]),
    ("IGESSolid", ["IGESSolid_"]),
    ("IGESBasic", ["IGESBasic_"]),
    ("IGESDraw", ["IGESDraw_"]),
    ("IGESDimen", ["IGESDimen_"]),
    ("IGESGraph", ["IGESGraph_"]),
    ("IGESAppli", ["IGESAppli_"]),
    ("IGESDefs", ["IGESDefs_"]),
    ("IGESSelect", ["IGESSelect_"]),
    ("IGESFile", ["IGESFile_"]),
    ("StepShape", ["StepShape_"]),
    ("StepGeom", ["StepGeom_"]),
    ("StepBasic", ["StepBasic_"]),
    ("StepVisual", ["StepVisual_"]),
    ("StepRepr", ["StepRepr_"]),
    ("StepData", ["StepData_"]),
    ("StepElement", ["StepElement_"]),
    ("StepDimTol", ["StepDimTol_"]),
    ("StepSelect", ["StepSelect_"]),
    ("StepFile", ["StepFile_"]),
    ("StepTidy", ["StepTidy_"]),
    ("StepToTopoDS", ["StepToTopoDS_"]),
    ("GeomToStep", ["GeomToStep_"]),
    ("TopoDSToStep", ["TopoDSToStep_"]),
    ("GeomToIGES", ["GeomToIGES_"]),
    ("Geom2dToIGES", ["Geom2dToIGES_"]),
    ("BRepToIGES", ["BRepToIGES_"]),
    ("STEPCAFControl", ["STEPCAFControl_"]),
    ("STEPConstruct", ["STEPConstruct_"]),
    ("TDataXtd", ["TDataXtd_"]),
    ("TFunction", ["TFunction_"]),
    ("TNaming", ["TNaming_"]),
    ("TPrsStd", ["TPrsStd_"]),
    ("XCAFDimTolObjects", ["XCAFDimTolObjects_"]),
    ("XCAFApp", ["XCAFApp_"]),
    ("TColgp", ["TColgp_"]),
    ("TColGeom", ["TColGeom_"]),
    ("TColGeom2d", ["TColGeom2d_"]),
    ("BinTools", ["BinTools_"]),
    ("ShapeCustom", ["ShapeCustom_"]),
    ("ShapeConstruct", ["ShapeConstruct_"]),
    ("ShapeAlgo", ["ShapeAlgo_"]),
    ("ShapeProcess", ["ShapeProcess_"]),
    ("LocOpe", ["LocOpe_"]),
    ("TopTrans", ["TopTrans_"]),
    ("LProp", ["LProp_"]),
    ("MAT", ["MAT_"]),
    ("MAT2d", ["MAT2d_"]),
    ("StdPrs", ["StdPrs_"]),
    ("StdFail", ["StdFail_"]),
    ("TransferBRep", ["TransferBRep_"]),
    ("LDOM", ["LDOM_"]),
    ("Hatch", ["Hatch_"]),
    ("HatchGen", ["HatchGen_"]),
    ("Geom2dHatch", ["Geom2dHatch_"]),
    ("BiTgte", ["BiTgte_"]),
    ("DsgPrs", ["DsgPrs_"]),
    # --- Step 3 additions: remaining data-exchange (StepAP2xx/StepFEA/StepKinematics),
    # --- OCAF persistence drivers (Bin*/Xml*), legacy topology (TopOpeBRep*, HLR*, Vrml*),
    # --- expression/numerical (Expr, Blend, FEmTool), and misc module-completeness headers.
    ("APIHeaderSection", ["APIHeaderSection_"]),
    ("AppBlend", ["AppBlend_"]),
    ("AppStd", ["AppStd_"]),
    ("AppStdL", ["AppStdL_"]),
    ("BRepBlend", ["BRepBlend_"]),
    ("BRepBndLib", ["BRepBndLib"]),
    ("BRepGraph", ["BRepGraph_"]),
    ("BRepGraphInc", ["BRepGraphInc_"]),
    ("BRepMAT2d", ["BRepMAT2d_"]),
    ("BRepPreviewAPI", ["BRepPreviewAPI_"]),
    ("BRepPrim", ["BRepPrim_"]),
    ("BRepToIGESBRep", ["BRepToIGESBRep_"]),
    ("BinDrivers", ["BinDrivers_"]),
    ("BinLDrivers", ["BinLDrivers_"]),
    ("BinMDF", ["BinMDF_"]),
    ("BinMDataStd", ["BinMDataStd_"]),
    ("BinMDataXtd", ["BinMDataXtd_"]),
    ("BinMDocStd", ["BinMDocStd_"]),
    ("BinMFunction", ["BinMFunction_"]),
    ("BinMNaming", ["BinMNaming_"]),
    ("BinMXCAFDoc", ["BinMXCAFDoc_"]),
    ("BinObjMgt", ["BinObjMgt_"]),
    ("BinTObjDrivers", ["BinTObjDrivers_"]),
    ("BinXCAFDrivers", ["BinXCAFDrivers_"]),
    ("Blend", ["Blend_"]),
    ("BlendFunc", ["BlendFunc_"]),
    ("CDF", ["CDF_"]),
    ("CDM", ["CDM_"]),
    ("ChFiKPart", ["ChFiKPart_"]),
    ("DBRep", ["DBRep_"]),
    ("DDF", ["DDF_"]),
    ("DEBREP", ["DEBREP_"]),
    ("DEGLTF", ["DEGLTF_"]),
    ("DEIGES", ["DEIGES_"]),
    ("DEOBJ", ["DEOBJ_"]),
    ("DEPLY", ["DEPLY_"]),
    ("DESTL", ["DESTL_"]),
    ("DEVRML", ["DEVRML_"]),
    ("DEXCAF", ["DEXCAF_"]),
    ("DNaming", ["DNaming_"]),
    ("Draw", ["Draw_"]),
    ("ElCLib", ["ElCLib"]),
    ("ElSLib", ["ElSLib"]),
    ("Expr", ["Expr_"]),
    ("ExprIntrp", ["ExprIntrp_"]),
    ("FEmTool", ["FEmTool_"]),
    ("FSD", ["FSD_"]),
    ("Geom2dGridEval", ["Geom2dGridEval_"]),
    ("GeomGridEval", ["GeomGridEval_"]),
    ("GeomProjLib", ["GeomProjLib"]),
    ("HLRAlgo", ["HLRAlgo_"]),
    ("HLRAppli", ["HLRAppli_"]),
    ("HLRBRep", ["HLRBRep_"]),
    ("HLRTopoBRep", ["HLRTopoBRep_"]),
    ("HeaderSection", ["HeaderSection_"]),
    ("HelixBRep", ["HelixBRep_"]),
    ("HelixGeom", ["HelixGeom_"]),
    ("Hermit", ["Hermit"]),
    ("IGESCAFControl", ["IGESCAFControl_"]),
    ("IGESConvGeom", ["IGESConvGeom_"]),
    ("IntImp", ["IntImp_"]),
    ("IntImpParGen", ["IntImpParGen_"]),
    ("IntStart", ["IntStart_"]),
    ("LDOMBasicString", ["LDOMBasicString"]),
    ("LDOMParser", ["LDOMParser"]),
    ("LDOMString", ["LDOMString"]),
    ("LocalAnalysis", ["LocalAnalysis_"]),
    ("MoniTool", ["MoniTool_"]),
    ("PCDM", ["PCDM_"]),
    ("PLib", ["PLib_"]),
    ("Plugin", ["Plugin_"]),
    ("Precision", ["Precision"]),
    ("RWHeaderSection", ["RWHeaderSection_"]),
    ("STEPEdit", ["STEPEdit_"]),
    ("STEPSelections", ["STEPSelections_"]),
    ("ShapePersistent", ["ShapePersistent_"]),
    ("ShapeProcessAPI", ["ShapeProcessAPI_"]),
    ("StdDrivers", ["StdDrivers_"]),
    ("StdLDrivers", ["StdLDrivers_"]),
    ("StdLPersistent", ["StdLPersistent_"]),
    ("StdObjMgt", ["StdObjMgt_"]),
    ("StdObject", ["StdObject_"]),
    ("StdPersistent", ["StdPersistent_"]),
    ("StdStorage", ["StdStorage_"]),
    ("StepAP203", ["StepAP203_"]),
    ("StepAP209", ["StepAP209_"]),
    ("StepAP214", ["StepAP214_"]),
    ("StepAP242", ["StepAP242_"]),
    ("StepFEA", ["StepFEA_"]),
    ("StepKinematics", ["StepKinematics_"]),
    ("StepToGeom", ["StepToGeom"]),
    ("Storage", ["Storage_"]),
    ("TObj", ["TObj_"]),
    ("TopBas", ["TopBas_"]),
    ("TopCnx", ["TopCnx_"]),
    ("TopOpeBRep", ["TopOpeBRep_"]),
    ("TopOpeBRepBuild", ["TopOpeBRepBuild_"]),
    ("TopOpeBRepDS", ["TopOpeBRepDS_"]),
    ("TopOpeBRepTool", ["TopOpeBRepTool_"]),
    ("UTL", ["UTL"]),
    ("Vrml", ["Vrml_"]),
    ("VrmlAPI", ["VrmlAPI_"]),
    ("VrmlConverter", ["VrmlConverter_"]),
    ("VrmlData", ["VrmlData_"]),
    ("XBRepMesh", ["XBRepMesh_"]),
    ("XSAlgo", ["XSAlgo_"]),
    ("XmlDrivers", ["XmlDrivers_"]),
    ("XmlLDrivers", ["XmlLDrivers_"]),
    ("XmlMDF", ["XmlMDF_"]),
    ("XmlMDataStd", ["XmlMDataStd_"]),
    ("XmlMDataXtd", ["XmlMDataXtd_"]),
    ("XmlMDocStd", ["XmlMDocStd_"]),
    ("XmlMFunction", ["XmlMFunction_"]),
    ("XmlMNaming", ["XmlMNaming_"]),
    ("XmlMXCAFDoc", ["XmlMXCAFDoc_"]),
    ("XmlObjMgt", ["XmlObjMgt_"]),
    ("XmlTObjDrivers", ["XmlTObjDrivers_"]),
    ("XmlXCAFDrivers", ["XmlXCAFDrivers_"]),
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
    """Locate the OCCT include dir, preferring the project's vcpkg install.

    Only the project's own vcpkg install (or `OCCT_INCLUDE_DIR`) is ever
    used; system OCCT installs under /usr are intentionally ignored.

    Honors the `VCPKG_DEFAULT_TRIPLET` env var (used by CI) and
    `OCCT_INCLUDE_DIR` for an explicit override.
    """
    candidates: list[tuple[Path, str]] = []
    explicit = os.environ.get("OCCT_INCLUDE_DIR")
    if explicit:
        candidates.append((Path(explicit), "explicit"))
    if project_root is not None:
        triplet = os.environ.get("VCPKG_DEFAULT_TRIPLET", "x64-linux")
        candidates.append(
            (project_root / "vcpkg" / "installed" / triplet / "include" / "opencascade", "vcpkg"))
    for include_dir, source in candidates:
        if (include_dir / "Standard_Version.hxx").exists():
            return OCCTInstall(include_dir=include_dir, source=source,
                               version=_read_version(include_dir))
    raise FileNotFoundError(
        "No OCCT install found; set OCCT_INCLUDE_DIR or install via vcpkg "
        f"(checked: {[str(d) for d, _ in candidates] or 'none'}).")

_install: OCCTInstall | None = None


def get_install(project_root: Path | None = None) -> OCCTInstall:
    global _install
    if _install is None:
        _install = find_occt_install(project_root)
    return _install


def module_headers(module: str, install: OCCTInstall) -> list[Path]:
    """All *.hxx headers in the install belonging to the given module.

    Also includes the module-aggregate header `<module>.hxx` when present
    (e.g. Standard.hxx defines `class Standard`).
    """
    prefixes = MODULE_BY_NAME.get(module)
    if prefixes is None:
        raise KeyError(f"unknown module: {module}")
    out: list[Path] = []
    for h in sorted(install.include_dir.glob("*.hxx")):
        if any(h.name.startswith(p) for p in prefixes):
            out.append(h)
    agg = install.include_dir / f"{module}.hxx"
    if agg.exists() and agg not in out:
        out.append(agg)
    return sorted(out, key=lambda h: h.name)


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
