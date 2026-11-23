"""Triangle meshes: read binary or ASCII STL and the first object of a 3MF, check them, write binary STL.

Every reader refuses oversized input before parsing it, drops degenerate facets, and reports (never
fixes) a mesh that is not watertight. Units are millimeters throughout.
"""

from __future__ import annotations

import re
import struct
import zipfile
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from xml.etree import ElementTree

import numpy as np

from ..errors import ConfigError

MAX_BYTES = 200 * 1024 * 1024      # refuse files (or a 3MF's model part, uncompressed) larger than this
MAX_TRIANGLES = 5_000_000          # and meshes with more triangles than this
DEGENERATE_AREA_MM2 = 1e-9         # facets smaller than this have no usable normal
WELD_MM = 1e-4                     # vertices closer than this are the same vertex for edge matching
SMALL_PART_MM = 2.0                # a part this small was probably drawn in meters or inches
LARGE_PART_MM = 2000.0             # and this large in microns or a wrong unit

STL_FACET = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
THREEMF_UNITS = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8,
                 "meter": 1000.0}
CORE_NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"


class MeshError(ConfigError):
    code = "mesh"


@dataclass
class Mesh:
    tris: np.ndarray                     # (n, 3, 3) float64, mm
    name: str = ""
    kind: str = ""                       # stl-binary, stl-ascii or 3mf
    warnings: list[str] = field(default_factory=list)
    removed_degenerate: int = 0

    @cached_property
    def cross(self) -> np.ndarray:
        t = self.tris
        return np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])

    @cached_property
    def areas(self) -> np.ndarray:
        return 0.5 * np.linalg.norm(self.cross, axis=1)

    @cached_property
    def normals(self) -> np.ndarray:
        n = np.linalg.norm(self.cross, axis=1)
        return self.cross / np.where(n > 0, n, 1.0)[:, None]

    @cached_property
    def surface_area(self) -> float:
        return float(self.areas.sum())

    @cached_property
    def volume(self) -> float:
        """Enclosed volume from signed tetrahedra (exact for a closed mesh, positive if outward facing)."""
        t = self.tris
        return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)

    @cached_property
    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        pts = self.tris.reshape(-1, 3)
        return pts.min(axis=0), pts.max(axis=0)

    @property
    def size(self) -> np.ndarray:
        lo, hi = self.bbox
        return hi - lo

    @cached_property
    def faces(self) -> np.ndarray:
        """(n, 3) vertex ids after welding vertices closer than WELD_MM."""
        q = np.round(self.tris.reshape(-1, 3) / WELD_MM).astype(np.int64)
        _, inv = np.unique(q, axis=0, return_inverse=True)
        return inv.reshape(-1, 3)

    @cached_property
    def edges(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Unique undirected edges as (a, b) pairs, the facet count per edge, and per facet-edge its edge index."""
        f = self.faces
        e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
        e.sort(axis=1)
        uniq, inv, counts = np.unique(e, axis=0, return_inverse=True, return_counts=True)
        return uniq, counts, inv.reshape(3, -1).T

    def watertight(self) -> dict:
        """Every edge shared by exactly two facets. Reported, not fixed."""
        _, counts, _ = self.edges
        open_edges = int((counts == 1).sum())
        nonmanifold = int((counts > 2).sum())
        return {"watertight": open_edges == 0 and nonmanifold == 0, "open_edges": open_edges,
                "nonmanifold_edges": nonmanifold, "edges": int(len(counts))}

    def transformed(self, rot: np.ndarray, center_on: tuple[float, float] | None = None) -> Mesh:
        """Rotated by rot (3x3), dropped to z = 0, and centered on the (x, y) point if given."""
        t = self.tris @ np.asarray(rot, float).T
        pts = t.reshape(-1, 3)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        shift = np.array([0.0, 0.0, -lo[2]])
        if center_on is not None:
            shift[:2] = np.asarray(center_on, float) - (lo[:2] + hi[:2]) / 2
        return Mesh(t + shift, self.name, self.kind, list(self.warnings), self.removed_degenerate)

    def facts(self) -> dict:
        lo, hi = self.bbox
        return {"triangles": int(len(self.tris)), "size_mm": [round(float(v), 3) for v in hi - lo],
                "volume_mm3": round(abs(self.volume), 3), "surface_mm2": round(self.surface_area, 3),
                "removed_degenerate": self.removed_degenerate, **self.watertight()}


# ---------------------------------------------------------------- reading

def load(path: str | Path) -> Mesh:
    p = Path(path).expanduser()
    if not p.is_file():
        raise MeshError(f"no file at {p}")
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise MeshError(f"{p.name} is {size / 1e6:.0f} MB, over Gantrel's {MAX_BYTES // (1024 * 1024)} MB limit",
                        "Simplify the mesh (fewer triangles) in the CAD program and export it again")
    ext = p.suffix.lower()
    if ext == ".3mf":
        mesh = read_3mf(p)
    elif ext == ".stl":
        mesh = read_stl(p.read_bytes())
    else:
        raise MeshError(f"{p.name} is not an STL or 3MF file", "Export the part as .stl or .3mf")
    mesh.name = p.name
    return mesh


def read_stl(data: bytes) -> Mesh:
    if len(data) > MAX_BYTES:
        raise MeshError(f"the STL is over Gantrel's {MAX_BYTES // (1024 * 1024)} MB limit")
    if len(data) >= 84:
        (n,) = struct.unpack_from("<I", data, 80)
        if len(data) == 84 + 50 * n:
            if n > MAX_TRIANGLES:
                raise MeshError(f"the STL has {n} triangles, over Gantrel's limit of {MAX_TRIANGLES}",
                                "Simplify the mesh and export it again")
            if n == 0:
                raise MeshError("the STL has no triangles")
            rec = np.frombuffer(data, dtype=STL_FACET, count=n, offset=84)
            return _finish(rec["v"].astype(np.float64), "stl-binary")
    head = data[:1024].lstrip().lower()
    if head.startswith(b"solid"):
        return _read_ascii(data)
    raise MeshError("this is not a valid STL file: it is not ASCII STL, and as binary STL its size does not match "
                    "its triangle count", "Export the part again as STL")


_VERTEX = re.compile(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)")


def _read_ascii(data: bytes) -> Mesh:
    facets = data.count(b"endfacet")
    if facets > MAX_TRIANGLES:
        raise MeshError(f"the STL has {facets} triangles, over Gantrel's limit of {MAX_TRIANGLES}",
                        "Simplify the mesh and export it again")
    found = _VERTEX.findall(data)
    if not found:
        raise MeshError("this ASCII STL has no vertices", "Export the part again as STL")
    if len(found) % 3 or len(found) // 3 != facets or data.count(b"endloop") != facets:
        raise MeshError("this ASCII STL is malformed: every facet needs exactly three vertices",
                        "Export the part again as STL")
    try:
        v = np.array(found, dtype=np.float64)
    except ValueError:
        raise MeshError("this ASCII STL has a vertex that is not a number", "Export the part again as STL") from None
    return _finish(v.reshape(-1, 3, 3), "stl-ascii")


def read_3mf(path: Path) -> Mesh:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise MeshError(f"{path.name} is not a valid 3MF (not a zip archive)", "Export the part again as 3MF") from None
    with zf:
        name = _model_part(zf)
        info = zf.getinfo(name)
        if info.file_size > MAX_BYTES:
            raise MeshError(f"the 3MF model is {info.file_size / 1e6:.0f} MB uncompressed, over Gantrel's limit",
                            "Simplify the mesh and export it again")
        raw = zf.read(name)
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as err:
        raise MeshError(f"the 3MF model XML is malformed ({err})", "Export the part again as 3MF") from None
    scale = THREEMF_UNITS.get(root.get("unit", "millimeter"))
    if scale is None:
        raise MeshError(f"the 3MF uses an unknown unit {root.get('unit')!r}")
    warnings = []
    for obj in root.iter(f"{CORE_NS}object"):
        mesh = obj.find(f"{CORE_NS}mesh")
        if mesh is None:
            continue
        verts = mesh.find(f"{CORE_NS}vertices")
        tris = mesh.find(f"{CORE_NS}triangles")
        if verts is None or tris is None:
            continue
        try:
            v = np.array([[float(e.get("x")), float(e.get("y")), float(e.get("z"))]
                          for e in verts.iter(f"{CORE_NS}vertex")], dtype=np.float64)
            idx = np.array([[int(e.get("v1")), int(e.get("v2")), int(e.get("v3"))]
                            for e in tris.iter(f"{CORE_NS}triangle")], dtype=np.int64)
        except (TypeError, ValueError):
            raise MeshError("the 3MF has a vertex or triangle with a missing or non-numeric value") from None
        if len(idx) > MAX_TRIANGLES:
            raise MeshError(f"the 3MF object has {len(idx)} triangles, over Gantrel's limit of {MAX_TRIANGLES}")
        if len(idx) == 0 or len(v) == 0:
            continue
        if idx.min() < 0 or idx.max() >= len(v):
            raise MeshError("the 3MF has a triangle that points at a vertex that does not exist")
        n_obj = sum(1 for o in root.iter(f"{CORE_NS}object") if o.find(f"{CORE_NS}mesh") is not None)
        if n_obj > 1:
            warnings.append(f"the 3MF has {n_obj} mesh objects; only the first ({obj.get('name') or obj.get('id')}) "
                            "is planned")
        if any(item.get("transform") for item in root.iter(f"{CORE_NS}item")):
            warnings.append("the 3MF build has a placement transform; Gantrel reads the object as drawn and "
                            "chooses its own orientation")
        m = _finish(v[idx] * scale, "3mf")
        m.warnings[:0] = warnings
        return m
    raise MeshError("the 3MF has no object with a mesh", "Export the part again as 3MF")


def _model_part(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    if "_rels/.rels" in names:
        try:
            rels = ElementTree.fromstring(zf.read("_rels/.rels"))
            for r in rels:
                if r.get("Type", "").endswith("/3dmodel") and r.get("Target"):
                    target = r.get("Target").lstrip("/")
                    if target in names:
                        return target
        except ElementTree.ParseError:
            pass
    models = [n for n in names if n.lower().endswith(".model") and n.startswith("3D/")]
    if not models:
        raise MeshError("the 3MF has no 3D model part", "Export the part again as 3MF")
    return models[0]


def _finish(tris: np.ndarray, kind: str) -> Mesh:
    if not np.isfinite(tris).all():
        raise MeshError("the mesh has a vertex that is not a finite number", "Export the part again")
    m = Mesh(tris, kind=kind)
    keep = m.areas > DEGENERATE_AREA_MM2
    removed = int((~keep).sum())
    if removed:
        m = Mesh(tris[keep], kind=kind)
        m.removed_degenerate = removed
        m.warnings.append(f"removed {removed} degenerate facet(s) with no area")
    if len(m.tris) == 0:
        raise MeshError("the mesh has no facets with any area")
    biggest = float(m.size.max())
    if biggest < SMALL_PART_MM:
        m.warnings.append(f"the part is only {biggest:.3g} mm across: it was probably drawn in meters or inches. "
                          "Gantrel reads mesh files as millimeters; scale it before printing")
    elif biggest > LARGE_PART_MM:
        m.warnings.append(f"the part is {biggest:.0f} mm across: it was probably drawn in microns or another unit. "
                          "Gantrel reads mesh files as millimeters; scale it before printing")
    wt = m.watertight()
    if not wt["watertight"]:
        m.warnings.append(f"the mesh is not watertight ({wt['open_edges']} open and {wt['nonmanifold_edges']} "
                          "non-manifold edges): volume, weight and supports are less reliable. Repair it in the "
                          "CAD program or a mesh repair tool")
    return m


# ---------------------------------------------------------------- writing

def stl_bytes(tris: np.ndarray, header: str = "gantrel-printer plan") -> bytes:
    tris = np.asarray(tris, dtype=np.float64)
    rec = np.zeros(len(tris), dtype=STL_FACET)
    cross = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    n = np.linalg.norm(cross, axis=1)
    rec["normal"] = cross / np.where(n > 0, n, 1.0)[:, None]
    rec["v"] = tris
    return header.encode()[:80].ljust(80, b" ") + struct.pack("<I", len(tris)) + rec.tobytes()


def ascii_stl(tris: np.ndarray, name: str = "part") -> str:
    out = [f"solid {name}"]
    for t in np.asarray(tris, dtype=np.float64):
        n = np.cross(t[1] - t[0], t[2] - t[0])
        n = n / (np.linalg.norm(n) or 1.0)
        out.append(f"  facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}")
        out.append("    outer loop")
        out += [f"      vertex {v[0]:.6e} {v[1]:.6e} {v[2]:.6e}" for v in t]
        out.append("    endloop")
        out.append("  endfacet")
    out.append(f"endsolid {name}")
    return "\n".join(out) + "\n"


def write_stl(path: str | Path, mesh: Mesh) -> Path:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(stl_bytes(mesh.tris))
    return p
