"""Reading and writing meshes: round trips, bad files refused, limits enforced."""

import io
import struct
import zipfile

import numpy as np
import pytest

import shapes
from gantrel_printer.plan import mesh
from gantrel_printer.plan.mesh import MeshError


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
    if extra_object:
        obj += obj.replace('id="1"', 'id="2"')
    xml = ('<?xml version="1.0" encoding="UTF-8"?><model unit="' + unit
           + '" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>' + obj
           + '</resources><build><item objectid="1"/></build></model>')
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
    assert any("2 mesh objects" in w for w in two.warnings)


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
