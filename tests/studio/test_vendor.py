"""The vendored three.js files are the npm three@0.170.0 release, byte for byte.

They are too large for a model reviewer to read, so this pin is their review: any change to them
fails here. The hashes were checked against the npm tarball for three@0.170.0 on 2026-11-18.
"""

import hashlib
from pathlib import Path

import pytest

VENDOR = Path(__file__).resolve().parents[2] / "src" / "gantrel_studio" / "web" / "vendor" / "three"

PINNED = {
    "three.module.min.js": "08fd7545d13d2c7fb65ab691530a802dafefd638596501854f267d0fb13c39e7",  # build/three.module.min.js
    "OrbitControls.js": "80efaadea4f8a636a65fb0bd08bfef62f3d93a0bb94e2e7500f23176c5c07f4e",  # examples/jsm/controls/
    "STLLoader.js": "a0a83c88b269c94e25b690fae770d350c4728c81853195186976be7af0f8a3b3",  # examples/jsm/loaders/
    "RoomEnvironment.js": "85869bdd22f7cf0b22e68dcefe6cb710480af0f5c901a76f7a7764fd07fc8014",  # examples/jsm/environments/
    "LICENSE": "4c40a1ef62450b857c3b2aaf294936304cd552d965fbcd9d32d4c5bcf4ba4454",
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_vendored_three_matches_the_npm_release(name):
    assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == PINNED[name]


def test_nothing_else_is_vendored():
    assert {p.name for p in VENDOR.iterdir()} == set(PINNED) | {"SOURCE"}
