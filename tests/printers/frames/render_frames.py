"""Render the synthetic guard frames in tests/printers/frames/ with Blender.

    blender -b -P tests/printers/frames/render_frames.py -- tests/printers/frames

Every image comes from this script, so the frames are original and ship under the repo's MIT
license. Each frame is a chamber-camera view of a print bed: good frames show a part being printed
with the nozzle on its top layer; failed frames show spaghetti, a part knocked off the bed or dragged
by the nozzle, or an empty bed while the printer reports a print well under way. labels.json records
the label, the failure kind and the printer status the guard is told for each frame.
"""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Vector

OUT = Path(sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else "tests/printers/frames")

COLORS = {
    "white": (0.85, 0.85, 0.82), "black": (0.03, 0.03, 0.035), "red": (0.65, 0.05, 0.04),
    "blue": (0.05, 0.15, 0.6), "green": (0.08, 0.45, 0.12), "orange": (0.9, 0.35, 0.03),
    "grey": (0.35, 0.36, 0.38), "yellow": (0.85, 0.7, 0.05), "teal": (0.03, 0.4, 0.4),
}


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 48
    sc.cycles.use_denoising = True
    sc.render.resolution_x, sc.render.resolution_y = 640, 360
    sc.render.image_settings.file_format = "JPEG"
    sc.render.image_settings.quality = 88
    sc.view_settings.view_transform = "Standard"
    world = bpy.data.worlds.new("w")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.02, 0.02, 0.025, 1)
    sc.world = world
    return sc


def material(name, color, rough=0.45, layers=False, bed=False):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    bsdf.inputs["Roughness"].default_value = rough
    if layers or bed:
        tc = nt.nodes.new("ShaderNodeTexCoord")
        bump = nt.nodes.new("ShaderNodeBump")
        if layers:  # horizontal layer lines
            tex = nt.nodes.new("ShaderNodeTexWave")
            tex.bands_direction = "Z"
            tex.inputs["Scale"].default_value = 6.0
            bump.inputs["Strength"].default_value = 0.25
        else:  # textured PEI sheet
            tex = nt.nodes.new("ShaderNodeTexNoise")
            tex.inputs["Scale"].default_value = 180.0
            bump.inputs["Strength"].default_value = 0.35
        nt.links.new(tc.outputs["Object"], tex.inputs["Vector"])
        nt.links.new(tex.outputs[0], bump.inputs["Height"])
        nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return m


def box(name, size, loc, mat, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc, rotation=rot)
    o = bpy.context.object
    o.name = name
    o.scale = size
    o.data.materials.append(mat)
    return o


