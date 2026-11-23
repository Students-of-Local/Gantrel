"""Golden meshes built in code: unions of axis-aligned boxes, so every support volume has an exact answer.

The union is cut on a grid at every box edge; a cell face between a solid cell and an empty one becomes
two triangles facing out. Shapes here never touch only along an edge, so the result is watertight.
"""

from __future__ import annotations

import math

import numpy as np

from gantrel_printer.plan.mesh import Mesh

Box = tuple[float, float, float, float, float, float]   # x0, x1, y0, y1, z0, z1


def boxes(*parts: Box, name: str = "part") -> Mesh:
    xs = sorted({v for b in parts for v in b[0:2]})
    ys = sorted({v for b in parts for v in b[2:4]})
    zs = sorted({v for b in parts for v in b[4:6]})
    nx, ny, nz = len(xs) - 1, len(ys) - 1, len(zs) - 1
    solid = np.zeros((nx + 2, ny + 2, nz + 2), bool)   # padded with an empty cell on every side
    for i in range(nx):
        for j in range(ny):
            for k in range(nz):
                c = ((xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2, (zs[k] + zs[k + 1]) / 2)
                solid[i + 1, j + 1, k + 1] = any(b[0] < c[0] < b[1] and b[2] < c[1] < b[3] and b[4] < c[2] < b[5]
                                                 for b in parts)
    tris = []
    for i in range(1, nx + 1):
        for j in range(1, ny + 1):
            for k in range(1, nz + 1):
                if not solid[i, j, k]:
                    continue
                x0, x1, y0, y1, z0, z1 = xs[i - 1], xs[i], ys[j - 1], ys[j], zs[k - 1], zs[k]
                if not solid[i - 1, j, k]:
                    tris += _quad((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0))
                if not solid[i + 1, j, k]:
                    tris += _quad((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1))
                if not solid[i, j - 1, k]:
                    tris += _quad((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1))
                if not solid[i, j + 1, k]:
                    tris += _quad((x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0))
                if not solid[i, j, k - 1]:
                    tris += _quad((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0))
                if not solid[i, j, k + 1]:
                    tris += _quad((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1))
    return Mesh(np.array(tris, float), name=name, kind="test")


def _quad(a, b, c, d):
    """Two triangles for the quad a b c d, wound counterclockwise seen from outside."""
    return [[a, b, c], [a, c, d]]


def cube(size: float = 20.0) -> Mesh:
    return boxes((0, size, 0, size, 0, size), name="cube")


def l_bracket() -> Mesh:
    """A 60 x 20 x 5 plate with a 5 x 20 x 30 leg standing on one end."""
    return boxes((0, 60, 0, 20, 0, 5), (0, 5, 0, 20, 5, 35), name="l-bracket")


def mushroom() -> Mesh:
    """A 10 x 10 x 20 stem under a 30 x 30 x 5 cap. Upright, the cap's underside (800 mm2) is 20 mm up."""
    return boxes((10, 20, 10, 20, 0, 20), (0, 30, 0, 30, 20, 25), name="mushroom")


MUSHROOM_UPRIGHT_SUPPORT_MM3 = (30 * 30 - 10 * 10) * 20.0


def arch() -> Mesh:
    """Two 10 x 10 x 20 pillars, 30 mm apart, under a 50 x 10 x 5 slab. The span is 30 mm."""
    return boxes((0, 10, 0, 10, 0, 20), (40, 50, 0, 10, 0, 20), (0, 50, 0, 10, 20, 25), name="arch")


ARCH_UPRIGHT_SUPPORT_MM3 = 30 * 10 * 20.0


def pin() -> Mesh:
    """A 3 x 3 x 40 pin on a 20 x 20 x 2 base: tiny layers when standing."""
    return boxes((0, 20, 0, 20, 0, 2), (8.5, 11.5, 8.5, 11.5, 2, 42), name="pin")


def long_bar() -> Mesh:
    """40 x 230 x 5: fits a 250 x 210 bed only after a quarter turn."""
    return boxes((0, 40, 0, 230, 0, 5), name="long-bar")


def cylinder(radius: float = 10.0, height: float = 20.0, sides: int = 64) -> Mesh:
    """A closed prism with many sides, for meshes that are not box shaped."""
    a = np.linspace(0, 2 * math.pi, sides, endpoint=False)
    ring = np.column_stack([radius * np.cos(a), radius * np.sin(a)])
    tris = []
    for i in range(sides):
        p, q = ring[i], ring[(i + 1) % sides]
        tris.append([(0, 0, 0), (q[0], q[1], 0), (p[0], p[1], 0)])
        tris.append([(0, 0, height), (p[0], p[1], height), (q[0], q[1], height)])
        tris.append([(p[0], p[1], 0), (q[0], q[1], 0), (q[0], q[1], height)])
        tris.append([(p[0], p[1], 0), (q[0], q[1], height), (p[0], p[1], height)])
    return Mesh(np.array(tris, float), name="cylinder", kind="test")


TERRAIN_BED_DOWN = (-0.2962, 0.5, -0.8138)   # where terrain()'s flat bottom faces after its tilt


def terrain_tris(n: int, w: float = 80.0, d: float = 60.0, h: float = 12.0, amp: float = 3.0) -> np.ndarray:
    """A closed block, w x d, with a flat bottom and a wavy top up to h + amp, both faces cut into an n x n
    grid (4 n^2 + 8 n triangles: n = 500 gives about a million). It is tilted 30 degrees about X and 20 about
    Y, so the side that should face the bed (the bottom, TERRAIN_BED_DOWN) is on no axis. Its volume is
    w x d x h exactly: the waves average out."""
    xs, ys = np.linspace(0, w, n + 1), np.linspace(0, d, n + 1)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    top = np.stack([gx, gy, h + amp * np.sin(gx / w * 3 * np.pi) * np.cos(gy / d * 2 * np.pi)], -1)
    bottom = np.stack([gx, gy, np.zeros_like(gx)], -1)

    def grid(p, up):
        a, b, c, e = p[:-1, :-1], p[1:, :-1], p[1:, 1:], p[:-1, 1:]
        pair = ((a, b, c), (a, c, e)) if up else ((a, c, b), (a, e, c))
        return np.concatenate([np.stack(t, -2).reshape(-1, 3, 3) for t in pair])

    def wall(t, b, out):
        a, c, e, f = b[:-1], b[1:], t[1:], t[:-1]
        pair = ((a, c, e), (a, e, f)) if out else ((a, e, c), (a, f, e))
        return np.concatenate([np.stack(q, 1) for q in pair])

    tris = np.concatenate([grid(top, True), grid(bottom, False),
                           wall(top[:, 0], bottom[:, 0], True), wall(top[:, -1], bottom[:, -1], False),
                           wall(top[0, :], bottom[0, :], False), wall(top[-1, :], bottom[-1, :], True)])
    rx, ry = math.radians(30), math.radians(20)
    turn_x = np.array([[1, 0, 0], [0, math.cos(rx), -math.sin(rx)], [0, math.sin(rx), math.cos(rx)]])
    turn_y = np.array([[math.cos(ry), 0, math.sin(ry)], [0, 1, 0], [-math.sin(ry), 0, math.cos(ry)]])
    return tris @ (turn_y @ turn_x).T


def terrain(n: int = 20) -> Mesh:
    return Mesh(terrain_tris(n), name=f"terrain-{n}", kind="test")
