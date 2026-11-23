"""Reading and writing meshes: round trips, bad files refused, limits enforced."""

import io
import struct
import zipfile

import numpy as np
import pytest

import shapes
from gantrel_printer import materials, slicing
from gantrel_printer.plan import mesh, orient
from gantrel_printer.plan.mesh import MeshError

UP = np.array([0.0, 0.0, -1.0])   # the file's own bottom faces the bed
MUSHROOM_MM3 = 10 * 10 * 20 + 30 * 30 * 5


def _upright_support(m):
    return orient.evaluate(m, UP, slicing.PRINTERS["creality-k2"], materials.get("PLA"))


def test_binary_round_trip(tmp_path):
    m = shapes.l_bracket()
    p = mesh.write_stl(tmp_path / "l.stl", m)
    back = mesh.load(p)
    assert back.kind == "stl-binary" and back.name == "l.stl"
    assert np.allclose(back.tris, m.tris, atol=1e-4)
    assert back.volume == pytest.approx(9000, rel=1e-6)
    assert back.watertight()["watertight"] and not back.warnings


def test_ascii_round_trip(tmp_path):
    m = shapes.cylinder()
    p = tmp_path / "c.stl"
    p.write_text(mesh.ascii_stl(m.tris, "cyl"))
    back = mesh.load(p)
    assert back.kind == "stl-ascii"
    assert np.allclose(back.tris, m.tris, atol=1e-5)
    assert back.volume == pytest.approx(m.volume, rel=1e-6)


def test_binary_with_a_solid_header_is_read_as_binary():
    m = shapes.cube()
    data = mesh.stl_bytes(m.tris, header="solid exported by a CAD program")
    assert mesh.read_stl(data).kind == "stl-binary"


@pytest.mark.parametrize("data", [
    b"",
    b"not a mesh at all, just text",
    b"\x00" * 83,
    b"\x00" * 80 + struct.pack("<I", 5) + b"\x00" * 50,              # says 5 triangles, has 1
    b"solid x\n facet normal 0 0 1\n outer loop\n vertex 0 0 0\n vertex 1 0 0\n endloop\n endfacet\nendsolid\n",
    b"solid x\n facet normal 0 0 1\n outer loop\n vertex 0 0 0\n vertex 1 0 0\n vertex a b c\n endloop\n endfacet\n",
    b"solid x\nendsolid x\n",
])
def test_malformed_stl_is_refused(data):
    with pytest.raises(MeshError):
        mesh.read_stl(data)


def test_zero_triangles_and_non_finite_are_refused():
    with pytest.raises(MeshError, match="no triangles"):
        mesh.read_stl(b"\x00" * 80 + struct.pack("<I", 0))
    t = shapes.cube().tris.copy()
    t[0, 0, 0] = np.nan
    with pytest.raises(MeshError, match="finite"):
        mesh.read_stl(mesh.stl_bytes(t))


def test_size_and_triangle_limits(tmp_path, monkeypatch):
    p = mesh.write_stl(tmp_path / "c.stl", shapes.cube())
    monkeypatch.setattr(mesh, "MAX_BYTES", 500)
    with pytest.raises(MeshError, match="limit"):
        mesh.load(p)
    monkeypatch.setattr(mesh, "MAX_BYTES", 200 * 1024 * 1024)
    monkeypatch.setattr(mesh, "MAX_TRIANGLES", 5)
    with pytest.raises(MeshError, match="triangles"):
        mesh.load(p)


def test_wrong_extension_and_missing_file(tmp_path):
    (tmp_path / "x.obj").write_text("v 0 0 0")
    with pytest.raises(MeshError, match="STL or 3MF"):
        mesh.load(tmp_path / "x.obj")
    with pytest.raises(MeshError, match="no file"):
        mesh.load(tmp_path / "missing.stl")


def test_degenerate_facets_dropped_and_open_mesh_reported():
    t = shapes.cube().tris
    flat = np.array([[[0, 0, 0], [1, 1, 1], [2, 2, 2]]], float)
    m = mesh.read_stl(mesh.stl_bytes(np.concatenate([t, flat])))
    assert m.removed_degenerate == 1 and len(m.tris) == 12
    opened = mesh.read_stl(mesh.stl_bytes(t[:-1]))
    assert not opened.watertight()["watertight"]
    assert any("not watertight" in w for w in opened.warnings)


