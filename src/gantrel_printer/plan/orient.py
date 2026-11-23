"""Choose how to lay a part on the bed: candidates, their supports and contact, and a ranking by goal.

Candidates are the six axis directions plus the area-weighted mean normals of the largest flat
regions (facet normals clustered within CLUSTER_DEG), at most MAX_CANDIDATES after removing near
duplicates. Each candidate is turned so that direction faces the bed, dropped to z = 0, and measured.
No candidate keeps a turned copy of the part; one is rebuilt when needed (Candidate.mesh). A part over
SCORE_FULL_MAX triangles has its supports and time scored on a simplified copy, then its best FINALISTS
measured again on the full mesh (rank).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .. import materials
from ..slicing import PrinterProfile
from . import geometry
from .mesh import Mesh, bounds, corner_z, placement, turn

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
    source: Mesh | None = field(default=None, repr=False)   # the part as loaded, shared by every candidate
    center_on: tuple[float, float] | None = None
    scored_on: int | None = None       # triangles its supports and time were measured on (rank sets it)

    @property
    def mesh(self) -> Mesh | None:
        """The turned part on the bed. Not kept on the candidate (a 1M-triangle part is about 100 MB per
        copy): it is rebuilt from the source on every access, so bind it once and let it go."""
        if self.source is None:
            return None
        return self.source.transformed(self.rotation, center_on=self.center_on)

    def turned(self, other: Mesh | None = None) -> Mesh | None:
        """Like mesh, or another copy of the part (a simplified one) laid on the bed the same way."""
        if other is None:
            return self.mesh
        return other.transformed(self.rotation, center_on=self.center_on)

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
            **({"scored_on_triangles": self.scored_on} if self.scored_on is not None else {}),
            **({"est_seconds": round(self.est_seconds)} if self.est_seconds is not None else {}),
            "reason": self.reason,
        }


MAX_REGIONS = 400              # flat regions collected before the rest of the facets are ignored
REGION_BLOCK = 4096            # facets compared to the regions at once


def candidate_directions(mesh: Mesh) -> np.ndarray:
    """The 6 axes plus the mean normals of the largest flat regions, deduplicated, at most MAX_CANDIDATES."""
    n, a = mesh.normals, mesh.areas
    order = np.argsort(-a)
    cos_c = math.cos(math.radians(CLUSTER_DEG))
    centers = np.zeros((MAX_REGIONS, 3))
    sums = np.zeros((MAX_REGIONS, 3))
    weights = np.zeros(MAX_REGIONS)
    k = 0
    # Greedy clustering over facets, largest first: a facet joins the region whose first facet's normal is
    # nearest, if within CLUSTER_DEG, else starts a region. Facets go in blocks of REGION_BLOCK; within a
    # block, every facet before the first one that starts a region joins a region already there, so those
    # are added at once and the block resumes after the new region. Once MAX_REGIONS exist, facets that
    # match none are dropped, and clustering stops at the end of that block.
    pos, total = 0, len(order)
    while pos < total:
        end = min((pos // REGION_BLOCK + 1) * REGION_BLOCK, total)
        ids = order[pos:end]
        v = n[ids]
        if k:
            dots = v @ centers[:k].T
            best = np.argmax(dots, axis=1)
            ok = dots[np.arange(len(ids)), best] >= cos_c
        else:
            best = np.zeros(len(ids), np.int64)
            ok = np.zeros(len(ids), bool)
        j = int(np.argmin(ok)) if k < MAX_REGIONS and not ok.all() else len(ids)
        sel = np.nonzero(ok[:j])[0]
        np.add.at(sums, best[sel], v[sel] * a[ids[sel]][:, None])
        np.add.at(weights, best[sel], a[ids[sel]])
        if j < len(ids):
            i = ids[j]
            centers[k], sums[k], weights[k] = n[i], n[i] * a[i], a[i]
            k += 1
            pos += j + 1
        else:
            pos = end
        if pos == end and k >= MAX_REGIONS:
            break
    regions = sorted(range(k), key=lambda r: -weights[r])
    dirs = [d for d in AXES]
    for r in regions:
        dirs.append(sums[r] / (np.linalg.norm(sums[r]) or 1.0))
    out: list[np.ndarray] = []
    cos_d = math.cos(math.radians(DUPLICATE_DEG))
    for d in dirs:
        if all(float(np.dot(d, o)) < cos_d for o in out):
            out.append(d)
        if len(out) >= MAX_CANDIDATES:
            break
    return np.array(out)


def evaluate(mesh: Mesh, down: np.ndarray, printer: PrinterProfile, fil: materials.Filament,
             load_axis: str | None = None, proxy: Mesh | None = None, bridges: bool = True) -> Candidate:
    """One orientation, measured. Fit, size, bed contact and overhang always come from the full mesh. With a
    proxy (a simplified copy of the mesh), supports are measured against the proxy's surfaces instead, which
    is what makes scoring 40 orientations of a big part fast; bridges=False skips the bridge search.

    The part is never turned as a whole here: its distinct vertices are turned (for the size and the
    heights), its normals give each facet's slope, and only the facets that face down get their corners
    looked up. One candidate costs a few passes over the mesh and holds no copy of it."""
    rot = geometry.rotation_to(down, [0.0, 0.0, -1.0])
    v, vid = mesh.vertices
    w_ = v @ rot.T
    lo, hi = bounds(w_)
    size = hi - lo
    w, d, h = float(size[0]), float(size[1]), float(size[2])
    bw, bd = printer.bed
    fits0 = w <= bw + 1e-6 and d <= bd + 1e-6
    fits90 = d <= bw + 1e-6 and w <= bd + 1e-6
    z_rot = 0
    if not fits0 and fits90:
        z_rot = 90
        rot = geometry.rot_z(90) @ rot
        w_ = v @ rot.T
        lo, hi = bounds(w_)
        w, d = d, w
    fits = (fits0 or fits90) and h <= printer.height + 1e-6
    shift = placement(lo, hi, (bw / 2, bd / 2))
    w_ += shift                               # every distinct vertex, as it sits on the bed
    nz = mesh.normals @ rot[2]                # each facet's normal, turned: its z part
    areas = mesh.areas

    sin_lim = math.sin(math.radians(fil.max_overhang_deg))
    sub = np.nonzero(nz < max(-sin_lim, BED_NORMAL_Z))[0]   # facets that face down enough to matter
    zv = w_[:, 2]
    corner = vid[sub]
    ztop = np.maximum(np.maximum(zv[corner[:, 0]], zv[corner[:, 1]]), zv[corner[:, 2]])
    nz_s = nz[sub]
    on_bed = (ztop <= BED_CONTACT_MM) & (nz_s < BED_NORMAL_Z)
    bed_area = float(areas[sub[on_bed]].sum())
    over_s = (nz_s < -sin_lim) & ~(ztop <= BED_CONTACT_MM)
    ids = sub[over_s]
    proj = areas[ids] * np.abs(nz[ids])
    over_area = float(proj.sum())

    vol = contact = 0.0
    rays = 0
    sampled = False
    if len(ids):
        rng = np.random.default_rng(SEED)
        p = proj / proj.sum()
        rays = RAY_SAMPLES
        pick = ids[rng.choice(len(ids), size=rays, p=p)]
        r1, r2 = rng.random(rays), rng.random(rays)
        flip = r1 + r2 > 1
        r1[flip], r2[flip] = 1 - r1[flip], 1 - r2[flip]
        tri = w_[vid[pick]]
        pts = tri[:, 0] + r1[:, None] * (tri[:, 1] - tri[:, 0]) + r2[:, None] * (tri[:, 2] - tri[:, 0])
        if proxy is None:
            floor_tris = w_[vid[nz > UP_NORMAL_Z]]
        else:   # the proxy, turned and moved exactly as the full mesh was
            pv, pid = proxy.vertices
            floor_tris = (pv @ rot.T + shift)[pid[proxy.normals @ rot[2] > UP_NORMAL_Z]]
        floors = geometry.highest_below(pts, floor_tris)
        del floor_tris
        weight = over_area / rays
        vol = float(((pts[:, 2] - floors) * weight).sum())
        landed = float((floors > BED_CONTACT_MM).sum() * weight)
        contact = over_area + landed
        sampled = len(ids) > rays

    strength = 0.0
    if load_axis:
        strength = abs(float((rot @ LOAD_AXES[load_axis])[2]))
    c = Candidate(down=[float(x) for x in down], rotation=rot, z_rotation_deg=z_rot, bed_contact_mm2=bed_area,
                  overhang_mm2=over_area, overhang_facets=len(ids), support_volume_mm3=vol,
                  support_contact_mm2=contact, support_rays=rays, sampled=sampled, height_mm=h,
                  footprint_mm=(w, d), fits=fits, strength_penalty=strength, source=mesh,
                  center_on=(bw / 2, bd / 2))
    if bridges:
        c.bridges = find_bridges(c.mesh)
    return c


def find_bridges(m: Mesh) -> list[dict]:
    """Flat ceilings held by walls on two sides that face each other, and their span. Best effort.

    A boundary edge of a flat, downward region is held when the facet on its other side goes down from
    it (a wall). The region is a bridge when two held edges face each other: each one's inward direction
    points toward the other. A ring held only around a central post (a mushroom cap) is a cantilever,
    not a bridge. The span is twice the largest distance from a point in the region to its nearest held edge.
    """
    t, nz = m.tris, m.normals[:, 2]
    zlow = corner_z(t)
    flat = np.nonzero((nz < BRIDGE_NORMAL_Z) & (zlow > BED_CONTACT_MM))[0]
    if len(flat) == 0:
        return []
    flat = flat[:BRIDGE_MAX_FACETS]
    _, _, fe = m.edges
    owner, start = m.edge_owners
    is_flat = np.zeros(len(t), bool)
    is_flat[flat] = True

    # Every (flat facet, its k-th edge, a facet on that edge) triple, from the shared edge index.
    slot_f = np.repeat(flat, 3)
    slot_k = np.tile(np.arange(3), len(flat))
    slot_e = fe[flat].ravel()
    cnt = start[slot_e + 1] - start[slot_e]
    pair_slot = np.repeat(np.arange(len(slot_f)), cnt)
    pair_g = owner[start[slot_e][pair_slot] + (np.arange(len(pair_slot)) - np.repeat(np.cumsum(cnt) - cnt, cnt))]
    pair_f = slot_f[pair_slot]

    # Join flat facets that share an edge and sit at the same height.
    join = (pair_g != pair_f) & is_flat[pair_g]
    join &= np.abs(zlow[pair_g] - zlow[pair_f]) < 1e-6
    parent = {int(f): int(f) for f in flat}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for f, g in zip(pair_f[join].tolist(), pair_g[join].tolist()):
        parent[find(g)] = find(f)
    root = np.array([find(int(f)) for f in flat])
    group = np.full(len(t), -1, np.int64)
    group[flat] = root

    # A held edge: exactly one facet across it is outside the region, and that facet goes down (a wall).
    outside = group[pair_g] != group[pair_f]
    n_out = np.bincount(pair_slot[outside], minlength=len(slot_f))
    other = np.full(len(slot_f), -1, np.int64)
    other[pair_slot[outside]] = pair_g[outside]
    held = n_out == 1
    held[held] = zlow[other[held]] < zlow[slot_f[held]] - 1e-3

    # Each held edge as a segment in xy, with the direction that points into its region.
    hs = np.nonzero(held)[0]
    hf, hk = slot_f[hs], slot_k[hs]
    ha, hb, hc = t[hf, hk, :2], t[hf, (hk + 1) % 3, :2], t[hf, (hk + 2) % 3, :2]
    perp = np.column_stack([-(hb[:, 1] - ha[:, 1]), hb[:, 0] - ha[:, 0]])
    length = np.linalg.norm(perp, axis=1)
    perp /= np.where(length > 0, length, 1.0)[:, None]
    perp[np.einsum("ij,ij->i", perp, hc - ha) < 0] *= -1   # toward the facet's third corner
    hroot = group[hf]
    by_root = np.argsort(hroot, kind="stable")
    roots, first, n_held = np.unique(hroot[by_root], return_index=True, return_counts=True)
    facet_order = np.argsort(root, kind="stable")
    sorted_root = root[facet_order]

    facing = _facing_regions(roots, n_held, ha[by_root], hb[by_root], perp[by_root])
    out = []
    for r, lo_h, nh in zip(roots[facing].tolist(), first[facing].tolist(), n_held[facing].tolist()):
        lo, hi = np.searchsorted(sorted_root, [r, r + 1])
        facets = flat[facet_order[lo:hi]].tolist()
        z = float(zlow[facets[0]])
        ids = by_root[lo_h:lo_h + nh]
        seg = np.stack([ha[ids], hb[ids]], axis=1)
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
    return sorted(out, key=lambda b: (-b["span_mm"], b["z_mm"], -b["area_mm2"]))


def _facing_regions(roots: np.ndarray, n_held: np.ndarray, a: np.ndarray, b: np.ndarray,
                    inward: np.ndarray) -> np.ndarray:
    """For each region (its held edges are consecutive in a, b, inward, n_held[i] of them), whether two of
    its held edges face each other: opposite inward directions, each in front of the other. Every pair
    within a region is tested at once, in batches of at most geometry.CHUNK pairs."""
    mid = (a + b) / 2
    region = np.repeat(np.arange(len(roots)), n_held)
    first = np.repeat(np.cumsum(n_held) - n_held, n_held)
    per_row = n_held[region]                          # each edge is paired with every edge of its region
    found = np.zeros(len(roots), bool)
    for rb in geometry._batches(per_row, geometry.CHUNK):
        i, k = geometry._expand(per_row[rb])
        i += rb.start
        j = first[i] + k
        gap = mid[j] - mid[i]                         # mid_j - mid_i
        ok = (np.einsum("ij,ij->i", inward[i], inward[j]) < -0.5) \
            & (np.einsum("ij,ij->i", gap, inward[i]) > 1e-6) & (np.einsum("ij,ij->i", -gap, inward[j]) > 1e-6)
        found[region[i[ok]]] = True
    return found


def _q(x: float, step: float) -> int:
    return int(round(x / step)) if step > 0 else 0


SCORE_FULL_MAX = 200_000      # parts with more triangles are scored on a simplified copy (Mesh.simplified)
SCORE_PROXY_TRIANGLES = 100_000
FINALISTS = 3                 # the best this many by the copy are measured again on the full mesh and re-sorted


def rank(mesh: Mesh, printer: PrinterProfile, fil: materials.Filament, goal: str = "quality",
         load_axis: str | None = None, time_fn=None) -> list[Candidate]:
    """Every candidate, best first. time_fn(candidate, turned) -> seconds is needed for the speed goal;
    turned is the part (or its simplified copy) as the candidate lays it on the bed.

    A part over SCORE_FULL_MAX triangles is scored in two passes. Every candidate gets its fit, size, bed
    contact and overhang from the full mesh, and its support volume and print time from a simplified copy
    of about SCORE_PROXY_TRIANGLES. The best FINALISTS are then measured again on the full mesh and sorted
    among themselves. Each candidate's scored_on says how many triangles its supports and time came from.
    """
    if goal not in GOALS:
        raise ValueError(f"unknown goal {goal!r}; use one of {', '.join(GOALS)}")
    if load_axis and load_axis not in LOAD_AXES:
        raise ValueError(f"unknown load axis {load_axis!r}; use x, y or z")
    proxy = mesh.simplified(SCORE_PROXY_TRIANGLES) if len(mesh.tris) > SCORE_FULL_MAX else None
    dirs = candidate_directions(mesh)
    cands = [evaluate(mesh, d, printer, fil, load_axis, proxy=proxy, bridges=False) for d in dirs]
    area_step = TIE_FRACTION * mesh.surface_area
    vol_step = TIE_FRACTION * abs(mesh.volume) if mesh.volume else area_step

    def timed(c: Candidate, turned: Mesh | None) -> None:
        if goal == "speed" or time_fn is not None:
            c.est_seconds = time_fn(c, turned if turned is not None else c.mesh) if time_fn else None

    def order(cs: list[Candidate]) -> list[Candidate]:
        times = [c.est_seconds for c in cs if c.est_seconds is not None]
        time_step = max(1.0, TIE_FRACTION * min(times)) if times else 1.0

        def key(c: Candidate):
            head = (0 if c.fits else 1, round(c.strength_penalty, 1))
            support = (_q(c.support_contact_mm2, area_step), _q(c.support_volume_mm3, vol_step))
            if goal == "speed" and c.est_seconds is not None:
                return head + (_q(c.est_seconds, time_step),) + support
            if goal == "balanced":
                # Supports first, then whichever of time or bed contact is better.
                return head + support + (_q(c.est_seconds or 0.0, 2 * time_step), -_q(c.bed_contact_mm2, area_step))
            return head + support + (-_q(c.bed_contact_mm2, area_step), round(c.height_mm, 2))

        return sorted(cs, key=key)

    for c in cands:
        c.scored_on = len((proxy if proxy is not None else mesh).tris)
        timed(c, c.turned(proxy) if proxy is not None else None)
    cands = order(cands)
    if proxy is not None:
        final = [evaluate(mesh, np.asarray(c.down), printer, fil, load_axis, bridges=False)
                 for c in cands[:FINALISTS]]
        for c in final:
            c.scored_on = len(mesh.tris)
            turned = c.mesh        # one full turned copy at a time
            timed(c, turned)
            c.bridges = find_bridges(turned)
            del turned
        cands = order(final) + cands[FINALISTS:]
    else:
        for c in cands[:FINALISTS]:
            c.bridges = find_bridges(c.mesh)
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
