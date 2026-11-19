"""STL (binary or ASCII) to a 3MF package, written with zipfile and nothing else."""

from __future__ import annotations

import io
import re
import struct
import zipfile
from xml.sax.saxutils import escape

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_VERTEX = re.compile(rb"vertex\s+(" + _FLOAT.encode() + rb")\s+(" + _FLOAT.encode() + rb")\s+(" + _FLOAT.encode() + rb")")


class StlProblem(ValueError):
    pass


def is_binary(data: bytes) -> bool:
    if len(data) < 84:
        return False
    n = struct.unpack_from("<I", data, 80)[0]
    return 84 + 50 * n == len(data)


def triangles(data: bytes) -> list[tuple[tuple[float, float, float], ...]]:
    """Every triangle as three (x, y, z) corners."""
    if is_binary(data):
        n = struct.unpack_from("<I", data, 80)[0]
        out = []
        for rec in struct.iter_unpack("<12fH", data[84:84 + 50 * n]):
            out.append((rec[3:6], rec[6:9], rec[9:12]))
        return out
    if data.lstrip()[:5].lower() == b"solid":
        pts = [tuple(float(x) for x in m.groups()) for m in _VERTEX.finditer(data)]
        if len(pts) % 3:
            raise StlProblem("ASCII STL has a facet without three vertices")
        return [tuple(pts[i:i + 3]) for i in range(0, len(pts), 3)]
    raise StlProblem("not a binary or ASCII STL")


def face_count(data: bytes) -> int:
    if is_binary(data):
        return struct.unpack_from("<I", data, 80)[0]
    return len(triangles(data))


def bbox_size(tris) -> list[float]:
    if not tris:
        return [0.0, 0.0, 0.0]
    xs, ys, zs = zip(*(v for t in tris for v in t))
    return [round(max(a) - min(a), 3) for a in (xs, ys, zs)]


def mesh(tris) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Shared vertices and index triangles; triangles that collapse to a line or point are dropped."""
    index: dict[tuple[float, float, float], int] = {}
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for t in tris:
        ids = []
        for v in t:
            key = (float(v[0]), float(v[1]), float(v[2]))
            i = index.get(key)
            if i is None:
                i = index[key] = len(verts)
                verts.append(key)
            ids.append(i)
        if len(set(ids)) == 3:
            faces.append((ids[0], ids[1], ids[2]))
    return verts, faces


def _num(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def model_xml(verts, faces, title: str = "") -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n',
        ' <metadata name="Application">Gantrel Studio</metadata>\n',
    ]
    if title:
        parts.append(f" <metadata name=\"Title\">{escape(title)}</metadata>\n")
    parts.append(' <resources>\n  <object id="1" type="model">\n   <mesh>\n    <vertices>\n')
    parts.extend(f'     <vertex x="{_num(x)}" y="{_num(y)}" z="{_num(z)}"/>\n' for x, y, z in verts)
    parts.append("    </vertices>\n    <triangles>\n")
    parts.extend(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in faces)
    parts.append('    </triangles>\n   </mesh>\n  </object>\n </resources>\n <build>\n  <item objectid="1"/>\n </build>\n</model>\n')
    return "".join(parts)


def stl_to_3mf(data: bytes, title: str = "") -> bytes:
    verts, faces = mesh(triangles(data))
    if not faces:
        raise StlProblem("the STL has no triangles")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", model_xml(verts, faces, title))
    return buf.getvalue()