def test_inward_winding_is_turned_right_way_out():
    """Some exporters wind every facet inward: the part is watertight but its volume is negative and every
    normal points in, so an upright mushroom's cap would read as facing up and need no supports."""
    m = mesh.read_stl(mesh.stl_bytes(shapes.mushroom().tris[:, [0, 2, 1]]))
    assert m.volume == pytest.approx(MUSHROOM_MM3)
    assert any("faced inward" in w for w in m.warnings)
    fresh = mesh.Mesh(m.tris.copy())    # the welding and edges carried over match ones worked out again
    assert np.array_equal(m.faces, fresh.faces)
    assert all(np.array_equal(a, b) for a, b in zip(m.edges, fresh.edges))
    c = _upright_support(m)
    assert c.needs_support
    assert c.support_volume_mm3 == pytest.approx(shapes.MUSHROOM_UPRIGHT_SUPPORT_MM3, rel=0.15)
    # A mesh already facing out is left alone, and an open one is not judged by its volume.
    assert not any("faced inward" in w for w in mesh.read_stl(mesh.stl_bytes(shapes.mushroom().tris)).warnings)
    opened = mesh.read_stl(mesh.stl_bytes(shapes.mushroom().tris[:-1, [0, 2, 1]]))
    assert not any("faced inward" in w for w in opened.warnings)


def test_unit_warnings():
    small = mesh.read_stl(mesh.stl_bytes(shapes.cube().tris / 1000))
    assert any("meters or inches" in w for w in small.warnings)
    big = mesh.read_stl(mesh.stl_bytes(shapes.cube().tris * 1000))
    assert any("microns" in w for w in big.warnings)


def _3mf(path, verts, tris, unit="millimeter", extra_object=False, rels=True):
    obj = ('<object id="1" type="model"><mesh><vertices>'
           + "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
           + "</vertices><triangles>"
           + "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
           + "</triangles></mesh></object>")
    items = '<item objectid="1"/>'
    if extra_object:
        obj += obj.replace('id="1"', 'id="2"')
        items += '<item objectid="2"/>'
    xml = ('<?xml version="1.0" encoding="UTF-8"?><model unit="' + unit
           + '" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>' + obj
           + '</resources><build>' + items + '</build></model>')
    with zipfile.ZipFile(path, "w") as z:
        if rels:
            z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                       'package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" '
                       'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", xml)
    return path


def _indexed(m):
    verts, idx = np.unique(m.tris.reshape(-1, 3), axis=0, return_inverse=True)
    return verts.tolist(), idx.reshape(-1, 3).tolist()


def test_3mf_reads_the_first_object_with_units(tmp_path):
    verts, tris = _indexed(shapes.cube(1.0))
    m = mesh.load(_3mf(tmp_path / "c.3mf", verts, tris, unit="inch"))
    assert m.kind == "3mf" and m.volume == pytest.approx(25.4 ** 3, rel=1e-9)
    two = mesh.load(_3mf(tmp_path / "two.3mf", verts, tris, extra_object=True, rels=False))
    assert any("build has 2 objects" in w for w in two.warnings)


def test_bad_3mf_is_refused(tmp_path, monkeypatch):
    (tmp_path / "notzip.3mf").write_bytes(b"hello")
    with pytest.raises(MeshError, match="zip"):
        mesh.load(tmp_path / "notzip.3mf")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "no model")
    (tmp_path / "empty.3mf").write_bytes(buf.getvalue())
    with pytest.raises(MeshError, match="no 3D model"):
        mesh.load(tmp_path / "empty.3mf")
    with pytest.raises(MeshError, match="does not exist"):
        mesh.load(_3mf(tmp_path / "bad.3mf", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 7]]))
    with pytest.raises(MeshError, match="unknown unit"):
        mesh.load(_3mf(tmp_path / "u.3mf", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]], unit="furlong"))
    verts, tris = _indexed(shapes.cube())
    big = _3mf(tmp_path / "big.3mf", verts, tris)
    monkeypatch.setattr(mesh, "MAX_BYTES", 300)   # the model part uncompressed is larger than this
    with pytest.raises(MeshError, match="uncompressed"):
        mesh.read_3mf(big)


