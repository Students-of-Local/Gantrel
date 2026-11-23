"""Vectorized ray tests on triangle meshes (numpy only), and rotations.

Work is done in chunks so no temporary array grows past CHUNK elements, and triangles are first
filtered by their xy bounding box against each chunk of rays.
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
    return tris[:, :, :2].min(axis=1), tris[:, :, :2].max(axis=1)


def vertical_hits(points: np.ndarray, tris: np.ndarray) -> list[np.ndarray]:
    """For each (x, y) point, the sorted z heights where a vertical line through it crosses the triangles."""
    pid, zs = _hits(points, tris)
    bounds = np.searchsorted(pid, np.arange(len(points) + 1))
    return [zs[bounds[i]:bounds[i + 1]] for i in range(len(points))]


def _hits(points: np.ndarray, tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Every crossing as (point index, z), sorted by point then z, repeated heights on one column removed."""
    points = np.asarray(points, float)
    none = (np.empty(0, np.int64), np.empty(0))
    if len(tris) == 0 or len(points) == 0:
        return none
    lo, hi = _xy_bbox(tris)
    x0, y0, z0 = tris[:, 0, 0], tris[:, 0, 1], tris[:, 0, 2]
    x1, y1, z1 = tris[:, 1, 0], tris[:, 1, 1], tris[:, 1, 2]
    x2, y2, z2 = tris[:, 2, 0], tris[:, 2, 1], tris[:, 2, 2]
    det = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    flat = np.abs(det) > 1e-12                           # vertical facets never block a vertical line
    order = np.lexsort((points[:, 1], points[:, 0]))      # neighbors together so the bbox filter bites
    hit_ids: list[np.ndarray] = []
    hit_z: list[np.ndarray] = []
    step = max(1, min(len(points), CHUNK // max(1, len(tris))))
    step = max(step, 16)
    for s in range(0, len(points), step):
        ids = order[s:s + step]
        p = points[ids]
        pl, ph = p.min(axis=0), p.max(axis=0)
        sel = flat & (hi[:, 0] >= pl[0]) & (lo[:, 0] <= ph[0]) & (hi[:, 1] >= pl[1]) & (lo[:, 1] <= ph[1])
        if not sel.any():
            continue
        idx = np.nonzero(sel)[0]
        for c in range(0, len(idx), max(1, CHUNK // len(p))):
            j = idx[c:c + max(1, CHUNK // len(p))]
            px, py = p[:, 0:1], p[:, 1:2]
            d = det[j]
            l0 = ((y1[j] - y2[j]) * (px - x2[j]) + (x2[j] - x1[j]) * (py - y2[j])) / d
            l1 = ((y2[j] - y0[j]) * (px - x2[j]) + (x0[j] - x2[j]) * (py - y2[j])) / d
            l2 = 1 - l0 - l1
            inside = (l0 >= -EDGE_EPS) & (l1 >= -EDGE_EPS) & (l2 >= -EDGE_EPS)
            z = l0 * z0[j] + l1 * z1[j] + l2 * z2[j]
            rows, cols = np.nonzero(inside)
            if len(rows):
                hit_ids.append(ids[rows])
                hit_z.append(z[rows, cols])
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
    """Distance along each ray to the nearest triangle (Moller-Trumbore), inf when it hits nothing."""
    origins = np.asarray(origins, float)
    dirs = np.asarray(dirs, float)
    best = np.full(len(origins), np.inf)
    if len(tris) == 0:
        return best
    v0 = tris[:, 0]
    e1 = tris[:, 1] - v0
    e2 = tris[:, 2] - v0
    step = max(1, CHUNK // len(tris))
    for s in range(0, len(origins), step):
        o = origins[s:s + step, None, :]
        d = dirs[s:s + step, None, :]
        pvec = np.cross(d, e2[None])
        det = np.einsum("ijk,jk->ij", pvec, e1)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        tvec = o - v0[None]
        u = np.einsum("ijk,ijk->ij", tvec, pvec) * inv
        qvec = np.cross(tvec, e1[None])
        v = np.einsum("ijk,ijk->ij", np.broadcast_to(d, qvec.shape), qvec) * inv
        t = np.einsum("ijk,jk->ij", qvec, e2) * inv
        hit = ok & (u >= -EDGE_EPS) & (v >= -EDGE_EPS) & (u + v <= 1 + EDGE_EPS) & (t > min_t)
        t = np.where(hit, t, np.inf)
        best[s:s + step] = t.min(axis=1)
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
