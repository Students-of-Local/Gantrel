"""Triangle meshes: read binary or ASCII STL and the first build object of a 3MF, check them, write binary STL.

Every reader refuses oversized input before parsing it, drops degenerate facets, and reports (never
fixes) a mesh that is not watertight. A watertight mesh whose facets all face inward is turned right way
out, with a warning. Units are millimeters throughout.
"""

from __future__ import annotations

import math
import posixpath
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
PLAN_MAX_TRIANGLES = 2_000_000     # the planner and check --model take parts up to this many triangles
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
    # A turned copy points here at the mesh it was turned from, and takes its welding and edges from it.
    topology: Mesh | None = field(default=None, repr=False, compare=False)

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
        return bounds(self.tris)

    @property
    def size(self) -> np.ndarray:
        lo, hi = self.bbox
        return hi - lo

    @cached_property
    def vertices(self) -> tuple[np.ndarray, np.ndarray]:
        """The distinct corner points (exactly equal coordinates merged, no welding) and, per facet, the
        index of each corner among them. Turning these instead of every corner of every facet is several
        times less work, with the same numbers."""
        pts = self.tris.reshape(-1, 3)
        order = np.lexsort((pts[:, 2], pts[:, 1], pts[:, 0]))
        s = pts[order]
        new = np.ones(len(s), bool)
        new[1:] = (s[1:] != s[:-1]).any(axis=1)
        ids = np.empty(len(pts), np.int64)
        ids[order] = np.cumsum(new) - 1
        return s[new], ids.reshape(-1, 3)

    @cached_property
    def faces(self) -> np.ndarray:
        """(n, 3) vertex ids after welding vertices closer than WELD_MM, numbered in sorted (x, y, z) order."""
        if self.topology is not None:
            return self.topology.faces
        q = np.round(self.tris.reshape(-1, 3) / WELD_MM).astype(np.int64)
        order = np.lexsort((q[:, 2], q[:, 1], q[:, 0]))
        s = q[order]
        new = np.ones(len(s), bool)
        new[1:] = (s[1:] != s[:-1]).any(axis=1)
        ids = np.empty(len(q), np.int64)
        ids[order] = np.cumsum(new) - 1
        return ids.reshape(-1, 3)

    @cached_property
    def edges(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Unique undirected edges as (a, b) pairs, the facet count per edge, and per facet-edge its edge index."""
        if self.topology is not None:
            return self.topology.edges
        f = self.faces
        e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
        e.sort(axis=1)
        n = int(f.max()) + 1 if len(f) else 1
        key, inv, counts = np.unique(e[:, 0] * n + e[:, 1], return_inverse=True, return_counts=True)
        uniq = np.column_stack([key // n, key % n])
        return uniq, counts, inv.reshape(3, -1).T

    @cached_property
    def edge_owners(self) -> tuple[np.ndarray, np.ndarray]:
        """The facets on each edge: owners[start[e]:start[e + 1]] are edge e's facets, in facet order."""
        if self.topology is not None:
            return self.topology.edge_owners
        _, counts, fe = self.edges
        slot_edge = fe.ravel()
        order = np.argsort(slot_edge, kind="stable")
        start = np.zeros(len(counts) + 1, np.int64)
        np.cumsum(counts, out=start[1:])
        return order // 3, start

    def watertight(self) -> dict:
        """Every edge shared by exactly two facets. Reported, not fixed."""
        _, counts, _ = self.edges
        open_edges = int((counts == 1).sum())
        nonmanifold = int((counts > 2).sum())
        return {"watertight": open_edges == 0 and nonmanifold == 0, "open_edges": open_edges,
                "nonmanifold_edges": nonmanifold, "edges": int(len(counts))}

    def transformed(self, rot: np.ndarray, center_on: tuple[float, float] | None = None) -> Mesh:
        """Rotated by rot (3x3), dropped to z = 0, and centered on the (x, y) point if given."""
        rot = np.asarray(rot, float)
        return self.placed(turn(self.tris, rot), rot, center_on)

    def placed(self, t: np.ndarray, rot: np.ndarray, center_on: tuple[float, float] | None = None,
               lo_hi: tuple[np.ndarray, np.ndarray] | None = None) -> Mesh:
        """This mesh with its triangles already turned by rot (t = tris @ rot.T): dropped to z = 0, centered
        on center_on if given. t is shifted in place and kept. The turned copy takes its welding and edges from
        this mesh (a rigid turn keeps them, and they are worked out once), shares its facet areas, and turns its
        normals by rot rather than recomputing them. lo_hi is bounds(t) when the caller has it already."""
        lo, hi = lo_hi if lo_hi is not None else bounds(t)
        shift = placement(lo, hi, center_on)
        t += shift
        m = Mesh(t, self.name, self.kind, list(self.warnings), self.removed_degenerate,
                 topology=self.topology or self)
        for key in ("areas", "surface_area"):
            if key in self.__dict__:
                m.__dict__[key] = self.__dict__[key]
        if abs(np.linalg.det(rot) - 1.0) < 1e-9:   # a proper rotation: normals turn with the part
            m.__dict__["normals"] = self.normals @ rot.T
        m.__dict__["bbox"] = (lo + shift, hi + shift)
        return m

    def simplified(self, target: int) -> Mesh:
        """A copy with about target triangles, for scoring orientations on a big part (vertex clustering).

        The distinct vertices are snapped to a grid of cubes and each cube's vertices merge into their mean;
        facets whose corners fall in fewer than three cubes go. The cube size starts from the surface area
        and grows until the copy has at most 1.25 x target facets. Same part in, same copy out. Flat faces
        stay flat; sharp edges are rounded by about one cube.
        """
        v, vid = self.vertices
        lo, _ = self.bbox
        # A surface cut by cubes of side h crosses about area / h^2 of them, with about two facets each.
        h = math.sqrt(2.0 * max(self.surface_area, 1e-9) / max(target, 1))
        for _ in range(8):
            q = np.floor((v - lo) / h).astype(np.int64)
            dims = q.max(axis=0) + 1
            cell, inv = np.unique((q[:, 0] * dims[1] + q[:, 1]) * dims[2] + q[:, 2], return_inverse=True)
            f = inv[vid]
            keep = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
            if keep.sum() <= 1.25 * target:
                break
            h *= math.sqrt(keep.sum() / target)
        count = np.bincount(inv, minlength=len(cell))
        mean = np.column_stack([np.bincount(inv, weights=v[:, k], minlength=len(cell)) for k in range(3)])
        mean /= count[:, None]
        m = Mesh(mean[f[keep]], self.name, self.kind)
        return Mesh(m.tris[m.areas > DEGENERATE_AREA_MM2], self.name, self.kind)

    def facts(self) -> dict:
        lo, hi = self.bbox
        return {"triangles": int(len(self.tris)), "size_mm": [round(float(v), 3) for v in hi - lo],
                "volume_mm3": round(abs(self.volume), 3), "surface_mm2": round(self.surface_area, 3),
                "removed_degenerate": self.removed_degenerate, **self.watertight()}


def fits_planner(m: "Mesh") -> None:
    """Refuse a part with more triangles than the planner handles in reasonable time and memory."""
    n = int(len(m.tris))
    if n > PLAN_MAX_TRIANGLES:
        raise MeshError(f"the part has {n} triangles; the planner takes up to {PLAN_MAX_TRIANGLES}",
                        f"Simplify the mesh to under {PLAN_MAX_TRIANGLES} triangles (decimate it in the CAD program "
                        "or slicer) and plan again; slicing the original still works")


def turn(tris: np.ndarray, rot: np.ndarray) -> np.ndarray:
    """tris @ rot.T as one (3n x 3) product, which is several times faster than the batched form."""
    return (tris.reshape(-1, 3) @ np.asarray(rot, float).T).reshape(tris.shape)


def placement(lo: np.ndarray, hi: np.ndarray, center_on: tuple[float, float] | None = None) -> np.ndarray:
    """The shift that drops a part with these bounds to z = 0 and centers it on (x, y) if given."""
    shift = np.array([0.0, 0.0, -lo[2]])
    if center_on is not None:
        shift[:2] = np.asarray(center_on, float) - (lo[:2] + hi[:2]) / 2
    return shift


def bounds(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The (x, y, z) minimum and maximum over every vertex, one coordinate at a time (fast on big meshes)."""
    lo = np.array([tris[..., k].min() for k in range(3)])
    hi = np.array([tris[..., k].max() for k in range(3)])
    return lo, hi


def corner_z(tris: np.ndarray, fn=np.minimum) -> np.ndarray:
    """Each facet's lowest (fn=np.minimum) or highest (np.maximum) z."""
    return fn(fn(tris[:, 0, 2], tris[:, 1, 2]), tris[:, 2, 2])


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
    """The first build item of a 3MF, with every component it is made of, in millimeters.

    Bambu Studio and OrcaSlicer keep each mesh in its own part (3D/Objects/object_N.model) and give the
    root model's objects only <components> that point at it (3MF Production extension, p:path). Components
    are followed with their transforms, inside the archive only, with cycles, depth, total triangles and
    total uncompressed size capped. The build item's own transform is applied too, so a part scaled in the
    slicer keeps its size; Gantrel then chooses the orientation itself.
    """
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise MeshError(f"{path.name} is not a valid 3MF (not a zip archive)", "Export the part again as 3MF") from None
    with zf:
        reader = _ThreeMF(zf)
        root_name = _model_part(zf)
        root = reader.model(root_name)
        scale = THREEMF_UNITS.get(root.get("unit", "millimeter"))
        if scale is None:
            raise MeshError(f"the 3MF uses an unknown unit {root.get('unit')!r}")
        warnings = []
        build = root.find(f"{CORE_NS}build")
        items = [] if build is None else build.findall(f"{CORE_NS}item")
        if items:
            item = items[0]
            if len(items) > 1:
                warnings.append(f"the 3MF build has {len(items)} objects; only the first ({_label(reader, root_name, item)}) "
                                "is planned")
            ref = (reader.resolve(item.get(PATH_ATTR), root_name), item.get("objectid"))
            top = _transform(item.get("transform"))
        else:   # no build: the first object that has a mesh or components
            objs = [o for o in root.iter(f"{CORE_NS}object")
                    if o.find(f"{CORE_NS}mesh") is not None or o.find(f"{CORE_NS}components") is not None]
            if not objs:
                raise MeshError("the 3MF has no object with a mesh", "Export the part again as 3MF")
            if len(objs) > 1:
                warnings.append(f"the 3MF has {len(objs)} objects and no build; only the first "
                                f"({objs[0].get('name') or objs[0].get('id')}) is planned")
            ref, top = (root_name, objs[0].get("id")), IDENTITY
        tris = reader.triangles(ref, top)
    if not tris:
        raise MeshError("the 3MF has no object with a mesh", "Export the part again as 3MF")
    m = _finish(np.concatenate(tris) * scale, "3mf")
    m.warnings[:0] = warnings
    return m


PROD_NS = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
PATH_ATTR = f"{PROD_NS}path"
MAX_COMPONENT_DEPTH = 16            # components inside components, deeper than any slicer writes
MAX_COMPONENTS = 100_000            # component references followed in one file
IDENTITY = (np.eye(3), np.zeros(3))


def _transform(text: str | None) -> tuple[np.ndarray, np.ndarray]:
    """A 3MF transform "m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32": a point p maps to p @ A + t."""
    if not text:
        return IDENTITY
    try:
        v = np.array([float(x) for x in text.split()], dtype=np.float64)
    except ValueError:
        raise MeshError(f"the 3MF has a transform that is not numbers ({text[:60]!r})") from None
    if v.shape != (12,) or not np.isfinite(v).all():
        raise MeshError("the 3MF has a transform without 12 finite numbers", "Export the part again as 3MF")
    return v[:9].reshape(3, 3), v[9:]


def _label(reader: _ThreeMF, part: str, item) -> str:
    try:
        obj = reader.objects(reader.resolve(item.get(PATH_ATTR), part)).get(item.get("objectid"))
    except MeshError:
        obj = None
    return (obj.get("name") if obj is not None else None) or str(item.get("objectid"))


class _ThreeMF:
    """Model parts of one archive, read once each, with the caps every part shares."""

    def __init__(self, zf: zipfile.ZipFile):
        self.zf = zf
        self.names = set(zf.namelist())
        self.parts: dict[str, ElementTree.Element] = {}
        self.index: dict[str, dict[str, ElementTree.Element]] = {}
        self.meshes: dict[tuple[str, str], np.ndarray] = {}
        self.bytes_read = 0
        self.triangle_count = 0
        self.components = 0

    def resolve(self, ref: str | None, current: str) -> str:
        """A p:path inside the archive. Anything that leaves it, or names a part that is not there, is refused."""
        if not ref:
            return current
        if "\\" in ref or ":" in ref:
            raise MeshError(f"the 3MF points at a part outside the archive ({ref!r})", "Export the part again as 3MF")
        name = posixpath.normpath(ref.lstrip("/"))
        if name.startswith("../") or name == ".." or name.startswith("/") or name not in self.names:
            raise MeshError(f"the 3MF points at a model part that is not in it ({ref!r})",
                            "Export the part again as 3MF")
        return name

    def model(self, name: str) -> ElementTree.Element:
        if name in self.parts:
            return self.parts[name]
        info = self.zf.getinfo(name)
        if self.bytes_read + info.file_size > MAX_BYTES:
            raise MeshError(f"the 3MF model is {(self.bytes_read + info.file_size) / 1e6:.0f} MB uncompressed, over "
                            "Gantrel's limit", "Simplify the mesh and export it again")
        with self.zf.open(info) as f:
            raw = f.read(MAX_BYTES - self.bytes_read + 1)
        self.bytes_read += len(raw)
        if self.bytes_read > MAX_BYTES:
            raise MeshError("the 3MF model is over Gantrel's limit uncompressed", "Simplify the mesh and export it again")
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as err:
            raise MeshError(f"the 3MF model XML is malformed ({err})", "Export the part again as 3MF") from None
        self.parts[name] = root
        return root

    def objects(self, part: str) -> dict[str, ElementTree.Element]:
        if part not in self.index:
            self.index[part] = {o.get("id"): o for o in self.model(part).iter(f"{CORE_NS}object")}
        return self.index[part]

    def triangles(self, ref: tuple[str, str], tf, stack: tuple = ()) -> list[np.ndarray]:
        part, oid = ref
        if ref in stack:
            raise MeshError("the 3MF has an object that contains itself through its components",
                            "Export the part again as 3MF")
        if len(stack) >= MAX_COMPONENT_DEPTH:
            raise MeshError(f"the 3MF nests components more than {MAX_COMPONENT_DEPTH} deep",
                            "Export the part again as 3MF")
        obj = self.objects(part).get(oid)
        if obj is None:
            raise MeshError(f"the 3MF refers to object {oid!r}, which is not in {part}", "Export the part again as 3MF")
        a, t = tf
        out = []
        local = self._mesh(part, oid, obj)
        if local is not None and len(local):
            self.triangle_count += len(local)
            if self.triangle_count > MAX_TRIANGLES:
                raise MeshError(f"the 3MF has more than {MAX_TRIANGLES} triangles, over Gantrel's limit",
                                "Simplify the mesh and export it again")
            out.append(local @ a + t)
        comps = obj.find(f"{CORE_NS}components")
        if comps is not None:
            for c in comps.findall(f"{CORE_NS}component"):
                self.components += 1
                if self.components > MAX_COMPONENTS:
                    raise MeshError(f"the 3MF has more than {MAX_COMPONENTS} components",
                                    "Simplify the model and export it again")
                ca, ct = _transform(c.get("transform"))
                child = (self.resolve(c.get(PATH_ATTR), part), c.get("objectid"))
                out += self.triangles(child, (ca @ a, ct @ a + t), stack + (ref,))
        return out

    def _mesh(self, part: str, oid: str, obj) -> np.ndarray | None:
        key = (part, oid)
        if key in self.meshes:
            return self.meshes[key]
        mesh = obj.find(f"{CORE_NS}mesh")
        verts = None if mesh is None else mesh.find(f"{CORE_NS}vertices")
        tris = None if mesh is None else mesh.find(f"{CORE_NS}triangles")
        if verts is None or tris is None:
            self.meshes[key] = None
            return None
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
            self.meshes[key] = None
            return None
        if idx.min() < 0 or idx.max() >= len(v):
            raise MeshError("the 3MF has a triangle that points at a vertex that does not exist")
        self.meshes[key] = v[idx]
        return self.meshes[key]


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


def inside_out(m: Mesh) -> Mesh:
    """The same mesh with every facet's winding reversed, so every normal points the other way. Its welding
    and edges carry over with the corners reordered (a, b, c -> a, c, b), so they are not worked out again."""
    out = Mesh(m.tris[:, [0, 2, 1]], m.name, m.kind, list(m.warnings), m.removed_degenerate)
    if "faces" in m.__dict__:
        out.__dict__["faces"] = m.faces[:, [0, 2, 1]]
    if "edges" in m.__dict__:
        uniq, counts, fe = m.edges
        out.__dict__["edges"] = (uniq, counts, fe[:, [2, 1, 0]])   # edge (a, c) was slot 2, (c, b) 1, (b, a) 0
    return out


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
    if wt["watertight"] and m.volume < 0:
        m = inside_out(m)
        m.warnings.append("the mesh's facets faced inward (its volume came out negative), so Gantrel turned every "
                          "facet around; if other programs show this part inside out, check the exporter")
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


ORIENTED_HEADER = "gantrel-printer plan: oriented"   # the header of a plan --write-oriented file


def write_stl(path: str | Path, mesh: Mesh, header: str = "gantrel-printer plan") -> Path:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(stl_bytes(mesh.tris, header))
    return p


def is_oriented_output(path: str | Path) -> bool:
    """True for a binary STL that plan --write-oriented wrote: it is already turned the plan's way."""
    p = Path(path).expanduser()
    if p.suffix.lower() != ".stl" or not p.is_file():
        return False
    with p.open("rb") as f:
        return f.read(80).startswith(ORIENTED_HEADER.encode())