def lathe(name, radius, height, mat, loc=(0, 0, 0), rot=(0, 0, 0), seg=72, rings=48):
    """A closed solid of revolution with radius(theta, z) in cm, base at z=0."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    grid = []
    for i in range(rings + 1):
        z = height * i / rings
        row = []
        for j in range(seg):
            t = 2 * math.pi * j / seg
            r = radius(t, z)
            row.append(bm.verts.new((r * math.cos(t), r * math.sin(t), z)))
        grid.append(row)
    for i in range(rings):
        for j in range(seg):
            a, b = grid[i][j], grid[i][(j + 1) % seg]
            c, d = grid[i + 1][(j + 1) % seg], grid[i + 1][j]
            bm.faces.new((a, b, c, d))
    bm.faces.new(list(reversed(grid[0])))
    bm.faces.new(grid[-1])
    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(o)
    o.location, o.rotation_euler = loc, rot
    o.data.materials.append(mat)
    for p in o.data.polygons:
        p.use_smooth = True
    return o


def strands(name, mat, rng, n, center, spread, zrange, hang_from=None, radius=0.06):
    """Loose filament: random smooth walks, some lying on the bed, some hanging from the nozzle."""
    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "3D"
    cu.bevel_depth = radius
    cu.bevel_resolution = 2
    for k in range(n):
        sp = cu.splines.new("NURBS")
        pts = rng.randint(14, 30)
        sp.points.add(pts - 1)
        if hang_from is not None and k % 3 == 0:
            p = Vector(hang_from)
        else:
            p = Vector((center[0] + rng.uniform(-spread, spread), center[1] + rng.uniform(-spread, spread),
                        rng.uniform(*zrange)))
        d = Vector((rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-0.6, 0.6))).normalized()
        for i in range(pts):
            sp.points[i].co = (p.x, p.y, max(0.05, p.z), 1)
            d = (d + Vector((rng.uniform(-0.9, 0.9), rng.uniform(-0.9, 0.9), rng.uniform(-0.7, 0.5)))).normalized()
            p = p + d * rng.uniform(0.4, 1.1)
            p.x = min(max(p.x, center[0] - spread * 1.6), center[0] + spread * 1.6)
            p.y = min(max(p.y, center[1] - spread * 1.6), center[1] + spread * 1.6)
            p.z = min(max(p.z, 0.05), zrange[1] + 1)
        sp.use_endpoint_u = True
        sp.order_u = 4
    o = bpy.data.objects.new(name, cu)
    bpy.context.collection.objects.link(o)
    o.data.materials.append(mat)
    return o


def printer(rng, nozzle_xy, nozzle_z, bed_gold):
    bed_mat = material("bed", (0.32, 0.24, 0.1) if bed_gold else (0.06, 0.06, 0.07), rough=0.7, bed=True)
    box("bed", (26, 26, 0.4), (0, 0, -0.2), bed_mat)
    metal = material("metal", (0.12, 0.12, 0.13), rough=0.35)
    box("bedframe", (30, 30, 1.0), (0, 0, -0.9), metal)
    wall = material("wall", (0.05, 0.05, 0.055), rough=0.8)
    box("back", (40, 0.5, 40), (0, 17, 15), wall)
    box("left", (0.5, 40, 40), (-17, 0, 15), wall)
    box("right", (0.5, 40, 40), (17, 0, 15), wall)
    x, y = nozzle_xy
    head = material("head", (0.04, 0.04, 0.045), rough=0.4)
    box("gantry", (34, 1.6, 1.6), (0, y, nozzle_z + 5.2), metal)
    box("toolhead", (5.0, 4.5, 6.0), (x, y - 0.6, nozzle_z + 3.6), head)
    brass = material("brass", (0.75, 0.55, 0.2), rough=0.25)
    bpy.ops.mesh.primitive_cone_add(radius1=0.06, radius2=0.35, depth=0.6, location=(x, y, nozzle_z + 0.3))
    bpy.context.object.data.materials.append(brass)
    # Chamber light strip at the top front, plus a soft fill.
    bpy.ops.object.light_add(type="AREA", location=(0, -6, 30))
    lt = bpy.context.object
    lt.data.energy = rng.uniform(3500, 6000)
    lt.data.size = 20
    lt.data.color = (1.0, 0.95, 0.88)
    bpy.ops.object.light_add(type="POINT", location=(-12, -14, 18))
    bpy.context.object.data.energy = rng.uniform(400, 1200)


def camera(rng, target_z):
    bpy.ops.object.camera_add(location=(rng.uniform(-4, 4), rng.uniform(-31, -27), rng.uniform(19, 25)))
    cam = bpy.context.object
    cam.data.lens = rng.uniform(20, 26)
    direction = Vector((rng.uniform(-1.5, 1.5), rng.uniform(-1, 2), target_z)) - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = cam


def part(kind, rng, mat, h, loc=(0, 0, 0), rot=(0, 0, 0)):
    """A printed part of the given kind, built up to height h (cm). Returns its objects."""
    if kind == "box":
        w, d = rng.uniform(4, 8), rng.uniform(4, 8)
        o = box("part", (w, d, h), (0, 0, h / 2), mat)
        return [o]
    if kind == "cylinder":
        r = rng.uniform(2, 4)
        return [lathe("part", lambda t, z: r, h, mat)]
    if kind == "vase":
        r, ribs, tw = rng.uniform(2.5, 4), rng.choice((6, 8, 10)), rng.uniform(0.2, 0.5)
        return [lathe("part", lambda t, z: r * (1 + 0.25 * math.sin(z / 6)) * (1 + 0.07 * math.sin(ribs * t + tw * z)),
                      h, mat)]
    if kind == "gear":
        r, teeth = rng.uniform(3, 5), rng.choice((12, 16, 20))
        return [lathe("part", lambda t, z: r * (1 + 0.08 * (1 if math.sin(teeth * t) > 0 else -1)), h, mat, seg=160)]
    if kind == "grid":
        out = []
        for i in range(2):
            for j in range(3):
                out.append(box(f"part{i}{j}", (2.2, 2.2, h), (-3.5 + 3.5 * j, -2 + 4 * i, h / 2), mat))
        return out
    if kind == "bracket":
        a = box("part_a", (8, 2, h), (0, -2, h / 2), mat)
        b = box("part_b", (2, 6, h), (-3, 1, h / 2), mat)
        return [a, b]
    raise ValueError(kind)


def place(objs, loc, rot):
    """Move and rotate a part's objects together about the bed origin."""
    bpy.ops.object.empty_add(location=(0, 0, 0))
    pivot = bpy.context.object
    for o in objs:
        o.parent = pivot
    pivot.location, pivot.rotation_euler = loc, rot


