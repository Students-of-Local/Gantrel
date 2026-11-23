"""Choose how to lay a part on the bed: candidates, their supports and contact, and a ranking by goal.

Candidates are the six axis directions plus the area-weighted mean normals of the largest flat
regions (facet normals clustered within CLUSTER_DEG), at most MAX_CANDIDATES after removing near
duplicates. Each candidate is turned so that direction faces the bed, dropped to z = 0, and measured.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .. import materials
from ..slicing import PrinterProfile
from . import geometry
from .mesh import Mesh

GOALS = ("quality", "balanced", "speed")
MAX_CANDIDATES = 40
CLUSTER_DEG = 5.0              # facet normals this close count as one flat region
DUPLICATE_DEG = 2.0            # candidate directions this close are the same candidate
BED_CONTACT_MM = 0.05          # a facet within this of the bed, facing down, touches the bed
BED_NORMAL_Z = -0.99           # and faces down at least this squarely
RAY_SAMPLES = 1024             # downward rays per candidate for support volume
SEED = 20261122                # fixed so the same part always gets the same answer
TIE_FRACTION = 0.01            # differences under 1 percent of the part's area or volume count as ties
UP_NORMAL_Z = 1e-6             # facets facing up at all can carry support (or the part) above them
BRIDGE_NORMAL_Z = -0.99        # flat ceilings that may print as bridges
BRIDGE_SAMPLES = 256
BRIDGE_MAX_FACETS = 20000     # past this many flat ceiling facets, only the first are checked
SHALLOW_SLOPE_Z = (0.5, 0.9998)  # up-facing slopes about 1 to 60 degrees from flat show layer steps (optimize)
AXES = np.array([[0, 0, -1], [0, 0, 1], [1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0]], float)
LOAD_AXES = {"x": np.array([1.0, 0, 0]), "y": np.array([0, 1.0, 0]), "z": np.array([0, 0, 1.0])}


@dataclass
class Candidate:
    down: list[float]                  # the direction, in the part's own coordinates, that faces the bed
    rotation: np.ndarray               # 3x3: part coordinates to printed coordinates (z up)
    z_rotation_deg: int
    bed_contact_mm2: float
    overhang_mm2: float                # projected area of facets steeper than the material allows
    overhang_facets: int
    support_volume_mm3: float
    support_contact_mm2: float         # where supports touch the part: under overhangs and where they land on it
    support_rays: int
    sampled: bool
    height_mm: float
    footprint_mm: tuple[float, float]
    fits: bool
    strength_penalty: float            # |load axis . Z| after turning: 1 means layers pull apart under load
    bridges: list[dict] = field(default_factory=list)
    est_seconds: float | None = None
    reason: str = ""
    mesh: Mesh | None = None           # the turned part, on the bed

    @property
    def needs_support(self) -> bool:
        return self.overhang_mm2 > 0

    def summary(self) -> dict:
        axis, angle = geometry.axis_angle_deg(self.rotation)
        return {
            "down_vector": [round(v, 4) + 0.0 for v in self.down],
            "rotation_matrix": [[round(float(v), 6) + 0.0 for v in row] for row in self.rotation],
            "rotate_xyz_deg": geometry.euler_xyz_deg(self.rotation),
            "axis_angle": {"axis": axis, "degrees": angle},
            "z_rotation_deg": self.z_rotation_deg,
            "bed_contact_mm2": round(self.bed_contact_mm2, 1),
            "overhang_mm2": round(self.overhang_mm2, 1), "overhang_facets": self.overhang_facets,
            "supports_needed": self.needs_support,
            "support_volume_mm3": round(self.support_volume_mm3, 1),
            "support_contact_mm2": round(self.support_contact_mm2, 1),
            "support_rays": self.support_rays, "support_sampled": self.sampled,
            "height_mm": round(self.height_mm, 3),
            "footprint_mm": [round(v, 2) for v in self.footprint_mm], "fits": self.fits,
            "strength_penalty": round(self.strength_penalty, 3),
            "bridges": self.bridges,
            **({"est_seconds": round(self.est_seconds)} if self.est_seconds is not None else {}),
            "reason": self.reason,
        }


def candidate_directions(mesh: Mesh) -> np.ndarray:
    """The 6 axes plus the mean normals of the largest flat regions, deduplicated, at most MAX_CANDIDATES."""
    n, a = mesh.normals, mesh.areas
    order = np.argsort(-a)
    cos_c = math.cos(math.radians(CLUSTER_DEG))
    centers: list[np.ndarray] = []
    sums: list[np.ndarray] = []
    weights: list[float] = []
    # Greedy clustering over facets, largest first; a facet joins the first region within CLUSTER_DEG.
    # Facets are visited in chunks: each chunk is compared to all regions at once.
    for s in range(0, len(order), 4096):
        ids = order[s:s + 4096]
        for i in ids:
            v = n[i]
            if centers:
                dots = np.asarray(centers) @ v
                k = int(np.argmax(dots))
                if dots[k] >= cos_c:
                    sums[k] += v * a[i]
                    weights[k] += a[i]
                    continue
            if len(centers) >= 400:   # enough regions; the rest are small
                continue
            centers.append(v.copy())
            sums.append(v * a[i])
            weights.append(float(a[i]))
        if len(centers) >= 400:
            break
    regions = sorted(range(len(centers)), key=lambda k: -weights[k])
    dirs = [d for d in AXES]
    for k in regions:
        v = sums[k] / (np.linalg.norm(sums[k]) or 1.0)
        dirs.append(v)
    out: list[np.ndarray] = []
    cos_d = math.cos(math.radians(DUPLICATE_DEG))
    for d in dirs:
        if all(float(np.dot(d, o)) < cos_d for o in out):
            out.append(d)
        if len(out) >= MAX_CANDIDATES:
            break
    return np.array(out)


def evaluate(mesh: Mesh, down: np.ndarray, printer: PrinterProfile, fil: materials.Filament,
             load_axis: str | None = None) -> Candidate:
    rot = geometry.rotation_to(down, [0.0, 0.0, -1.0])
    turned = mesh.transformed(rot)
    size = turned.size
    w, d, h = float(size[0]), float(size[1]), float(size[2])
    bw, bd = printer.bed
    fits0 = w <= bw + 1e-6 and d <= bd + 1e-6
    fits90 = d <= bw + 1e-6 and w <= bd + 1e-6
    z_rot = 0
    if not fits0 and fits90:
        z_rot = 90
        rot = geometry.rot_z(90) @ rot
        turned = mesh.transformed(rot)
        w, d = d, w
    fits = (fits0 or fits90) and h <= printer.height + 1e-6
    turned = mesh.transformed(rot, center_on=(bw / 2, bd / 2))

    t, nz, areas = turned.tris, turned.normals[:, 2], turned.areas
    ztop = t[:, :, 2].max(axis=1)
    on_bed = (ztop <= BED_CONTACT_MM) & (nz < BED_NORMAL_Z)
    bed_area = float(areas[on_bed].sum())
    sin_lim = math.sin(math.radians(fil.max_overhang_deg))
    over = (nz < -sin_lim) & ~(ztop <= BED_CONTACT_MM)
    proj = areas * np.abs(nz)
    over_area = float(proj[over].sum())

    vol = contact = 0.0
    rays = 0
    sampled = False
    if over.any():
        rng = np.random.default_rng(SEED)
        ids = np.nonzero(over)[0]
        p = proj[ids] / proj[ids].sum()
        rays = RAY_SAMPLES
        pick = ids[rng.choice(len(ids), size=rays, p=p)]
        r1, r2 = rng.random(rays), rng.random(rays)
        flip = r1 + r2 > 1
        r1[flip], r2[flip] = 1 - r1[flip], 1 - r2[flip]
        tri = t[pick]
        pts = tri[:, 0] + r1[:, None] * (tri[:, 1] - tri[:, 0]) + r2[:, None] * (tri[:, 2] - tri[:, 0])
        floors = geometry.highest_below(pts, t[nz > UP_NORMAL_Z])
        weight = over_area / rays
        vol = float(((pts[:, 2] - floors) * weight).sum())
        landed = float((floors > BED_CONTACT_MM).sum() * weight)
        contact = over_area + landed
        sampled = len(ids) > rays

    strength = 0.0
    if load_axis:
        strength = abs(float((rot @ LOAD_AXES[load_axis])[2]))
    c = Candidate(down=[float(v) for v in down], rotation=rot, z_rotation_deg=z_rot, bed_contact_mm2=bed_area,
                  overhang_mm2=over_area, overhang_facets=int(over.sum()), support_volume_mm3=vol,
                  support_contact_mm2=contact, support_rays=rays, sampled=sampled, height_mm=h,
                  footprint_mm=(w, d), fits=fits, strength_penalty=strength, mesh=turned)
    c.bridges = find_bridges(turned)
    return c


def find_bridges(m: Mesh) -> list[dict]:
    """Flat ceilings held by walls on two sides that face each other, and their span. Best effort.

    A boundary edge of a flat, downward region is held when the facet on its other side goes down from
    it (a wall). The region is a bridge when two held edges face each other: each one's inward direction
    points toward the other. A ring held only around a central post (a mushroom cap) is a cantilever,
    not a bridge. The span is twice the largest distance from a point in the region to its nearest held edge.
    """
    t, nz = m.tris, m.normals[:, 2]
    zlow = t[:, :, 2].min(axis=1)
    flat = np.nonzero((nz < BRIDGE_NORMAL_Z) & (zlow > BED_CONTACT_MM))[0]
    if len(flat) == 0:
        return []
    flat = flat[:BRIDGE_MAX_FACETS]
    _, _, fe = m.edges
    # Facets that use each edge: sort facet-edge slots by edge id, then look up ranges.
    slot_edge = fe.ravel()
    order = np.argsort(slot_edge, kind="stable")
    sorted_edges = slot_edge[order]
    slot_facet = order // 3

    def owners(e: int) -> np.ndarray:
        lo, hi = np.searchsorted(sorted_edges, [e, e + 1])
        return slot_facet[lo:hi]

    flat_set = set(int(f) for f in flat)
    parent = {f: f for f in flat_set}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for f in flat_set:
        for e in fe[f]:
            for g in owners(int(e)):
                g = int(g)
                if g != f and g in flat_set and abs(zlow[g] - zlow[f]) < 1e-6:
                    parent[find(g)] = find(f)
    groups: dict[int, list[int]] = {}
    for f in flat_set:
        groups.setdefault(find(f), []).append(f)
    out = []
    for facets in groups.values():
        fs = set(facets)
        z = float(zlow[facets[0]])
        segs, inward = [], []
        for f in facets:
            for k, e in enumerate(fe[f]):
                others = [int(g) for g in owners(int(e)) if int(g) not in fs]
                if len(others) != 1:
                    continue
                g = others[0]
                if t[g][:, 2].min() >= z - 1e-3:   # the neighbor does not go down: no wall under this edge
                    continue
                a, b, c = t[f][k][:2], t[f][(k + 1) % 3][:2], t[f][(k + 2) % 3][:2]
                perp = np.array([-(b[1] - a[1]), b[0] - a[0]])
                perp /= np.linalg.norm(perp) or 1.0
                if np.dot(perp, c - a) < 0:     # point into the region, toward the facet's third corner
                    perp = -perp
                segs.append((a, b))
                inward.append(perp)
        if len(segs) < 2:
            continue
        seg = np.array(segs)
        mid = seg.mean(axis=1)
        inn = np.array(inward)
        gap = mid[None, :, :] - mid[:, None, :]                       # gap[i, j] = mid_j - mid_i
        facing = ((inn @ inn.T) < -0.5) & (np.einsum("ijk,ik->ij", gap, inn) > 1e-6) \
            & (np.einsum("ijk,jk->ij", -gap, inn) > 1e-6)
        if not facing.any():
            continue
        rng = np.random.default_rng(SEED)
        area = m.areas[facets]
        pick = np.asarray(facets)[rng.choice(len(facets), size=BRIDGE_SAMPLES, p=area / area.sum())]
        r1, r2 = rng.random(BRIDGE_SAMPLES), rng.random(BRIDGE_SAMPLES)
        flip = r1 + r2 > 1
        r1[flip], r2[flip] = 1 - r1[flip], 1 - r2[flip]
        tri = t[pick][:, :, :2]
        pts = tri[:, 0] + r1[:, None] * (tri[:, 1] - tri[:, 0]) + r2[:, None] * (tri[:, 2] - tri[:, 0])
        a, b = seg[:, 0], seg[:, 1]
        ab = b - a
        tt = (np.einsum("pk,sk->ps", pts, ab) - np.einsum("sk,sk->s", a, ab)) / np.maximum(
            np.einsum("sk,sk->s", ab, ab), 1e-12)
        tt = np.clip(tt, 0, 1)
        nearest = a[None] + tt[..., None] * ab[None]
        dist = np.linalg.norm(pts[:, None, :] - nearest, axis=2).min(axis=1)
        out.append({"z_mm": round(z, 3), "span_mm": round(float(2 * dist.max()), 1),
                    "area_mm2": round(float(area.sum()), 1)})
    return sorted(out, key=lambda b: -b["span_mm"])


def _q(x: float, step: float) -> int:
    return int(round(x / step)) if step > 0 else 0


def rank(mesh: Mesh, printer: PrinterProfile, fil: materials.Filament, goal: str = "quality",
         load_axis: str | None = None, time_fn=None) -> list[Candidate]:
    """Every candidate, best first. time_fn(candidate) -> seconds is needed for the speed goal."""
    if goal not in GOALS:
        raise ValueError(f"unknown goal {goal!r}; use one of {', '.join(GOALS)}")
    if load_axis and load_axis not in LOAD_AXES:
        raise ValueError(f"unknown load axis {load_axis!r}; use x, y or z")
    cands = [evaluate(mesh, d, printer, fil, load_axis) for d in candidate_directions(mesh)]
    area_step = TIE_FRACTION * mesh.surface_area
    vol_step = TIE_FRACTION * abs(mesh.volume) if mesh.volume else area_step
    if goal == "speed" or time_fn is not None:
        for c in cands:
            c.est_seconds = time_fn(c) if time_fn else None

    times = [c.est_seconds for c in cands if c.est_seconds is not None]
    time_step = max(1.0, TIE_FRACTION * min(times)) if times else 1.0   # measured before sort() empties the list

    def key(c: Candidate):
        head = (0 if c.fits else 1, round(c.strength_penalty, 1))
        support = (_q(c.support_contact_mm2, area_step), _q(c.support_volume_mm3, vol_step))
        if goal == "speed" and c.est_seconds is not None:
            return head + (_q(c.est_seconds, time_step),) + support
        if goal == "balanced":
            # Supports first, then whichever of time or bed contact is better.
            return head + support + (_q(c.est_seconds or 0.0, 2 * time_step), -_q(c.bed_contact_mm2, area_step))
        return head + support + (-_q(c.bed_contact_mm2, area_step), round(c.height_mm, 2))

    cands.sort(key=key)
    for c in cands:
        c.reason = _reason(c, load_axis)
    return cands


def _reason(c: Candidate, load_axis: str | None) -> str:
    bits = []
    if not c.fits:
        bits.append("does not fit the printer this way")
    if c.needs_support:
        bits.append(f"needs about {c.support_volume_mm3:.0f} mm3 of support under {c.overhang_mm2:.0f} mm2 of overhang")
    else:
        bits.append("no supports")
    bits.append(f"{c.bed_contact_mm2:.0f} mm2 on the bed, {c.height_mm:.1f} mm tall")
    if load_axis:
        bits.append("layers run along the load" if c.strength_penalty < 0.5 else "load pulls the layers apart")
    if c.z_rotation_deg:
        bits.append("turned 90 degrees on the bed to fit")
    if c.est_seconds is not None:
        bits.append(f"about {c.est_seconds / 60:.0f} min")
    return ", ".join(bits)


THIN_SAMPLES = 512
THIN_MAX_TRIANGLES = 200_000    # past this the ray test is too slow; the rule is skipped and says so
THIN_PERCENTILE = 2             # ignore the thinnest 2 percent of samples: corners and edges read as thin


def thinnest_wall(m: Mesh) -> float | None:
    """Wall thickness from rays cast inward from random points on the surface. None when skipped."""
    if len(m.tris) > THIN_MAX_TRIANGLES:
        return None
    rng = np.random.default_rng(SEED)
    a = m.areas
    pick = rng.choice(len(a), size=THIN_SAMPLES, p=a / a.sum())
    r1, r2 = rng.random(THIN_SAMPLES), rng.random(THIN_SAMPLES)
    flip = r1 + r2 > 1
    r1[flip], r2[flip] = 1 - r1[flip], 1 - r2[flip]
    tri = m.tris[pick]
    pts = tri[:, 0] + r1[:, None] * (tri[:, 1] - tri[:, 0]) + r2[:, None] * (tri[:, 2] - tri[:, 0])
    d = geometry.ray_distances(pts, -m.normals[pick], m.tris, min_t=1e-4)
    d = d[np.isfinite(d)]
    if len(d) == 0:
        return None
    return float(np.percentile(d, THIN_PERCENTILE))
