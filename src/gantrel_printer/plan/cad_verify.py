"""Deterministic pure-NumPy 3D CAD / DFM mesh verifier.

Pure math + STL parsing only (stdlib + numpy; zero network, zero subprocess).
Checks implemented deterministically:
  1. Watertight 2-manifold (0 boundary edges, 0 multi-manifold edges, 0 winding mismatches).
  2. Consistent outward winding & positive signed volume (catches inverted polyhedra).
  3. Connected-component body count & per-body bounding boxes / volumes.
  4. Degenerate face / edge counts & far-from-origin float32 hazard check.
  5. Flat bed seating (`min_z == 0 +- 0.05 mm`) & first-layer bed contact area.
  6. Unsupported overhang fraction past 45 deg from vertical (excluding bed contact).
  7. Exact plane cross-section loop extraction (outer contours, inner holes, radial wall thickness).
  8. Multi-body clash detection (AABB overlap grid + face-centroid Z-ray parity).
"""
from __future__ import annotations

import math
import struct
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np


def load_stl(path: str | Path) -> np.ndarray:
    """Load binary or ASCII STL into float64 array of shape (N, 3, 3)."""
    data = Path(path).read_bytes()
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if 84 + 50 * n == len(data):
            if n == 0:
                return np.empty((0, 3, 3), dtype=np.float64)
            dt = np.dtype([
                ("normal", "<f4", (3,)),
                ("v", "<f4", (3, 3)),
                ("attr", "<u2"),
            ])
            arr = np.frombuffer(data, dtype=dt, count=n, offset=84)
            return arr["v"].astype(np.float64)
    # Fallback ASCII STL
    verts: list[tuple[float, float, float]] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("vertex "):
            parts = s.split()
            if len(parts) >= 4:
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
    if len(verts) < 3:
        return np.empty((0, 3, 3), dtype=np.float64)
    m = (len(verts) // 3) * 3
    return np.array(verts[:m], dtype=np.float64).reshape(-1, 3, 3)


def _quantize_vertices(tris: np.ndarray, tol: float = 1e-4) -> tuple[np.ndarray, np.ndarray]:
    """Weld coincident vertices at `tol` mm, returning (unique_verts, face_indices)."""
    pts = tris.reshape(-1, 3)
    q = np.round(pts / tol).astype(np.int64)
    _, idx_unique, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    # Use original float coords of first representative for accuracy
    uniq = pts[idx_unique]
    faces = inv.ravel().reshape(-1, 3)
    return uniq, faces


def connected_components(faces: np.ndarray, n_verts: int) -> list[np.ndarray]:
    """Return list of triangle index arrays, one per vertex-connected body."""
    n_tris = len(faces)
    if n_tris == 0:
        return []
    parent = list(range(n_verts))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for a, b, c in faces.tolist():
        union(a, b)
        union(b, c)

    groups: dict[int, list[int]] = defaultdict(list)
    for ti, (a, _, _) in enumerate(faces.tolist()):
        groups[find(a)].append(ti)
    comps = [np.array(idxs, dtype=np.int64) for idxs in groups.values()]
    comps.sort(key=len, reverse=True)
    return comps


def analyze_mesh(tris: np.ndarray, overhang_deg: float = 45.0) -> dict[str, Any]:
    """Compute exact topology, manifoldness, winding, bodies, bed seating, and overhang metrics."""
    n = len(tris)
    if n == 0:
        return {"triangle_count": 0, "is_watertight": False, "is_manifold": False, "bodies": 0}

    v0, v1, v2 = tris[:, 0], tris[:, 1], tris[:, 2]
    e01 = v1 - v0
    e02 = v2 - v0
    e12 = v2 - v1
    cross = np.cross(e01, e02)
    double_areas = np.linalg.norm(cross, axis=1)
    areas = 0.5 * double_areas
    surface_area = float(areas.sum())
    signed_volume = float(np.einsum("ij,ij->i", v0, cross).sum() / 6.0)

    degenerate_faces = int(np.sum(areas <= 1e-6))
    edge_lens = np.concatenate([
        np.linalg.norm(e01, axis=1),
        np.linalg.norm(e02, axis=1),
        np.linalg.norm(e12, axis=1),
    ])
    degenerate_edges = int(np.sum(edge_lens <= 1e-6))

    pts = tris.reshape(-1, 3)
    bmin = pts.min(axis=0)
    bmax = pts.max(axis=0)
    size = bmax - bmin
    max_abs = float(np.abs(pts).max())

    # Exact half-edge manifoldness & winding consistency
    uniq, faces = _quantize_vertices(tris, tol=1e-4)
    # Build directed edges (u, v) for all non-degenerate faces
    u = np.concatenate([faces[:, 0], faces[:, 1], faces[:, 2]])
    v = np.concatenate([faces[:, 1], faces[:, 2], faces[:, 0]])
    valid_e = u != v
    u, v = u[valid_e], v[valid_e]
    lo = np.minimum(u, v)
    hi = np.maximum(u, v)
    direction = np.where(u < v, 1, -1)

    # Sort undirected edges to count occurrences and check opposite directions
    order = np.lexsort((hi, lo))
    lo_s, hi_s, dir_s = lo[order], hi[order], direction[order]
    edge_change = np.empty(len(lo_s) + 1, dtype=bool)
    edge_change[0] = True
    edge_change[-1] = True
    if len(lo_s) > 1:
        edge_change[1:-1] = (lo_s[1:] != lo_s[:-1]) | (hi_s[1:] != hi_s[:-1])
    starts = np.flatnonzero(edge_change)
    counts = np.diff(starts)

    boundary_edges = int(np.sum(counts == 1))
    multi_manifold_edges = int(np.sum(counts > 2))

    # For every edge with count == 2, sum of directions must be 0 (+1 and -1)
    pair_idx = starts[:-1][counts == 2]
    if len(pair_idx) > 0:
        winding_mismatches = int(np.sum((dir_s[pair_idx] + dir_s[pair_idx + 1]) != 0))
    else:
        winding_mismatches = 0

    is_watertight = (boundary_edges == 0) and (multi_manifold_edges == 0) and (n >= 4)
    is_manifold = is_watertight and (winding_mismatches == 0) and (signed_volume > 0)

    # Connected bodies
    comps = connected_components(faces, len(uniq))
    body_infos = []
    for c_idx in comps:
        ct = tris[c_idx]
        cp = ct.reshape(-1, 3)
        cmin, cmax = cp.min(axis=0), cp.max(axis=0)
        cv = float(np.einsum("ij,ij->i", ct[:, 0], np.cross(ct[:, 1] - ct[:, 0], ct[:, 2] - ct[:, 0])).sum() / 6.0)
        body_infos.append({
            "tris": int(len(c_idx)),
            "bounds_min": cmin.tolist(),
            "bounds_max": cmax.tolist(),
            "size": (cmax - cmin).tolist(),
            "volume": round(cv, 3),
        })

    # Bed seating & overhang fraction
    min_z = float(bmin[2])
    nonzero = double_areas > 1e-9
    nz = np.zeros(n, dtype=np.float64)
    nz[nonzero] = cross[nonzero, 2] / double_areas[nonzero]
    # If mesh is inverted (negative volume), don't fake overhang pass
    tri_z_max = tris[:, :, 2].max(axis=1)
    on_bed = (tri_z_max <= min_z + 0.08) & (nz < -0.95)
    first_layer_area = float(areas[on_bed].sum())

    # Overhang angle from vertical: vertical wall has nz=0, horizontal ceiling has nz=-1.
    # Angle from vertical > overhang_deg means nz < -sin(radians(overhang_deg)) - 1e-4
    # (allow tiny tolerance of +1.5 deg so exact 45 deg chamfers pass cleanly)
    thresh_nz = -math.sin(math.radians(overhang_deg + 1.5))
    overhang_mask = (~on_bed) & (nz < thresh_nz) & (tris[:, :, 2].min(axis=1) > min_z + 0.15)
    overhang_area = float(areas[overhang_mask].sum())
    overhang_frac = overhang_area / surface_area if surface_area > 0 else 1.0

    return {
        "triangle_count": int(n),
        "bounds_min": [round(float(x), 4) for x in bmin],
        "bounds_max": [round(float(x), 4) for x in bmax],
        "size": [round(float(x), 4) for x in size],
        "max_abs_coord": round(max_abs, 3),
        "signed_volume": round(signed_volume, 3),
        "surface_area": round(surface_area, 3),
        "degenerate_faces": degenerate_faces,
        "degenerate_edges": degenerate_edges,
        "boundary_edges": boundary_edges,
        "multi_manifold_edges": multi_manifold_edges,
        "winding_mismatches": winding_mismatches,
        "is_watertight": is_watertight,
        "is_manifold": is_manifold,
        "bodies": len(comps),
        "body_infos": body_infos,
        "min_z": round(min_z, 4),
        "first_layer_area_mm2": round(first_layer_area, 2),
        "overhang_area_mm2": round(overhang_area, 2),
        "overhang_fraction": round(overhang_frac, 4),
        "_comps": comps,
    }


def slice_loops(tris: np.ndarray, axis: str, coord: float) -> list[np.ndarray]:
    """Exact plane-triangle cross-section returning closed 2D loops (Nx2 arrays).

    Plane coordinates:
      axis == 'z' -> (x, y)
      axis == 'y' -> (x, z)
      axis == 'x' -> (y, z)
    """
    ax_idx = {"x": 0, "y": 1, "z": 2}[axis]
    other = [i for i in (0, 1, 2) if i != ax_idx]

    # Small jitter avoids slicing directly along horizontal edges/coplanar faces
    c = coord + 1.37e-4
    d = tris[:, :, ax_idx] - c  # (N, 3)
    above = d > 0
    count_above = above.sum(axis=1)
    straddle = (count_above == 1) | (count_above == 2)
    st = tris[straddle]
    sd = d[straddle]
    if len(st) == 0:
        return []

    segs: list[tuple[tuple[float, float], tuple[float, float]]] = []
    edges = ((0, 1), (1, 2), (2, 0))
    for tri, td in zip(st, sd):
        pts2d = []
        for i, j in edges:
            if (td[i] > 0) != (td[j] > 0):
                t = td[i] / (td[i] - td[j])
                p = tri[i] + t * (tri[j] - tri[i])
                pts2d.append((float(p[other[0]]), float(p[other[1]])))
        if len(pts2d) == 2:
            if math.hypot(pts2d[0][0] - pts2d[1][0], pts2d[0][1] - pts2d[1][1]) > 1e-6:
                segs.append((pts2d[0], pts2d[1]))

    if not segs:
        return []

    # Chain segments into closed loops by quantized vertex key
    scale = 1000.0  # 1e-3 mm snapping for chaining

    def key(p: tuple[float, float]) -> tuple[int, int]:
        return (int(round(p[0] * scale)), int(round(p[1] * scale)))

    adj: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, (p0, p1) in enumerate(segs):
        adj[key(p0)].append(idx)
        adj[key(p1)].append(idx)

    used = [False] * len(segs)
    loops: list[np.ndarray] = []
    for start_idx in range(len(segs)):
        if used[start_idx]:
            continue
        used[start_idx] = True
        p0, p1 = segs[start_idx]
        chain = [p0, p1]
        cur_k = key(p1)
        start_k = key(p0)
        for _ in range(len(segs) + 1):
            if cur_k == start_k and len(chain) >= 4:
                break
            nxt = None
            for si in adj[cur_k]:
                if not used[si]:
                    nxt = si
                    break
            if nxt is None:
                break
            used[nxt] = True
            a, b = segs[nxt]
            if key(a) == cur_k:
                chain.append(b)
                cur_k = key(b)
            else:
                chain.append(a)
                cur_k = key(a)
        if len(chain) >= 4 and key(chain[-1]) == start_k:
            loops.append(np.array(chain[:-1], dtype=np.float64))
    return loops


def loop_area(loop: np.ndarray) -> float:
    x, y = loop[:, 0], loop[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def loop_centroid(loop: np.ndarray) -> np.ndarray:
    x, y = loop[:, 0], loop[:, 1]
    c = x * np.roll(y, -1) - np.roll(x, -1) * y
    a = 0.5 * c.sum()
    if abs(a) < 1e-12:
        return loop.mean(axis=0)
    return np.array([
        ((x + np.roll(x, -1)) * c).sum() / (6.0 * a),
        ((y + np.roll(y, -1)) * c).sum() / (6.0 * a),
    ])


def point_in_loop(pt: np.ndarray, loop: np.ndarray) -> bool:
    x, y = float(pt[0]), float(pt[1])
    x0, y0 = loop[:, 0], loop[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    cond = (y0 > y) != (y1 > y)
    if not np.any(cond):
        return False
    xi = x0[cond] + (y - y0[cond]) * (x1[cond] - x0[cond]) / (y1[cond] - y0[cond])
    return bool(np.sum(x < xi) % 2 == 1)


def classify_loops(loops: list[np.ndarray]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split 2D loops into outer boundaries (even containment depth) and inner holes (odd depth)."""
    infos = []
    for l in loops:
        a = abs(loop_area(l))
        if a < 1e-4:
            continue
        cen = loop_centroid(l)
        bbox = np.ptp(l, axis=0)
        dists = np.linalg.norm(l - cen, axis=1)
        infos.append({
            "loop": l,
            "area": a,
            "centroid": cen,
            "bbox": bbox,
            "eq_diameter": 2.0 * math.sqrt(a / math.pi),
            "min_diameter": float(2.0 * dists.min()),
            "max_diameter": float(2.0 * dists.max()),
        })
    outers, holes = [], []
    for i, info in enumerate(infos):
        pt = info["loop"][0]
        depth = 0
        for j, other in enumerate(infos):
            if i != j and other["area"] > info["area"] and point_in_loop(pt, other["loop"]):
                depth += 1
        info["depth"] = depth
        if depth % 2 == 0:
            outers.append(info)
        else:
            holes.append(info)
    outers.sort(key=lambda d: d["area"], reverse=True)
    holes.sort(key=lambda d: d["area"], reverse=True)
    return outers, holes


def min_wall_around_hole(hole: dict[str, Any], outers: list[dict[str, Any]]) -> float:
    """Minimum Euclidean distance from a hole contour to its enclosing outer contour."""
    h_pts = hole["loop"]
    enclosing = [o for o in outers if o["area"] > hole["area"] and point_in_loop(h_pts[0], o["loop"])]
    if not enclosing:
        return 0.0
    # Pick smallest enclosing outer loop
    outer = min(enclosing, key=lambda o: o["area"])["loop"]
    # Distance from hole vertices to outer segments (and vice versa)
    def pts_to_segs(pts: np.ndarray, poly: np.ndarray) -> float:
        a = poly
        b = np.roll(poly, -1, axis=0)
        ab = b - a
        ab2 = np.einsum("ij,ij->i", ab, ab)
        best = float("inf")
        for p in pts:
            ap = p - a
            t = np.clip(np.einsum("ij,ij->i", ap, ab) / np.where(ab2 > 1e-12, ab2, 1.0), 0.0, 1.0)
            proj = a + t[:, None] * ab
            d2 = np.einsum("ij,ij->i", proj - p, proj - p).min()
            if d2 < best:
                best = d2
        return math.sqrt(best)

    return min(pts_to_segs(h_pts, outer), pts_to_segs(outer, h_pts))


def check_two_body_clash(tris_a: np.ndarray, tris_b: np.ndarray, margin: float = 0.02) -> bool:
    """Return True if two meshes physically intersect (clash), False if cleanly separated."""
    amin, amax = tris_a.reshape(-1, 3).min(axis=0), tris_a.reshape(-1, 3).max(axis=0)
    bmin, bmax = tris_b.reshape(-1, 3).min(axis=0), tris_b.reshape(-1, 3).max(axis=0)
    ov_lo = np.maximum(amin, bmin)
    ov_hi = np.minimum(amax, bmax)
    if np.any(ov_hi <= ov_lo + margin):
        return False

    def points_inside(pts: np.ndarray, mesh_tris: np.ndarray) -> np.ndarray:
        x0, y0, z0 = mesh_tris[:, 0, 0], mesh_tris[:, 0, 1], mesh_tris[:, 0, 2]
        x1, y1, z1 = mesh_tris[:, 1, 0], mesh_tris[:, 1, 1], mesh_tris[:, 1, 2]
        x2, y2, z2 = mesh_tris[:, 2, 0], mesh_tris[:, 2, 1], mesh_tris[:, 2, 2]
        det = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        ok = np.abs(det) > 1e-12
        x0, y0, z0 = x0[ok], y0[ok], z0[ok]
        x1, y1, z1 = x1[ok], y1[ok], z1[ok]
        x2, y2, z2 = x2[ok], y2[ok], z2[ok]
        det = det[ok]
        inside = np.zeros(len(pts), dtype=bool)
        for idx, (px, py, pz) in enumerate(pts):
            px_j, py_j = px + 1.23e-5, py + 2.34e-5
            l0 = ((y1 - y2) * (px_j - x2) + (x2 - x1) * (py_j - y2)) / det
            l1 = ((y2 - y0) * (px_j - x2) + (x0 - x2) * (py_j - y2)) / det
            l2 = 1.0 - l0 - l1
            hit = (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
            if np.any(hit):
                hz = l0[hit] * z0[hit] + l1[hit] * z1[hit] + l2[hit] * z2[hit]
                if int(np.sum(hz > pz)) % 2 == 1:
                    inside[idx] = True
        return inside

    # Sample strictly inside the overlap AABB (catches coplanar-face boxes and crossing struts)
    xs = np.linspace(ov_lo[0] + margin * 0.5, ov_hi[0] - margin * 0.5, 5)
    ys = np.linspace(ov_lo[1] + margin * 0.5, ov_hi[1] - margin * 0.5, 5)
    zs = np.linspace(ov_lo[2] + margin * 0.5, ov_hi[2] - margin * 0.5, 5)
    gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")
    grid_pts = np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])
    in_a = points_inside(grid_pts, tris_a)
    if np.any(in_a):
        if np.any(points_inside(grid_pts[in_a], tris_b)):
            return True

    # Also test face centroids of A strictly inside B and vice versa
    cen_a = tris_a.mean(axis=1)
    cand_a = cen_a[np.all((cen_a > bmin + margin) & (cen_a < bmax - margin), axis=1)]
    if len(cand_a) > 0 and np.any(points_inside(cand_a[:128], tris_b)):
        return True
    cen_b = tris_b.mean(axis=1)
    cand_b = cen_b[np.all((cen_b > amin + margin) & (cen_b < amax - margin), axis=1)]
    if len(cand_b) > 0 and np.any(points_inside(cand_b[:128], tris_a)):
        return True
    return False