KINDS = ("box", "cylinder", "vase", "gear", "grid", "bracket")


def scene(i, label, mode, rng):
    reset()
    # A black part knocked over reads as a shadow even to a person, so failure frames use lighter colors.
    color_name = rng.choice(sorted(COLORS) if label == "ok" else sorted(set(COLORS) - {"black"}))
    mat = material("pla", COLORS[color_name], rough=0.4, layers=True)
    kind = KINDS[i % len(KINDS)]
    total = rng.uniform(5, 11)
    if label == "ok":
        frac = rng.uniform(0.15, 0.95)
        h = total * frac
        part(kind, rng, mat, h)
        nozzle = (rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5), h)
        progress = frac
    elif mode == "spaghetti":
        frac = rng.uniform(0.35, 0.9)
        base = total * rng.uniform(0.05, 0.3)
        if rng.random() < 0.7:
            part(kind, rng, mat, base)
        nozzle_z = total * frac
        nozzle = (rng.uniform(-2, 2), rng.uniform(-2, 2), nozzle_z)
        strands("spaghetti", mat, rng, rng.randint(25, 45), (0, 0), rng.uniform(3, 5.5), (0.1, nozzle_z),
                hang_from=(nozzle[0], nozzle[1], nozzle_z))
        if rng.random() < 0.4:  # a blob on the nozzle
            bpy.ops.mesh.primitive_uv_sphere_add(radius=rng.uniform(0.8, 1.4),
                                                 location=(nozzle[0], nozzle[1], nozzle_z - 0.2))
            bpy.context.object.data.materials.append(mat)
        progress = frac
    elif mode == "detached":
        frac = rng.uniform(0.3, 0.8)
        h = total * frac
        objs = part(kind, rng, mat, h)
        style = i % 3
        if style == 0:  # knocked over, lying on its side near the edge
            place(objs, (rng.uniform(-8, 8), rng.uniform(-7, 4), 2.5), (math.pi / 2, 0, rng.uniform(0, 3)))
        elif style == 1:  # torn loose and leaning
            place(objs, (rng.uniform(-6, 6), rng.uniform(-5, 3), 0.6), (rng.uniform(0.5, 0.9), rng.uniform(-0.3, 0.3), 0))
        else:  # dragged along on the nozzle, off the bed
            place(objs, (rng.uniform(-3, 3), 0, h * 0.9), (rng.uniform(0.2, 0.5), 0, 0))
        nozzle = (rng.uniform(-2, 2), rng.uniform(-2, 2), h + (h * 0.9 if style == 2 else 0))
        if rng.random() < 0.6:
            strands("strands", mat, rng, rng.randint(4, 12), (nozzle[0], nozzle[1]), 2.5, (0.1, nozzle[2]),
                    hang_from=nozzle)
        progress = frac
    elif mode == "empty_bed":
        frac = rng.uniform(0.45, 0.8)
        nozzle = (rng.uniform(-2, 2), rng.uniform(-2, 2), total * frac)
        progress = frac
    else:
        raise ValueError(mode)
    printer(rng, nozzle[:2], nozzle[2], bed_gold=rng.random() < 0.5)
    camera(rng, min(nozzle[2], 6) * 0.6)
    layers = 200
    status = {"state": "printing", "progress": round(progress, 2), "layer": int(progress * layers),
              "total_layers": layers}
    return color_name, kind, status


def main():
    plan = [("ok", "printing")] * 12 + [("failed", "spaghetti")] * 7 + [("failed", "detached")] * 6 \
        + [("failed", "empty_bed")] * 2
    labels = []
    for n, (label, mode) in enumerate(plan):
        rng = random.Random(1000 + n)
        color, kind, status = scene(n, label, mode, rng)
        folder = OUT / ("good" if label == "ok" else "failed")
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{'good' if label == 'ok' else mode}-{n:02d}.jpg"
        bpy.context.scene.render.filepath = str(folder / name)
        bpy.ops.render.render(write_still=True)
        labels.append({"file": f"{folder.name}/{name}", "label": label, "kind": mode, "part": kind, "color": color,
                       "status": status, "source": "synthetic, render_frames.py"})
        print(f"rendered {folder.name}/{name}", flush=True)
    (OUT / "labels.synthetic.json").write_text(json.dumps(labels, indent=1) + "\n")


main()
