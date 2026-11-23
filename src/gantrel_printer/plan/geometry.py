"""Vectorized ray tests on triangle meshes (numpy only), and rotations.

Work is done in chunks so no temporary array grows past CHUNK elements. Rays are never tested against
every triangle: vertical rays are bucketed on an xy grid and each triangle meets only the rays in the
cells its bounding box covers; other rays first pass a bounding-sphere test. Only the pairs that could
hit get the exact test, so the cost follows the pairs that matter, not rays x triangles.
"""

from __future__ import annotations

import math

import numpy as np

CHUNK = 2_000_000          # max rays x triangles per numpy step
EDGE_EPS = 1e-9            # barycentric tolerance: a ray on a shared edge counts for both facets, then deduped
SAME_Z_MM = 1e-6           # hits closer than this on one column are one hit
COLUMNS = 4096             # vertical rays for a cross-section profile (64 x 64 on a square part)
# Grid points are nudged off round coordinates so they never land exactly on a mesh edge.
JITTER = (0.1234567, 0.2345678)


def rotation_to(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The rotation matrix that turns unit vector a onto unit vector b (Rodrigues)."""
    a = np.asarray(a, float) / np.linalg.norm(a)
    b = np.asarray(b, float) / np.linalg.norm(b)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if c < -1 + 1e-12:  # opposite: turn half way round any axis perpendicular to a
        axis = np.cross(a, [1.0, 0.0, 0.0])
        if np.linalg.norm(axis) < 1e-6:
            axis = np.cross(a, [0.0, 1.0, 0.0])
        axis /= np.linalg.norm(axis)
        return 2 * np.outer(axis, axis) - np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx / (1 + c)


def rot_z(deg: float) -> np.ndarray:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def euler_xyz_deg(r: np.ndarray) -> list[float]:
    """Angles (degrees) to rotate about X, then Y, then Z (fixed axes) that give r. For slicer rotate boxes."""
    sy = -r[2, 0]
    if abs(sy) < 1 - 1e-9:
        y = math.asin(sy)
        x = math.atan2(r[2, 1], r[2, 2])
        z = math.atan2(r[1, 0], r[0, 0])
    else:
        y = math.copysign(math.pi / 2, sy)
        x = math.atan2(-r[1, 2], r[1, 1])
        z = 0.0
    return [round(math.degrees(v), 3) + 0.0 for v in (x, y, z)]


def axis_angle_deg(r: np.ndarray) -> tuple[list[float], float]:
    ang = math.acos(max(-1.0, min(1.0, (np.trace(r) - 1) / 2)))
    if ang < 1e-9:
        return [0.0, 0.0, 1.0], 0.0
    if abs(ang - math.pi) < 1e-6:
        w, v = np.linalg.eigh((r + np.eye(3)) / 2)
        axis = v[:, int(np.argmax(w))]
    else:
        axis = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]]) / (2 * math.sin(ang))
    return [round(float(a), 6) + 0.0 for a in axis], round(math.degrees(ang), 3)


def _xy_bbox(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a, b, c = tris[:, 0, :2], tris[:, 1, :2], tris[:, 2, :2]
    return np.minimum(np.minimum(a, b), c), np.maximum(np.maximum(a, b), c)


def vertical_hits(points: np.ndarray, tris: np.ndarray) -> list[np.ndarray]:
    """For each (x, y) point, the sorted z heights where a vertical line through it crosses the triangles."""
    pid, zs = _hits(points, tris)
    bounds = np.searchsorted(pid, np.arange(len(points) + 1))
    return [zs[bounds[i]:bounds[i + 1]] for i in range(len(points))]


def _batches(weights: np.ndarray, limit: int):
    """Slices of consecutive items whose weights add up to at most limit (at least one item each)."""
    csum = np.cumsum(weights)
    i, n = 0, len(weights)
    while i < n:
        base = csum[i - 1] if i else 0
        j = max(i + 1, int(np.searchsorted(csum, base + limit, side="right")))
        yield slice(i, j)
        i = j


def _expand(counts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For items with these counts: each slot's item index, and its rank within the item."""
    owner = np.repeat(np.arange(len(counts)), counts)
    rank = np.arange(len(owner)) - np.repeat(np.cumsum(counts) - counts, counts)
    return owner, rank


def _hits(points: np.ndarray, tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Every crossing as (point index, z), sorted by point then z, repeated heights on one column removed."""
    points = np.asarray(points, float)
    none = (np.empty(0, np.int64), np.empty(0))
    if len(tris) == 0 or len(points) == 0:
        return none
    x0, y0 = tris[:, 0, 0], tris[:, 0, 1]
    x1, y1 = tris[:, 1, 0], tris[:, 1, 1]
    x2, y2 = tris[:, 2, 0], tris[:, 2, 1]
    det = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    tid = np.nonzero(np.abs(det) > 1e-12)[0]              # vertical facets never block a vertical line
    if len(tid) == 0:
        return none
    lo = np.column_stack([np.minimum(np.minimum(x0[tid], x1[tid]), x2[tid]),
                          np.minimum(np.minimum(y0[tid], y1[tid]), y2[tid])])
    hi = np.column_stack([np.maximum(np.maximum(x0[tid], x1[tid]), x2[tid]),
                          np.maximum(np.maximum(y0[tid], y1[tid]), y2[tid])])
    # Bucket the points on a grid of about one cell per point over their own xy box.
    plo, phi = points.min(axis=0), points.max(axis=0)
    span = np.maximum(phi - plo, 1e-9)
    nx = max(1, min(len(points), int(round(math.sqrt(len(points) * span[0] / span[1])))))
    ny = max(1, int(round(len(points) / nx)))
    cw = span / np.array([nx, ny])
    pcx = np.clip(((points[:, 0] - plo[0]) / cw[0]).astype(np.int64), 0, nx - 1)
    pcy = np.clip(((points[:, 1] - plo[1]) / cw[1]).astype(np.int64), 0, ny - 1)
    pcell = pcx * ny + pcy
    porder = np.argsort(pcell, kind="stable")
    cell_start = np.searchsorted(pcell[porder], np.arange(nx * ny + 1))
    # Each triangle covers the cells under its bounding box, widened a hair so a point on its edge counts.
    margin = 1e-6 * max(1.0, float(np.abs(points).max()), float(np.abs(lo).max()), float(np.abs(hi).max()))
    inside_box = (hi[:, 0] >= plo[0] - margin) & (lo[:, 0] <= phi[0] + margin) \
        & (hi[:, 1] >= plo[1] - margin) & (lo[:, 1] <= phi[1] + margin)
    tid, lo, hi = tid[inside_box], lo[inside_box], hi[inside_box]
    if len(tid) == 0:
        return none
    tx0 = np.clip(np.floor((lo[:, 0] - margin - plo[0]) / cw[0]), 0, nx - 1).astype(np.int64)
    tx1 = np.clip(np.floor((hi[:, 0] + margin - plo[0]) / cw[0]), 0, nx - 1).astype(np.int64)
    ty0 = np.clip(np.floor((lo[:, 1] - margin - plo[1]) / cw[1]), 0, ny - 1).astype(np.int64)
    ty1 = np.clip(np.floor((hi[:, 1] + margin - plo[1]) / cw[1]), 0, ny - 1).astype(np.int64)
    ncy = ty1 - ty0 + 1
    ncell = (tx1 - tx0 + 1) * ncy
    z0, z1, z2 = tris[:, 0, 2], tris[:, 1, 2], tris[:, 2, 2]
    hit_ids: list[np.ndarray] = []
    hit_z: list[np.ndarray] = []
    for tb in _batches(ncell, CHUNK):
        own, r = _expand(ncell[tb])
        own += tb.start
        cell = (tx0[own] + r // ncy[own]) * ny + (ty0[own] + r % ncy[own])
        cnt = cell_start[cell + 1] - cell_start[cell]
        keep = cnt > 0
        own, cell, cnt = own[keep], cell[keep], cnt[keep]
        for pb in _batches(cnt, CHUNK):
            o2, r2 = _expand(cnt[pb])
            ti = own[pb][o2]
            pi = porder[cell_start[cell[pb][o2]] + r2]
            j = tid[ti]
            px, py = points[pi, 0], points[pi, 1]
            d = det[j]
            l0 = ((y1[j] - y2[j]) * (px - x2[j]) + (x2[j] - x1[j]) * (py - y2[j])) / d
            l1 = ((y2[j] - y0[j]) * (px - x2[j]) + (x0[j] - x2[j]) * (py - y2[j])) / d
            l2 = 1 - l0 - l1
            inside = (l0 >= -EDGE_EPS) & (l1 >= -EDGE_EPS) & (l2 >= -EDGE_EPS)
            if inside.any():
                j, l0, l1, l2 = j[inside], l0[inside], l1[inside], l2[inside]
                hit_ids.append(pi[inside])
                hit_z.append(l0 * z0[j] + l1 * z1[j] + l2 * z2[j])
    if not hit_ids:
        return none
    pid = np.concatenate(hit_ids)
    zs = np.concatenate(hit_z)
    order = np.lexsort((zs, pid))
    pid, zs = pid[order], zs[order]
    # Hits on a shared edge show up once per facet: keep one when the same column repeats a height.
    keep = np.ones(len(zs), bool)
    keep[1:] = (pid[1:] != pid[:-1]) | (np.diff(zs) > SAME_Z_MM)
    return pid[keep], zs[keep]


def highest_below(points: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """For each 3D point, the highest z where the triangles cross the vertical line under it, or 0 (the bed)."""
    points = np.asarray(points, float)
    res = np.zeros(len(points))
    pid, zs = _hits(points[:, :2], tris)
    below = zs < points[pid, 2] - SAME_Z_MM
    np.maximum.at(res, pid[below], zs[below])
    return res


def ray_distances(origins: np.ndarray, dirs: np.ndarray, tris: np.ndarray, min_t: float = 1e-6) -> np.ndarray:
    """Distance along each ray to the nearest triangle (Moller-Trumbore), inf when it hits nothing.

    A triangle is tested exactly only when the ray's line passes within its bounding sphere (centroid and
    farthest corner) and the sphere is not wholly behind the origin."""
    origins = np.asarray(origins, float)
    dirs = np.asarray(dirs, float)
    best = np.full(len(origins), np.inf)
    if len(tris) == 0 or len(origins) == 0:
        return best
    v0 = tris[:, 0]
    e1 = tris[:, 1] - v0
    e2 = tris[:, 2] - v0
    # The sphere test runs in coordinates centered on the part so the expanded squares stay accurate.
    mid = (tris.reshape(-1, 3).min(axis=0) + tris.reshape(-1, 3).max(axis=0)) / 2
    cen = tris.mean(axis=1) - mid
    rad = np.sqrt(((tris - tris.mean(axis=1)[:, None, :]) ** 2).sum(axis=2).max(axis=1))
    o = origins - mid
    dn = dirs / np.where(np.linalg.norm(dirs, axis=1) > 0, np.linalg.norm(dirs, axis=1), 1.0)[:, None]
    scale = max(1.0, float(np.abs(cen).max()), float(np.abs(o).max()))
    tol = 1e-6 * scale
    cc = np.einsum("ij,ij->i", cen, cen)
    oo = np.einsum("ij,ij->i", o, o)
    od = np.einsum("ij,ij->i", o, dn)
    reach = (rad + tol) ** 2
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    step = max(1, CHUNK // len(origins))
    for s in range(0, len(tris), step):
        c = cen[s:s + step]
        along = c @ dn.T - od[None, :]                                 # (c - o) . d
        dist2 = cc[s:s + step, None] + oo[None, :] - 2 * (c @ o.T) - along ** 2
        near = (dist2 <= reach[s:s + step, None] + tol * scale) & (along >= -(rad[s:s + step, None] + tol))
        ti, ri = np.nonzero(near)
        rows.append(ri)
        cols.append(ti + s)
    ri, j = np.concatenate(rows), np.concatenate(cols)
    for s in range(0, len(ri), CHUNK):
        r, jj = ri[s:s + CHUNK], j[s:s + CHUNK]
        d = dirs[r]
        pvec = np.cross(d, e2[jj])
        det = np.einsum("ij,ij->i", pvec, e1[jj])
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        tvec = origins[r] - v0[jj]
        u = np.einsum("ij,ij->i", tvec, pvec) * inv
        qvec = np.cross(tvec, e1[jj])
        v = np.einsum("ij,ij->i", d, qvec) * inv
        t = np.einsum("ij,ij->i", qvec, e2[jj]) * inv
        hit = ok & (u >= -EDGE_EPS) & (v >= -EDGE_EPS) & (u + v <= 1 + EDGE_EPS) & (t > min_t)
        np.minimum.at(best, r[hit], t[hit])
    return best


def column_grid(lo: np.ndarray, hi: np.ndarray, columns: int = COLUMNS) -> tuple[np.ndarray, float]:
    """Points on a grid over the xy box, about `columns` of them, and the area each one stands for."""
    w, d = max(hi[0] - lo[0], 1e-6), max(hi[1] - lo[1], 1e-6)
    nx = max(1, int(round(math.sqrt(columns * w / d))))
    ny = max(1, int(round(columns / nx)))
    dx, dy = w / nx, d / ny
    xs = lo[0] + (np.arange(nx) + 0.5 + JITTER[0] * 0.01) * dx
    ys = lo[1] + (np.arange(ny) + 0.5 + JITTER[1] * 0.01) * dy
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    return np.column_stack([gx.ravel(), gy.ravel()]), dx * dy


def layer_areas(tris: np.ndarray, layer_z: np.ndarray, columns: int = COLUMNS) -> np.ndarray:
    """Cross-section area (mm2) of a closed mesh at each height, from a grid of vertical rays.

    Each column's crossings, sorted, pair up as solid intervals (in, out). A mesh that is not
    closed can leave an odd crossing; that last one is ignored.
    """
    pts = tris.reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    grid, cell = column_grid(lo, hi, columns)
    pid, zs = _hits(grid, tris)
    diff = np.zeros(len(layer_z) + 1)
    if len(pid):
        bounds = np.searchsorted(pid, np.arange(len(grid) + 1))
        count = np.diff(bounds)
        rank = np.arange(len(pid)) - bounds[pid]
        usable = rank < (count - count % 2)[pid]          # an odd last crossing (open mesh) is dropped
        s_ = zs[usable & (rank % 2 == 0)]
        e_ = zs[usable & (rank % 2 == 1)]
        np.add.at(diff, np.searchsorted(layer_z, s_, side="left"), cell)
        np.add.at(diff, np.searchsorted(layer_z, e_, side="left"), -cell)
    return np.cumsum(diff)[:-1]