def test_transformed_drops_to_the_bed_and_centers():
    m = shapes.cube().transformed(np.eye(3), center_on=(100, 50))
    lo, hi = m.bbox
    assert lo[2] == pytest.approx(0) and (lo[:2] + hi[:2]) / 2 == pytest.approx([100, 50])


def test_golden_meshes_are_small_and_watertight():
    for f in (shapes.cube, shapes.l_bracket, shapes.mushroom, shapes.arch, shapes.pin, shapes.long_bar, shapes.cylinder):
        m = f()
        assert m.watertight()["watertight"], m.name
        assert m.volume > 0, m.name
        assert len(mesh.stl_bytes(m.tris)) < 100 * 1024, m.name


CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
RELS = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')


def _mesh_object(oid, m):
    verts, tris = _indexed(m)
    return (f'<object id="{oid}" type="model"><mesh><vertices>'
            + "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts) + "</vertices><triangles>"
            + "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris) + "</triangles></mesh></object>")


def _model(resources, build=""):
    return (f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{CORE}" xmlns:p="{PROD}" '
            f'requiredextensions="p"><resources>{resources}</resources><build>{build}</build></model>')


def _bambu_3mf(path, root_resources, build, parts):
    """The layout Bambu Studio and OrcaSlicer save: meshes in 3D/Objects/*.model, components in the root."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", _model(root_resources, build))
        for name, resources in parts.items():
            z.writestr(name, _model(resources))
    return path


def test_3mf_components_in_other_parts_with_their_transforms(tmp_path):
    cube = shapes.cube(10.0)
    # Two parts of one object: the cube scaled 2x, and the cube moved 30 mm along x.
    root = ('<object id="3" type="model"><components>'
            '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="2 0 0 0 2 0 0 0 2 0 0 0"/>'
            '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 30 0 0"/>'
            '</components></object>')
    f = _bambu_3mf(tmp_path / "bambu.3mf", root, '<item objectid="3" transform="1 0 0 0 1 0 0 0 1 128 128 0"/>',
                   {"3D/Objects/object_1.model": _mesh_object(1, cube)})
    m = mesh.load(f)
    assert len(m.tris) == 24 and m.volume == pytest.approx(8000 + 1000, rel=1e-9)
    lo, hi = m.bbox
    assert np.allclose(lo, [128, 128, 0]) and np.allclose(hi, [128 + 40, 128 + 20, 20])


def test_3mf_nested_components_compose_inner_first(tmp_path):
    # Object 3 holds object 2 scaled 2x; object 2 holds the 1 mm cube moved 5 mm along x. Inner first: x 10..12.
    res = (_mesh_object(1, shapes.cube(1.0))
           + '<object id="2" type="model"><components><component objectid="1" transform="1 0 0 0 1 0 0 0 1 5 0 0"/>'
             '</components></object>'
           + '<object id="3" type="model"><components><component objectid="2" transform="2 0 0 0 2 0 0 0 2 0 0 0"/>'
             '</components></object>')
    f = _bambu_3mf(tmp_path / "nested.3mf", res, '<item objectid="3"/>', {})
    lo, hi = mesh.load(f).bbox
    assert lo[0] == pytest.approx(10) and hi[0] == pytest.approx(12) and hi[2] == pytest.approx(2)


def test_3mf_component_cycles_escapes_and_missing_parts_are_refused(tmp_path):
    cyc = ('<object id="1" type="model"><components><component objectid="2"/></components></object>'
           '<object id="2" type="model"><components><component objectid="1"/></components></object>')
    with pytest.raises(MeshError, match="contains itself"):
        mesh.load(_bambu_3mf(tmp_path / "cycle.3mf", cyc, '<item objectid="1"/>', {}))
    for ref in ("/../../etc/passwd", "../3D/3dmodel.model/../../x.model", "C:/x.model", "/3D/Objects/missing.model"):
        comp = f'<object id="1" type="model"><components><component p:path="{ref}" objectid="1"/></components></object>'
        with pytest.raises(MeshError, match="(outside the archive|not in it)"):
            mesh.load(_bambu_3mf(tmp_path / "escape.3mf", comp, '<item objectid="1"/>', {}))
    gone = '<object id="1" type="model"><components><component objectid="9"/></components></object>'
    with pytest.raises(MeshError, match="object '9'"):
        mesh.load(_bambu_3mf(tmp_path / "gone.3mf", gone, '<item objectid="1"/>', {}))


def test_3mf_component_totals_are_capped(tmp_path, monkeypatch):
    many = ('<object id="2" type="model"><components>'
            + '<component p:path="/3D/Objects/object_1.model" objectid="1"/>' * 3 + '</components></object>')
    f = _bambu_3mf(tmp_path / "many.3mf", many, '<item objectid="2"/>',
                   {"3D/Objects/object_1.model": _mesh_object(1, shapes.cube())})
    assert len(mesh.load(f).tris) == 36
    monkeypatch.setattr(mesh, "MAX_TRIANGLES", 30)       # each copy alone is under the limit; the total is not
    with pytest.raises(MeshError, match="more than 30 triangles"):
        mesh.load(f)
    monkeypatch.undo()
    with zipfile.ZipFile(f) as z:
        root = z.getinfo("3D/3dmodel.model").file_size
    monkeypatch.setattr(mesh, "MAX_BYTES", root + 100)   # the root part fits; root plus the object part does not
    with pytest.raises(MeshError, match="uncompressed"):
        mesh.read_3mf(f)


def test_plan_reads_a_bambu_style_3mf(tmp_path):
    from gantrel_printer import slicing
    from gantrel_printer.plan import optimize
    root = ('<object id="2" type="model"><components><component p:path="/3D/Objects/object_1.model" objectid="1" '
            'transform="1 0 0 0 1 0 0 0 1 0 0 0"/></components></object>')
    f = _bambu_3mf(tmp_path / "part.3mf", root, '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 128 128 0"/>',
                   {"3D/Objects/object_1.model": _mesh_object(1, shapes.mushroom())})
    p = optimize.plan(mesh.load(f), slicing.PRINTERS["bambu-x1c"], "PLA")
    assert p["check"]["ok"] and p["part"]["volume_mm3"] == pytest.approx(abs(shapes.mushroom().volume), rel=1e-6)


def test_3mf_mirrored_parts_keep_their_facets_facing_out(tmp_path):
    """A part mirrored in Bambu Studio or OrcaSlicer has a transform with a negative determinant. Mirroring
    flips which way every facet winds, so the reader must turn them back. Here only the mushroom is mirrored
    and the cube beside it is bigger, so the total volume stays positive and a whole-mesh check would miss it."""
    root = ('<object id="3" type="model"><components>'
            '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="-1 0 0 0 1 0 0 0 1 30 0 0"/>'
            '<component p:path="/3D/Objects/object_2.model" objectid="2" transform="1 0 0 0 1 0 0 0 1 60 0 0"/>'
            '</components></object>')
    f = _bambu_3mf(tmp_path / "mirrored.3mf", root, '<item objectid="3"/>',
                   {"3D/Objects/object_1.model": _mesh_object(1, shapes.mushroom()),
                    "3D/Objects/object_2.model": _mesh_object(2, shapes.cube(40.0))})
    m = mesh.load(f)
    assert m.volume == pytest.approx(40.0 ** 3 + MUSHROOM_MM3)
    c = _upright_support(m)
    assert c.support_volume_mm3 == pytest.approx(shapes.MUSHROOM_UPRIGHT_SUPPORT_MM3, rel=0.15)
    # The whole build item mirrored (x and y: a determinant of +1 is a turn, not a mirror; x alone is).
    one = _bambu_3mf(tmp_path / "item.3mf", '<object id="3" type="model"><components>'
                     '<component p:path="/3D/Objects/object_1.model" objectid="1"/></components></object>',
                     '<item objectid="3" transform="-1 0 0 0 1 0 0 0 1 128 128 0"/>',
                     {"3D/Objects/object_1.model": _mesh_object(1, shapes.mushroom())})
    m = mesh.load(one)
    assert m.volume == pytest.approx(MUSHROOM_MM3)
    assert not any("faced inward" in w for w in m.warnings)
    assert _upright_support(m).support_volume_mm3 == pytest.approx(shapes.MUSHROOM_UPRIGHT_SUPPORT_MM3, rel=0.15)
