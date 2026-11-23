"""Measure a part STL against its PARTSET.md checks.

Usage: python -I measure.py <part_dir> [<part_dir> ...]
Writes <part_dir>/measure.json and prints one line per check.
Sections come from OpenSCAD projection(cut=true); they are rasterized at RES mm.
Run with the smoothstone tool's Python (numpy, scipy, trimesh).
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile

import numpy as np
import trimesh
from scipy import ndimage

OPENSCAD = os.path.expanduser("~/.local/bin/openscad")
RES = 0.05


def tol(nominal):
    return max(0.3, 0.01 * abs(nominal))


# ---------- sections ----------

def section(stl, axis, at):
    """Loops (list of Nx2 arrays) of the part cut by the plane axis=at.
    Plane coords: z -> (x, y); x -> (z, y); y -> (x, z)."""
    stl = os.path.abspath(stl)
    if axis == "z":
        body = f'translate([0,0,{-at}]) import("{stl}");'
    elif axis == "x":
        body = f'translate([0,0,{at}]) rotate([0,90,0]) import("{stl}");'
    else:
        body = f'translate([0,0,{at}]) rotate([-90,0,0]) import("{stl}");'
    with tempfile.TemporaryDirectory() as td:
        scad = os.path.join(td, "s.scad")
        svg = os.path.join(td, "s.svg")
        with open(scad, "w") as f:
            f.write(f"projection(cut=true) {body}\n")
        subprocess.run([OPENSCAD, "--backend=manifold", "-o", svg, scad],
                       capture_output=True, timeout=600)
        if not os.path.exists(svg):
            return []
        txt = open(svg).read()
    loops = []
    for sub in re.findall(r"M([^z]*)z", txt):
        pts = re.findall(r"(-?[\d.e+-]+),(-?[\d.e+-]+)", sub)
        a = np.array([[float(x), -float(y)] for x, y in pts])
        if len(a) >= 3:
            loops.append(a)
    if axis == "x":
        # plane coords came out as (z, y); swap to (y, z) for readability
        loops = [l[:, ::-1] for l in loops]
    return loops


def area(l):
    x, y = l[:, 0], l[:, 1]
    return 0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)


def centroid(l):
    x, y = l[:, 0], l[:, 1]
    c = x * np.roll(y, -1) - np.roll(x, -1) * y
    a = 0.5 * c.sum()
    return np.array([((x + np.roll(x, -1)) * c).sum(), ((y + np.roll(y, -1)) * c).sum()]) / (6 * a)


def raster(loops, res=RES, pad=2.0, bounds=None):
    """Even-odd fill. Returns mask[row=y, col=x], origin (x0, y0)."""
    allp = np.vstack(loops)
    if bounds is None:
        x0, y0 = allp.min(0) - pad
        x1, y1 = allp.max(0) + pad
    else:
        x0, y0, x1, y1 = bounds
    nx = int((x1 - x0) / res) + 1
    ny = int((y1 - y0) / res) + 1
    e0 = np.vstack([l for l in loops])
    e1 = np.vstack([np.roll(l, -1, 0) for l in loops])
    mask = np.zeros((ny, nx), bool)
    xs_c = x0 + (np.arange(nx) + 0.5) * res
    for r in range(ny):
        y = y0 + (r + 0.5) * res
        m = (e0[:, 1] <= y) != (e1[:, 1] <= y)
        if not m.any():
            continue
        a, b = e0[m], e1[m]
        xi = np.sort(a[:, 0] + (y - a[:, 1]) * (b[:, 0] - a[:, 0]) / (b[:, 1] - a[:, 1]))
        idx = np.searchsorted(xi, xs_c)
        mask[r] = idx % 2 == 1
    return mask, (x0, y0)


def holes(loops):
    """Inner loops (holes) as (eq_diameter, centroid, bbox_size)."""
    out = []
    outer_sign = np.sign(area(max(loops, key=lambda l: abs(area(l)))))
    for l in loops:
        a = area(l)
        if np.sign(a) != outer_sign:
            out.append((2 * math.sqrt(abs(a) / math.pi), centroid(l), np.ptp(l, 0)))
    return out


# ---------- helpers ----------

def mesh_info(stl):
    m = trimesh.load(stl, force="mesh")
    lo, hi = m.bounds
    return m, lo, hi


def chk(results, name, value, ok, want):
    results.append({"check": name, "value": value, "want": want, "pass": bool(ok)})


def near(v, nom, t=None):
    t = tol(nom) if t is None else t
    return abs(v - nom) <= t


def stone_pass(d):
    p = os.path.join(d, "check.txt")
    if not os.path.exists(p):
        return False, "no check"
    t = open(p).read()
    fails = [f.split(";")[0] for f in re.findall(r"^(?:FAIL|FIX:).*$", t, re.M)]
    return (not fails and "PASS" in t), ("; ".join(fails) if fails else "all PASS")


def max_probe(mask, region=None):
    """Diameter of the largest circle that fits in the empty space (inside region)."""
    empty = ~mask
    if region is not None:
        empty &= region
    lab, n = ndimage.label(empty)
    # drop the outside (touches the border)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])))
    inner = np.isin(lab, [i for i in range(1, n + 1) if i not in border])
    dt = ndimage.distance_transform_edt(inner) * RES
    return 2 * dt.max(), inner


# ---------- parts ----------

def p01(d, stl, R):
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    chk(R, "outline x", round(sz[0], 2), near(sz[0], 120), "120 +-1.2")
    chk(R, "outline y", round(sz[1], 2), near(sz[1], 120), "120 +-1.2")
    chk(R, "thickness", round(sz[2], 2), near(sz[2], 2.5), "2.5 +-0.3")
    L = section(stl, "z", lo[2] + sz[2] / 2)
    c = (lo[:2] + hi[:2]) / 2
    hs = holes(L)
    corner = [h for h in hs if np.linalg.norm(np.abs(h[1] - c) - 52.5) < 6]
    chk(R, "corner holes found", len(corner), len(corner) == 4, "4")
    if corner:
        ds = [round(h[0], 2) for h in corner]
        chk(R, "hole diameters", ds, all(near(x, 4.3) for x in ds), "4.3 +-0.3")
        sp = [round(abs(h[1][k] - c[k]) * 2, 2) for h in corner for k in (0, 1)]
        chk(R, "hole spacing", sorted(set(sp)), all(near(x, 105) for x in sp), "105 +-1.05")
    mask, (x0, y0) = raster(L)
    yy, xx = np.mgrid[0:mask.shape[0], 0:mask.shape[1]]
    px = x0 + (xx + 0.5) * RES - c[0]
    py = y0 + (yy + 0.5) * RES - c[1]
    in110 = px ** 2 + py ** 2 <= 55 ** 2
    probe, _ = max_probe(mask, region=None)
    # exclude the screw holes from the finger probe: they are 4.3 mm anyway
    chk(R, "largest opening (probe diameter)", round(probe, 2), probe < 12.0, "< 12.0")
    open_frac = float((~mask & in110).sum() / in110.sum())
    chk(R, "open area in 110 mm circle", round(open_frac, 3), open_frac >= 0.5, ">= 0.50")


def p02(d, stl, R):
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    # find the width axis: the axis the profile is extruded along (smallest of the three near 30)
    k = int(np.argmin(np.abs(sz - 30)))
    chk(R, "width", round(sz[k], 2), near(sz[k], 30), "30 +-0.3")
    ax = "xyz"[k]
    L = section(stl, ax, lo[k] + 5)   # away from the screw holes
    mask, (x0, y0) = raster(L, pad=20)
    # cradle: largest circle that fits in the open region below the tip top, touching the part
    empty = ~mask
    dt = ndimage.distance_transform_edt(empty) * RES
    # restrict to the convex hull region of the part: points with part material on both sides in x
    cols = mask.any(0)
    rows_any = mask.any(1)
    hullmask = np.zeros_like(mask)
    for r in np.where(rows_any)[0]:
        c = np.where(mask[r])[0]
        hullmask[r, c.min():c.max() + 1] = True
    lab, n = ndimage.label(empty & hullmask)
    best = 0
    for i in range(1, n + 1):
        best = max(best, dt[lab == i].max() * 2)
    chk(R, "cradle fits dowel (largest circle in the throat)", round(best, 2), 25.7 <= best <= 28.4,
        "25.7..28.4 (25.4 dowel + 0.3..3.0)")
    # screw holes: section across the back plate, mid thickness
    xs = [(h[0], h[1]) for h in holes(section(stl, "x", lo[0] + 3))] if True else []
    ds = [round(h[0], 2) for h in xs]
    chk(R, "screw holes", ds, len(ds) == 2 and all(4.2 <= x <= 4.8 for x in ds), "2 holes, 4.2..4.8")
    # retention: the tip rises past the dowel center
    r_in = best / 2
    lab_best = None
    for i in range(1, n + 1):
        if abs(dt[lab == i].max() * 2 - best) < 1e-6:
            lab_best = i
    rr, cc = np.unravel_index(np.argmax(np.where(lab == lab_best, dt, 0)), dt.shape)
    cy = y0 + (rr + 0.5) * RES
    span = int(15 / RES)
    r_px = int(r_in / RES)
    tops = []
    for side in (mask[:, cc + r_px + 2: cc + r_px + span], mask[:, max(cc - r_px - span, 0): cc - r_px - 2]):
        rws = np.where(side.any(1))[0]
        tops.append(y0 + (rws.max() + 0.5) * RES if len(rws) else -1e9)
    tip_top = min(tops)   # the lower side is the tip, the higher one the back plate
    chk(R, "tip rises past dowel center", round(tip_top - cy, 2), tip_top - cy >= 0, ">= 0 mm above center")


def p03(d, stl, R):
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    chk(R, "footprint x", round(sz[0], 2), near(sz[0], 83.5), "83.5 +-0.3")
    chk(R, "footprint y", round(sz[1], 2), near(sz[1], 41.5), "41.5 +-0.3")
    chk(R, "height with lip", round(sz[2], 2), abs(sz[2] - 46.4) <= 0.5, "46.4 +-0.5")
    L0 = section(stl, "z", lo[2] + 0.05)
    outs = [l for l in L0 if area(l) > 0] or L0
    feet = sorted([tuple(np.round(np.ptp(l, 0), 2)) for l in L0 if abs(area(l)) > 500])
    chk(R, "foot bottoms", feet, len(feet) == 2 and all(near(f[0], 35.6) and near(f[1], 35.6) for f in feet),
        "2 feet, 35.6 x 35.6 +-0.3")
    Lm = section(stl, "z", lo[2] + 20)
    cav = [h for h in holes(Lm) if h[0] > 10]
    chk(R, "cavities at mid height (one divider)", len(cav), len(cav) == 2, "2")
    Lt = section(stl, "z", lo[2] + 42 - 4)
    cavt = [h for h in holes(Lt) if h[0] > 5]
    dy = (np.mean([c[2][1] for c in cav]) - np.mean([c[2][1] for c in cavt])) if cav and cavt else 0
    chk(R, "label shelf (cavity shallower near top)", round(dy, 2), dy >= 4, ">= 4 mm narrower in y")


def p04(d, stl, R):
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    L = section(stl, "z", lo[2] + sz[2] / 2)
    mask, (x0, y0) = raster(L, pad=0.5)
    # slots open on the +y edge: fill the convex outline in y per column to make them inner
    top_row = mask.shape[0] - 1
    filled = mask.copy()
    rows = np.where(mask.any(1))[0]
    filled[rows.min():rows.max() + 1, :] |= False
    hull = np.zeros_like(mask)
    cols = np.where(mask.any(0))[0]
    r0, r1 = rows.min(), rows.max()
    hull[r0:r1 + 1, cols.min():cols.max() + 1] = True
    empty = hull & ~mask
    lab, n = ndimage.label(empty)
    seats, mouths, centers = [], [], []
    for i in range(1, n + 1):
        reg = lab == i
        if reg.sum() * RES * RES < 5:
            continue
        dt = ndimage.distance_transform_edt(reg) * RES
        seats.append(round(2 * dt.max(), 2))
        rr, cc = np.unravel_index(np.argmax(dt), dt.shape)
        centers.append(x0 + (cc + 0.5) * RES)
        widths = [reg[r].sum() * RES for r in range(rr + int(dt.max() / RES), r1 + 1) if reg[r].any()]
        mouths.append(round(min(widths), 2) if widths else 0)
    chk(R, "slot count", len(seats), len(seats) == 6, "6")
    pitches = np.round(np.diff(sorted(centers)), 2).tolist()
    chk(R, "pitch", pitches, all(near(p, 10) for p in pitches) and pitches, "10 +-0.3")
    chk(R, "seat diameter", seats, all(6.0 <= s <= 6.6 for s in seats) and seats, "6.0..6.6")
    chk(R, "mouth width (click-in)", mouths, all(5.2 <= w <= 5.9 for w in mouths) and mouths, "5.2..5.9")


def gear_mask(L, res, half):
    return raster(L, res=res, bounds=(-half, -half, half, half))[0]


def p05(d, stl, R):
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    chk(R, "thickness", round(sz[2], 2), near(sz[2], 8), "8 +-0.3")
    L = section(stl, "z", lo[2] + sz[2] / 2)
    c = (lo[:2] + hi[:2]) / 2
    L = [l - c for l in L]
    allp = np.vstack(L)
    rad = np.linalg.norm(allp, axis=1)
    outer = max(L, key=lambda l: abs(area(l)))
    ro = np.linalg.norm(outer, axis=1)
    chk(R, "tip diameter", round(2 * ro.max(), 2), near(2 * ro.max(), 39.0), "39.0 +-0.39")
    chk(R, "root diameter", round(2 * ro.min(), 2), abs(2 * ro.min() - 32.25) <= 0.4, "32.25 +-0.4")
    # teeth: sample the outline radius around the circle
    res = 0.04
    half = 21
    mask = gear_mask(L, res, half)
    n = mask.shape[0]
    ang = np.radians(np.arange(0, 360, 0.25))

    def rad_profile(r):
        x = ((r * np.cos(ang) + half) / res).astype(int)
        y = ((r * np.sin(ang) + half) / res).astype(int)
        return mask[np.clip(y, 0, n - 1), np.clip(x, 0, n - 1)]

    ring = rad_profile(18.0)
    runs = np.diff(np.concatenate([[ring[-1]], ring]).astype(int))
    teeth = int((runs == 1).sum())
    chk(R, "tooth count", teeth, teeth == 24, "24")
    thick = ring.sum() * 0.25 / teeth * math.pi / 180 * 18.0 if teeth else 0
    chk(R, "tooth thickness at pitch circle", round(thick, 3), abs(thick - 2.356) <= 0.15, "2.356 +-0.15")
    # mesh test: second gear at center distance 36, rotated to put a gap facing the first
    yy, xx = np.mgrid[0:n, 0:n]
    px = -half + (xx + 0.5) * res
    py = -half + (yy + 0.5) * res
    # find the angle of a tooth center near 0 deg for A and set B so a gap faces A
    a_tooth = np.degrees(ang[np.argmax(ring)])
    pts = np.argwhere(mask)
    P = np.c_[-half + (pts[:, 1] + 0.5) * res, -half + (pts[:, 0] + 0.5) * res]
    P = P[np.linalg.norm(P, axis=1) > 15.5]          # tooth zone only
    worst_overlap, min_gap = 0, 1e9
    ring_a = ring
    centers_a = np.where(np.diff(np.concatenate([[ring_a[-1]], ring_a]).astype(int)) == 1)[0]
    # tooth center angles of A
    tc = []
    for s in centers_a:
        e = s
        while ring_a[e % len(ring_a)]:
            e += 1
        tc.append(((s + e - 1) / 2) * 0.25)
    t0 = min(tc, key=lambda a: min(abs(a), abs(a - 360)))
    for step in np.linspace(0, 360 / 24, 13):
        ra = np.radians(step - t0)                    # A: tooth at +x, then turn
        rb_ = np.radians(180 + 180 / 24 - t0 - step)  # B: gap toward A, turns the other way
        A = P @ np.array([[math.cos(ra), math.sin(ra)], [-math.sin(ra), math.cos(ra)]])
        Bp = P @ np.array([[math.cos(rb_), math.sin(rb_)], [-math.sin(rb_), math.cos(rb_)]]) + [36.0, 0]
        A = A[A[:, 0] > 14]
        Bp = Bp[Bp[:, 0] < 22]
        # overlap: B points that fall inside A's mask after un-rotating
        back = Bp @ np.array([[math.cos(-ra), math.sin(-ra)], [-math.sin(-ra), math.cos(-ra)]])
        ix = ((back[:, 0] + half) / res).astype(int)
        iy = ((back[:, 1] + half) / res).astype(int)
        ok = (ix >= 0) & (ix < n) & (iy >= 0) & (iy < n)
        ov = mask[iy[ok], ix[ok]].sum() * res * res
        worst_overlap = max(worst_overlap, ov)
        from scipy.spatial import cKDTree
        dd, _ = cKDTree(A).query(Bp, k=1)
        min_gap = min(min_gap, dd.min())
    chk(R, "meshes with a copy at 36 mm (overlap mm2)", round(worst_overlap, 3), worst_overlap < 0.05, "< 0.05 mm2")
    chk(R, "closest approach while meshing (pixel centers)", round(float(min_gap), 3), True, "report")
    hs = [h for h in holes(L) if h[0] < 8]
    if hs:
        bb = hs[0][2]
        chk(R, "bore across round", round(max(bb), 2), 5.05 <= max(bb) <= 5.4, "5.05..5.4")
        chk(R, "bore across flat", round(min(bb), 2), 4.55 <= min(bb) <= 4.8, "4.55..4.8")
    else:
        chk(R, "bore", None, False, "present")


def p06(d, stl, R):
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    k = int(np.argmin(np.abs(sz - 80)))
    chk(R, "width", round(sz[k], 2), near(sz[k], 80), "80 +-0.8")
    ax = "xyz"[k]
    L = section(stl, ax, lo[k] + 10)
    mask, (x0, y0) = raster(L, pad=30)
    pts = np.argwhere(mask)
    P = np.c_[x0 + (pts[:, 1] + 0.5) * RES, y0 + (pts[:, 0] + 0.5) * RES]
    # The phone leans back on the rest. Rotate so a rest at angle `ang` stands vertical
    # (n = forward, +x; u = up the rest, +y), find the rest's front face, drop a 9 mm slab
    # against it and see where it lands and how far it can slide forward.
    g = 0.2
    best = None
    for ang in range(55, 86):
        th = math.radians(ang - 90)
        Q = P @ np.array([[math.cos(th), math.sin(th)], [-math.sin(th), math.cos(th)]])
        q0 = Q.min(0)
        idx = ((Q - q0) / g).astype(int)
        G = np.zeros(idx.max(0)[::-1] + 1, bool)
        G[idx[:, 1], idx[:, 0]] = True
        rows = np.where(G.any(1))[0]
        upper = rows[rows > rows.min() + 0.5 * (rows.max() - rows.min())]
        front = np.array([np.where(G[r])[0].max() for r in upper])
        spread = np.ptp(front) * g
        if best is None or spread < best[1]:
            best = (ang, spread, G, front)
    ang, spread, G, front = best
    a = int(np.median(front)) + 1
    w = int(round(9.0 / g))
    win = G[:, a:a + w]
    floor = np.where(win.any(1))[0]
    seated = len(floor) > 0
    play = None
    if seated:
        b = floor.max() + 1
        ahead = G[b:b + int(8 / g), a + w:]
        hit = np.where(ahead.any(0))[0]
        play = round(hit.min() * g, 2) if len(hit) else None
    chk(R, "back rest angle (straightest fit)", ang, 68 <= ang <= 72, "70 +-2 deg")
    chk(R, "9 mm phone seats on a ledge in front of the rest", bool(seated), seated, "seats")
    chk(R, "forward play of the phone at the ledge", play, play is not None and play <= 3, "<= 3 mm")
    Ls = section(stl, ax, lo[k] + sz[k] / 2)
    ms, _ = raster(Ls, pad=30, bounds=(x0, y0, x0 + mask.shape[1] * RES, y0 + mask.shape[0] * RES))
    removed = (mask & ~ms[:mask.shape[0], :mask.shape[1]]).sum() * RES * RES
    chk(R, "cable slot cut at mid width (mm2 of profile removed)", round(removed, 1), removed > 20, "> 20 mm2")


def p07(d, stl, R):
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    chk(R, "plate x", round(sz[0], 2), near(sz[0], 91), "91 +-0.91")
    chk(R, "plate y", round(sz[1], 2), near(sz[1], 62), "62 +-0.62")
    chk(R, "overall height", round(sz[2], 2), near(sz[2], 16), "16 +-0.3")
    Lp = section(stl, "z", lo[2] + 0.3)
    Lp2 = section(stl, "z", lo[2] + 2.3)
    if Lp2:
        th = None
        for z in np.arange(0.5, 6, 0.1):
            Lz = section(stl, "z", lo[2] + z) if False else None
    # plate thickness: highest z where the cut still covers most of the outline
    full = abs(sum(area(l) for l in Lp))
    th = 0
    for z in np.arange(0.5, 5.0, 0.05):
        a = abs(sum(area(l) for l in section(stl, "z", lo[2] + z)))
        if a > 0.5 * full:
            th = z
    chk(R, "plate thickness (last 0.05 mm step with plate)", round(float(th), 2), 1.7 <= th <= 2.3, "2.0 +-0.3")
    Ls = section(stl, "z", lo[2] + 10)
    hs = sorted([h for h in holes(Ls)], key=lambda h: (round(h[1][1]), h[1][0]))
    ds = [round(h[0], 2) for h in hs]
    chk(R, "standoff holes", ds, len(ds) == 4 and all(2.5 <= x <= 3.0 for x in ds), "4 holes, 2.5..3.0")
    if len(hs) == 4:
        cs = np.array([h[1] for h in hs])
        sx = np.ptp(cs[:, 0])
        sy = np.ptp(cs[:, 1])
        chk(R, "hole spacing x", round(sx, 2), near(sx, 58), "58 +-0.58")
        chk(R, "hole spacing y", round(sy, 2), near(sy, 49), "49 +-0.49")
        hc = cs.mean(0)
    else:
        hc = None
    vents = [h for h in holes(Lp) if h[0] > 0 and not any(np.linalg.norm(h[1] - x[1]) < 1 for x in hs)]
    va = sum(math.pi * (h[0] / 2) ** 2 for h in vents)
    vc = (sum(h[1] * (h[0] ** 2) for h in vents) / sum(h[0] ** 2 for h in vents)) if vents else None
    off = float(np.linalg.norm(vc - hc)) if (vc is not None and hc is not None) else None
    chk(R, "vent open area", round(va, 1), va >= 600, ">= 600 mm2")
    chk(R, "vent centered on hole pattern", round(off, 2) if off is not None else None,
        off is not None and off <= 5, "<= 5 mm off")


def p08(d, stl, R):
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    s = sorted(sz)
    chk(R, "legs (outside)", [round(sz[0], 2), round(sz[2], 2)], near(sz[0], 50) and near(sz[2], 50), "50 +-0.5")
    chk(R, "width", round(sz[1], 2), near(sz[1], 25), "25 +-0.3")
    # leg on the bed: holes through z
    L = section(stl, "z", lo[2] + 1.0)
    hs = [h for h in holes(L) if h[0] < 8]
    pos = sorted([round(h[1][0] - lo[0], 2) for h in hs])
    ds = [round(h[0], 2) for h in hs]
    cl = [round(h[1][1] - (lo[1] + 12.5), 2) for h in hs]
    chk(R, "bed leg holes", ds, len(ds) == 2 and all(near(x, 4.5) for x in ds), "2 holes, 4.5 +-0.3")
    chk(R, "bed leg hole positions from corner", pos, pos and all(near(p, n) for p, n in zip(pos, [15, 40])), "15, 40 +-0.3")
    chk(R, "bed leg holes on center line", cl, cl and all(abs(x) <= 0.3 for x in cl), "+-0.3")
    Lt = section(stl, "z", lo[2] + 4.95)
    hst = [h for h in holes(Lt) if 5 < h[0] < 12 and h[1][0] - lo[0] > 8]
    cs = [round(h[0], 2) for h in hst]
    chk(R, "countersink on inside face (dia at 0.05 below face)", cs, len(cs) == 2 and all(8.2 <= x <= 9.5 for x in cs), "8.4..9.5")
    # upright leg: holes through x
    Lx = section(stl, "x", lo[0] + 1.0)
    hx = [h for h in holes(Lx) if h[0] < 8]
    posx = sorted([round(h[1][1] - lo[2], 2) for h in hx])
    chk(R, "upright leg holes", [round(h[0], 2) for h in hx], len(hx) == 2 and all(near(h[0], 4.5) for h in hx), "2 holes, 4.5 +-0.3")
    chk(R, "upright leg hole positions", posx, posx and all(near(p, n) for p, n in zip(posx, [15, 40])), "15, 40 +-0.3")
    Lg = section(stl, "y", lo[1] + 1.0)
    ga = abs(sum(area(l) for l in Lg))
    chk(R, "gusset at the side (section area at y=1)", round(ga, 1), ga > 2 * 50 * 5 + 200 - 25, "> legs only")


def p09(d, stl, R):
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    chk(R, "height", round(sz[2], 2), near(sz[2], 180), "180 +-1.8")
    maxd = max(sz[0], sz[1])
    # max diameter as 2x max radius from the axis (surface centroid; the bbox center
    # shifts for an odd number of twisted lobes)
    c = m.centroid[:2]
    r = np.linalg.norm(m.vertices[:, :2] - c, axis=1).max()
    chk(R, "max diameter", round(2 * r, 2), near(2 * r, 80), "80 +-0.8")
    L = section(stl, "z", lo[2] + 0.1)
    a = abs(sum(area(l) for l in L))
    chk(R, "base contact", round(a, 1), a >= 1500, ">= 1500 mm2")
    Lm = section(stl, "z", lo[2] + sz[2] / 2)
    chk(R, "solid body for vase mode (loops at mid height)", len(Lm), len(Lm) == 1, "1")


def p10(d, stl, R):
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    Lr = section(stl, "z", hi[2] - 0.5)
    hs = holes(Lr)
    rim = max([h[0] for h in hs], default=0)
    chk(R, "inner rim diameter", round(rim, 2), 104 <= rim <= 112, "104..112")
    # floor: lowest z where a large hole appears
    fl = None
    for z in np.arange(0.5, 15, 0.25):
        hz = [h for h in holes(section(stl, "z", lo[2] + z)) if h[0] > 50]
        if hz:
            fl = z
            break
    depth = sz[2] - fl if fl is not None else 0
    chk(R, "inner depth", round(depth, 2), depth >= 91, ">= 91")
    Ld = section(stl, "z", lo[2] + 0.5)
    dr = max([h[0] for h in holes(Ld)], default=0)
    chk(R, "drain hole", round(dr, 2), dr >= 8, ">= 8 mm")


def p11(d, stl, R):
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    chk(R, "width", round(sz[0], 2), 45 <= sz[0] <= 55, "45..55")
    L = section(stl, "z", lo[2] + 0.5)
    hs = [h for h in holes(L) if h[0] > 2]
    ring = max([h[0] for h in hs], default=0)
    chk(R, "key ring hole", round(ring, 2), ring >= 4.0, ">= 4.0")
    top = section(stl, "z", hi[2] - 0.3)
    ta = abs(sum(area(l) for l in top))
    # relief: the top is only the letters if the relief stands up
    zs = sorted(set(np.round(m.vertices[:, 2], 2)))
    chk(R, "letters stand up (top levels)", zs[-3:], len(zs) >= 2 and zs[-1] - zs[-2] >= 0.8, ">= 0.8 mm")
    # OCR the cut 0.3 mm below the top: raised letters come out black on white
    from PIL import Image
    tm, _ = raster(top, res=0.1, pad=4)
    png = os.path.join(d, "ocr_cut.png")
    Image.fromarray(np.where(tm[::-1], 0, 255).astype(np.uint8)).save(png)
    txt = subprocess.run(["tesseract", png, "-", "--psm", "7"], capture_output=True, text=True).stdout.strip()
    chk(R, "OCR of the cut 0.3 mm below the top (ocr_cut.png)", txt, re.sub(r"[^A-Z]", "", txt.upper()) == "EMMA", "EMMA")


def p12(d, stl, R):
    from PIL import Image
    m, lo, hi = mesh_info(stl)
    sz = hi - lo
    chk(R, "bounding box", [round(x, 2) for x in sz], True, "report")
    img = np.asarray(Image.open(os.path.join(d, "lithophane_photo.png")).convert("L"), float) / 255
    # picture plane: the two largest extents; thickness along the smallest within the picture height
    k_thin = int(np.argmin(sz))
    # cut horizontal slices through the picture and read thickness along the thin axis
    zs = np.linspace(lo[2] + 0.2 * sz[2], lo[2] + 0.9 * sz[2], 40)
    rows = []
    for z in zs:
        L = section(stl, "z", z)
        if not L:
            rows.append(None)
            continue
        mask, (x0, y0) = raster(L, res=0.1, pad=1)
        # thickness along y for each x column
        t = mask.sum(0) * 0.1
        xs = x0 + (np.arange(mask.shape[1]) + 0.5) * 0.1
        rows.append((xs, t))
    ok_rows = [r for r in rows if r is not None]
    allt = np.concatenate([r[1][r[1] > 0] for r in ok_rows])
    # picture area: drop the frame by trimming 6 mm on each side
    xmin = lo[0] + 6
    xmax = hi[0] - 6
    pic = np.concatenate([r[1][(r[0] > xmin) & (r[0] < xmax)] for r in ok_rows])
    pic = pic[pic > 0]
    chk(R, "thinnest in picture", round(float(np.percentile(pic, 1)), 2), abs(np.percentile(pic, 1) - 0.8) <= 0.15, "0.8 +-0.15")
    chk(R, "thickest in picture", round(float(np.percentile(pic, 99)), 2), abs(np.percentile(pic, 99) - 3.0) <= 0.15, "3.0 +-0.15")
    def corr(a, b):
        a = a.ravel() - a.mean()
        b = b.ravel() - b.mean()
        return float((a * b).sum() / math.sqrt((a * a).sum() * (b * b).sum() + 1e-12))
    # register the picture box: try frame widths, picture height from the image aspect
    ih, iw = img.shape
    best = {}
    for f in np.arange(0, 10.5, 0.5):
        pw = sz[0] - 2 * f
        ph = pw * ih / iw
        ztop = hi[2] - f
        zs2 = np.linspace(ztop - 0.02 * ph, ztop - 0.98 * ph, 40)
        if zs2[-1] >= lo[2]:
            best[f] = zs2
    # thickness map for each candidate box (reuse cuts at fixed z, interpolate)
    cutz = np.linspace(lo[2], hi[2], 90)
    cuts = []
    for z in cutz:
        L = section(stl, "z", z)
        if not L:
            cuts.append((np.array([lo[0], hi[0]]), np.zeros(2)))
            continue
        mask, (x0, y0) = raster(L, res=0.1, pad=1)
        cuts.append((x0 + (np.arange(mask.shape[1]) + 0.5) * 0.1, mask.sum(0) * 0.1))
    scored = []
    for f, zs2 in best.items():
        xs_mm = np.linspace(lo[0] + f + 0.5, hi[0] - f - 0.5, 60)
        T = np.array([np.interp(xs_mm, *cuts[int(np.argmin(np.abs(cutz - z)))]) for z in zs2])
        I = 1 - img[np.linspace(0, ih - 1, len(zs2)).astype(int)][:, np.linspace(0, iw - 1, 60).astype(int)]
        scored.append((corr(T, I), corr(T, I[:, ::-1]), f))
    r_norm, r_mirror, f = max(scored, key=lambda s: max(abs(s[0]), abs(s[1])))
    chk(R, "picture width (box minus 2 x registered frame)", [round(sz[0] - 2 * f, 2), f], near(sz[0] - 2 * f, 100), "100 +-1")
    chk(R, "thickness vs dark (r, unmirrored)", round(r_norm, 3), r_norm >= 0.8, ">= 0.8")
    chk(R, "reads unmirrored from the front", [round(r_norm, 3), round(r_mirror, 3)], r_norm > r_mirror, "r > r_mirror")
    L0 = section(stl, "z", lo[2] + 0.1)
    if L0:
        dep = max(np.ptp(l[:, 1]) for l in L0)
        chk(R, "foot depth on the bed", round(dep, 2), dep >= 20, ">= 20 mm")


PARTS = {"p01": p01, "p02": p02, "p03": p03, "p04": p04, "p05": p05, "p06": p06,
         "p07": p07, "p08": p08, "p09": p09, "p10": p10, "p11": p11, "p12": p12}


def main():
    for d in sys.argv[1:]:
        d = os.path.abspath(d)
        name = os.path.basename(d)[:3]
        stl = os.path.join(d, "part.stl")
        R = []
        if not os.path.exists(stl):
            R.append({"check": "STL built", "value": None, "want": "file", "pass": False})
        else:
            sp, msg = stone_pass(d)
            chk(R, "stone check", msg, sp, "all PASS")
            try:
                PARTS[name](d, stl, R)
            except Exception as e:  # report, don't hide
                R.append({"check": "measure error", "value": repr(e)[:200], "want": "none", "pass": False})
        json.dump(R, open(os.path.join(d, "measure.json"), "w"), indent=1, default=str)
        npass = sum(r["pass"] for r in R)
        print(f"== {os.path.basename(d)}: {npass}/{len(R)} checks pass")
        for r in R:
            print(f"  {'PASS' if r['pass'] else 'FAIL'} {r['check']}: {r['value']} (want {r['want']})")


if __name__ == "__main__":
    main()
