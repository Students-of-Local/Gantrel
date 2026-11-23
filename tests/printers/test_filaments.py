"""Loaded filament: `gantrel-printer filaments` and Adapter.filaments() against fixtures and fake printers.

Every read here is read-only: the tests also check no fake printer was told to do anything.
"""

from __future__ import annotations

import copy
import json
import socket

import pytest

from gantrel_printer import cli
from gantrel_printer.adapters import ADAPTERS, build
from gantrel_printer.adapters.bambu import ams_slots
from gantrel_printer.adapters.creality import cfs_slots, creality_materials
from gantrel_printer.config import PrinterConfig
from gantrel_printer.errors import ProtocolError, Unsupported
from gantrel_printer.model import FAMILIES, Slot, hex_color, material_family

import fakes
from test_contract import make_rig

# The owner's Creality K2 with one CFS unit, read once, read-only, on 2026-11-22. The read-only tool
# reported the reply as a summary, so this fixture is rebuilt from that summary in the shape it gave:
# box.state "connect", units T1 to T4 with state, arrays indexed A to D for material_type,
# color_value (color in the last 6 characters) and remain_len (percent, as a string), and the unit's
# dry_and_humidity and temperature. Only T1 was connected.
K2_BOX_2026_11_22 = {
    "state": "connect",
    "T1": {"state": "connect",
           "material_type": ["-1", "008001", "-1", "008001"],
           "color_value": ["-1", "0ffffff", "-1", "0000000"],
           "remain_len": ["-1", "100", "-1", "0"],
           "dry_and_humidity": 41, "temperature": 25},
    **{f"T{n}": {"state": "disconnect", "material_type": ["-1"] * 4, "color_value": ["-1"] * 4,
                 "remain_len": ["-1"] * 4} for n in (2, 3, 4)},
}
UNIT_T1 = {"humidity": 41.0, "temperature_c": 25.0}
SLOT_KEYS = {"slot", "empty", "family", "material", "brand", "name", "color", "percent_left", "grams_left",
             "nozzle_min_c", "nozzle_max_c", "source", "extra"}


def reply(box) -> dict:
    return {"result": {"eventtime": 1.0, "status": {"box": box}}}


def rows(slots: list[Slot]) -> list[tuple]:
    return [(s.slot, s.empty, s.family, s.material, s.color, s.percent_left) for s in slots]


@pytest.fixture
def moon(tls_cert):
    rig = make_rig("moonraker", tls_cert)  # with an API key, so every call must carry it
    yield rig
    for c in rig.closers:
        c()


@pytest.fixture
def creality(tls_cert):
    """The stock-firmware adapter, with the K2's Moonraker (a fake) on its own port."""
    rig = make_rig("creality", tls_cert)
    moon_brain = fakes.Brain(secret=None)
    srv = fakes.serve_http(moon_brain, fakes.moonraker_routes)
    rig.cfg.extra["moonraker_port"] = srv.server_address[1]
    rig.moon = moon_brain
    yield rig
    srv.shutdown()
    for c in rig.closers:
        c()


@pytest.fixture
def bambu(tls_cert):
    rig = make_rig("bambu", tls_cert)
    yield rig
    for c in rig.closers:
        c()


# ---------------------------------------------------------------- Creality CFS


def test_k2_real_read_is_reproduced_exactly():
    slots = cfs_slots(reply(K2_BOX_2026_11_22), "k2")
    assert rows(slots) == [
        ("T1A", True, "unknown", "", "", None),
        ("T1B", False, "PLA", "008001", "#ffffff", 100.0),
        ("T1C", True, "unknown", "", "", None),
        ("T1D", False, "PLA", "008001", "#000000", 0.0),
    ]
    assert all(s.extra == UNIT_T1 for s in slots)
    assert [s.source for s in slots] == [f"moonraker box.T1.material_type[{i}]" for i in range(4)]
    # Creality Print does not publish a product name or temperatures for this code, so none is invented.
    assert all((s.brand, s.name, s.nozzle_min_c, s.nozzle_max_c) == ("", "", None, None) for s in slots)


def test_k2_through_the_moonraker_adapter(moon):
    moon.brain.box = copy.deepcopy(K2_BOX_2026_11_22)
    slots = moon.adapter().filaments()
    assert [s.slot for s in slots] == ["T1A", "T1B", "T1C", "T1D"]
    assert slots[1].family == "PLA" and slots[1].color == "#ffffff"
    assert moon.brain.events == [] and moon.brain.proxied == []


def test_k2_through_the_stock_creality_adapter(creality):
    creality.moon.box = copy.deepcopy(K2_BOX_2026_11_22)
    slots = creality.adapter().filaments()
    assert rows(slots) == rows(cfs_slots(reply(K2_BOX_2026_11_22), "k2"))
    assert creality.brain.events == [] and creality.moon.events == []


def test_two_cfs_units_in_order():
    box = copy.deepcopy(K2_BOX_2026_11_22)
    box["T2"] = {"state": "connect", "material_type": ["008001", "-1", "099999", "008001"],
                 "color_value": ["0ff0000", "-1", "000ff00", "#0000ff"], "remain_len": ["55", "-1", "80", "12"],
                 "dry_and_humidity": "38", "temperature": "26"}
    slots = cfs_slots(reply(box), "k2")
    assert [s.slot for s in slots] == ["T1A", "T1B", "T1C", "T1D", "T2A", "T2B", "T2C", "T2D"]
    assert rows(slots)[4:] == [
        ("T2A", False, "PLA", "008001", "#ff0000", 55.0),
        ("T2B", True, "unknown", "", "", None),
        ("T2C", False, "unknown", "099999", "#00ff00", 80.0),
        ("T2D", False, "PLA", "008001", "#0000ff", 12.0),
    ]
    assert slots[4].extra == {"humidity": 38.0, "temperature_c": 26.0}


def test_units_sort_by_number_not_text():
    box = {"state": "connect", **{k: {"state": "connect", "material_type": ["008001"]} for k in ("T10", "T2", "T1")}}
    assert [s.slot for s in cfs_slots(reply(box), "k2")][::4] == ["T1A", "T2A", "T10A"]


def test_unknown_code_is_reported_never_guessed():
    box = {"state": "connect", "T1": {"state": "connect", "material_type": ["101001", "08001", "-1", "-1"]}}
    slots = cfs_slots(reply(box), "k2")
    # 101001 is not in the table, and neither is 08001 (Creality Print's 5-character id for Ender-PLA).
    assert [(s.family, s.material) for s in slots[:2]] == [("unknown", "101001"), ("unknown", "08001")]
    assert set(creality_materials()) == {"008001"}


def test_creality_table_is_only_known_families():
    for code, m in creality_materials().items():
        assert m["family"] in FAMILIES and m["code"] == code


@pytest.mark.parametrize("box", [
    "garbage",
    ["not", "a", "dict"],
    {"state": "connect", "T1": "garbage"},
    {"state": "connect", "T1": {"state": "connect", "material_type": "008001"}},
    {"state": "connect", "T1": {"state": "connect", "material_type": ["008001"], "remain_len": {"a": 1}}},
])
def test_malformed_cfs_reply_is_a_plain_protocol_error(box):
    with pytest.raises(ProtocolError, match="k2 sent CFS"):
        cfs_slots(reply(box), "k2")


@pytest.mark.parametrize("bad", [{}, {"result": "x"}, {"result": {"status": []}}])
def test_reply_without_a_status_is_a_protocol_error(bad):
    with pytest.raises(ProtocolError, match="without a status"):
        cfs_slots(bad, "k2")


def test_untrusted_cfs_fields_are_cleaned():
    long = "\x1b[31m" + "9" * 80
    box = {"state": "connect", "T1": {"state": "connect", "material_type": [long, 8001, "008001", "008001"],
                                      "color_value": ["0zzzzzz", "0ffffff", "0ffffffffff", None],
                                      "remain_len": ["500", "-3", "nan", "1e999"],
                                      "dry_and_humidity": "wet", "temperature": True}}
    slots = cfs_slots(reply(box), "k2")
    assert slots[0].material == "?[31m" + "9" * 11 and slots[0].family == "unknown"
    assert slots[1].material == "" and slots[1].family == "unknown"  # a number is not a Creality code
    assert [s.color for s in slots] == ["", "#ffffff", "", ""]
    assert [s.percent_left for s in slots] == [None, None, None, None]
    assert all(s.extra == {} for s in slots)


def test_absent_box_is_one_external_slot(creality):
    slots = creality.adapter().filaments()  # the fake K2 has no box object
    assert len(slots) == 1 and slots[0].slot == "ext" and not slots[0].empty
    assert slots[0].family == "unknown" and slots[0].source == "moonraker: no box object"


def test_disconnected_box_is_one_external_slot(moon):
    moon.brain.box = {"state": "disconnect"}
    slots = moon.adapter().filaments()
    assert [(s.slot, s.family, s.source) for s in slots] == [("ext", "unknown", "moonraker box.state disconnect")]


def test_oversized_and_non_json_box_replies_are_refused(moon):
    moon.brain.box = b"{" + b" " * (1024 * 1024 + 10) + b"}"
    with pytest.raises(ProtocolError, match="too large"):
        moon.adapter().filaments()
    moon.brain.box = b"<html>nope</html>"
    with pytest.raises(ProtocolError, match="not JSON"):
        moon.adapter().filaments()


def test_stock_adapter_without_moonraker_says_so(tls_cert):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again before the call, so nothing answers
    cfg = PrinterConfig(name="k1", type="creality", host="127.0.0.1", extra={"moonraker_port": port})
    with pytest.raises(Unsupported, match="did not answer on port"):
        build(cfg).filaments()


# ---------------------------------------------------------------- Moonraker and Spoolman

SPOOL = {"id": 3, "remaining_weight": 742.5, "initial_weight": 1000,
         "filament": {"id": 9, "name": "PolyTerra PLA", "material": "PLA", "color_hex": "1A2B3C",
                      "settings_extruder_temp": 210, "vendor": {"id": 2, "name": "Polymaker"}}}


def test_spoolman_active_spool(moon):
    moon.brain.spoolman = {"connected": True, "spool_id": 3, "spools": {3: SPOOL}}
    [slot] = moon.adapter().filaments()
    assert slot.to_dict() == {
        "slot": "ext", "empty": False, "family": "PLA", "material": "PLA", "brand": "Polymaker",
        "name": "PolyTerra PLA", "color": "#1a2b3c", "percent_left": None, "grams_left": 742.5,
        "nozzle_min_c": None, "nozzle_max_c": None, "source": "moonraker spoolman /v1/spool/3",
        "extra": {"spool_id": 3, "extruder_temp_c": 210.0}}
    # The proxy was asked only to GET that spool; the printer was told nothing.
    assert moon.brain.proxied == [{"request_method": "GET", "path": "/v1/spool/3", "use_v2_response": True}]
    assert moon.brain.events == []


@pytest.mark.parametrize("spoolman", [
    None,  # Moonraker without the spoolman component: 404
    {"connected": False, "spool_id": 3, "spools": {3: SPOOL}},
    {"connected": True, "spool_id": None, "spools": {}},
])
def test_without_a_spoolman_spool_one_unknown_slot(moon, spoolman):
    moon.brain.spoolman = spoolman
    [slot] = moon.adapter().filaments()
    assert (slot.slot, slot.empty, slot.family, slot.source) == ("ext", False, "unknown", "moonraker: no Spoolman spool")
    assert moon.brain.proxied == []


def test_spoolman_error_and_bad_spool(moon):
    moon.brain.spoolman = {"connected": True, "spool_id": 4, "spools": {}}
    with pytest.raises(ProtocolError, match="could not read spool 4"):
        moon.adapter().filaments()
    moon.brain.spoolman = {"connected": True, "spool_id": 4, "spools": {4: ["not", "a", "spool"]}}
    with pytest.raises(ProtocolError, match="spool 4 in a shape"):
        moon.adapter().filaments()
    moon.brain.spoolman = {"connected": True, "spool_id": 4,
                           "spools": {4: {"remaining_weight": "lots", "filament": {"material": "Bambu PETG-CF ++",
                                                                                   "color_hex": "#12345"}}}}
    [slot] = moon.adapter().filaments()
    assert (slot.family, slot.material, slot.color, slot.grams_left) == ("other", "Bambu PETG-CF ++", "", None)


# ---------------------------------------------------------------- Bambu AMS

AMS = {"ams": [
    {"id": "0", "humidity": "4", "temp": "24.5", "tray": [
        {"id": "0", "tray_type": "PLA", "tray_sub_brands": "PLA Basic", "tray_color": "FF0000FF", "remain": 85,
         "nozzle_temp_min": "190", "nozzle_temp_max": "230"},
        {"id": "1"},
        {"id": "2", "tray_type": "PETG", "tray_sub_brands": "PETG HF", "tray_color": "00FF00FF", "remain": -1,
         "nozzle_temp_min": "230", "nozzle_temp_max": "260"},
        {"id": "3", "tray_type": "", "tray_color": "00000000"},
    ]},
    {"id": "1", "humidity": "5", "temp": "25.0", "tray": [
        {"id": "0", "tray_type": "PLA-CF", "tray_color": "333333FF", "remain": 40,
         "nozzle_temp_min": "210", "nozzle_temp_max": "240"},
        {"id": "1", "tray_type": "PA-CF", "tray_color": "000000FF", "remain": 100},
        {"id": "2", "tray_type": "TPU-AMS", "tray_color": "FFFFFFFF", "remain": 10},
        {"id": "3", "tray_type": "PET-CF", "tray_color": "GGGGGGFF", "remain": 50,
         "nozzle_temp_min": "300", "nozzle_temp_max": "250"},
    ]},
], "ams_exist_bits": "3", "tray_now": "0"}
VT_TRAY = {"id": "254", "tray_type": "ABS", "tray_sub_brands": "", "tray_color": "000000FF", "remain": 0,
           "nozzle_temp_min": "240", "nozzle_temp_max": "270"}


def test_bambu_two_ams_units_and_the_external_spool(bambu):
    bambu.fake.ams, bambu.fake.vt_tray = copy.deepcopy(AMS), copy.deepcopy(VT_TRAY)
    slots = bambu.adapter().filaments()
    assert [(s.slot, s.empty, s.family, s.material, s.color, s.percent_left) for s in slots] == [
        ("A1", False, "PLA", "PLA", "#ff0000", 85.0),
        ("A2", True, "unknown", "", "", None),
        ("A3", False, "PETG", "PETG", "#00ff00", None),
        ("A4", True, "unknown", "", "", None),
        ("B1", False, "PLA-CF", "PLA-CF", "#333333", 40.0),
        ("B2", False, "PA", "PA-CF", "#000000", 100.0),
        ("B3", False, "TPU", "TPU-AMS", "#ffffff", 10.0),
        ("B4", False, "other", "PET-CF", "", 50.0),
        ("ext", False, "ABS", "ABS", "#000000", 0.0),
    ]
    a1 = slots[0]
    assert (a1.name, a1.nozzle_min_c, a1.nozzle_max_c) == ("PLA Basic", 190.0, 230.0)
    assert a1.extra == {"humidity_level": 4.0, "temperature_c": 24.5}
    assert a1.source == "bambu print.ams.ams[0].tray[0]" and slots[-1].source == "bambu print.vt_tray"
    assert (slots[7].nozzle_min_c, slots[7].nozzle_max_c) == (None, None)  # min above max is dropped
    assert bambu.fake.ignored == [] and bambu.brain.events == []


def test_bambu_waits_past_a_partial_push_for_the_ams(bambu):
    bambu.fake.ams, bambu.fake.vt_tray = copy.deepcopy(AMS), copy.deepcopy(VT_TRAY)
    bambu.fake.partial_first = True
    slots = bambu.adapter().filaments()
    assert slots[0].slot == "A1" and slots[0].family == "PLA" and slots[-1].slot == "ext"


def test_bambu_report_without_ams_is_one_unknown_slot(bambu):
    [slot] = bambu.adapter().filaments()
    assert (slot.slot, slot.family, slot.source) == ("ext", "unknown", "bambu: no ams or vt_tray in the report")


@pytest.mark.parametrize("report", [
    {"ams": {"ams": "nope"}},
    {"ams": ["not", "a", "dict"]},
    {"ams": {"ams": [{"id": "0", "tray": "nope"}]}},
    {"ams": {"ams": [{"id": "0", "tray": ["nope"]}]}},
    {"vt_tray": "nope"},
])
def test_bambu_malformed_ams_is_a_plain_protocol_error(report):
    with pytest.raises(ProtocolError, match="AMS data in a shape"):
        ams_slots(report, "p1s")


def test_bambu_malformed_ams_over_mqtt(bambu):
    bambu.fake.ams = {"ams": [{"id": "0", "tray": [7]}]}
    with pytest.raises(ProtocolError, match="AMS data in a shape"):
        bambu.adapter().filaments()


# ---------------------------------------------------------------- every other adapter


@pytest.mark.parametrize("kind", sorted(set(ADAPTERS) - {"moonraker", "creality", "bambu"}))
def test_adapters_without_filament_reports_say_so(kind):
    cfg = PrinterConfig(name="p", type=kind, host="127.0.0.1", port=9)
    with pytest.raises(Unsupported, match="that printer type does not report loaded filament") as err:
        build(cfg, "x").filaments()
    assert err.value.hint == "Ask the person which filament is loaded"


# ---------------------------------------------------------------- CLI


@pytest.fixture
def k2_cli():
    brain = fakes.Brain(secret=None)
    brain.box = copy.deepcopy(K2_BOX_2026_11_22)
    srv = fakes.serve_http(brain, fakes.moonraker_routes)
    assert cli.main(["pair", "k2", "--type", "moonraker", "--host", "127.0.0.1", "--port",
                     str(srv.server_address[1]), "--model", "creality-k2"]) == 0
    yield brain
    srv.shutdown()


def test_cli_json_shape(k2_cli, capsys):
    capsys.readouterr()
    assert cli.main(["filaments", "k2", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"printer", "type", "slots", "next"}
    assert (data["printer"], data["type"]) == ("k2", "moonraker")
    assert all(set(s) == SLOT_KEYS for s in data["slots"])
    assert data["slots"][1] == {
        "slot": "T1B", "empty": False, "family": "PLA", "material": "008001", "brand": "", "name": "",
        "color": "#ffffff", "percent_left": 100.0, "grams_left": None, "nozzle_min_c": None, "nozzle_max_c": None,
        "source": "moonraker box.T1.material_type[1]", "extra": UNIT_T1}
    assert "ask the person" in data["next"]
    assert k2_cli.events == []


def test_cli_table(k2_cli, capsys):
    capsys.readouterr()
    assert cli.main(["filaments", "k2"]) == 0
    text = capsys.readouterr().out
    lines = text.splitlines()
    assert lines[0].split() == ["SLOT", "FAMILY", "NAME", "COLOR", "LEFT", "NOZZLE", "SOURCE"]
    assert lines[1].split() == ["T1A", "empty"]
    assert lines[2].split()[:6] == ["T1B", "PLA", "008001", "#ffffff", "100%", "-"]
    assert lines[-1].startswith("NEXT: Pick the slot whose family matches")


def test_cli_unsupported_printer(capsys):
    assert cli.main(["pair", "duet", "--type", "duet", "--host", "127.0.0.1", "--no-check"]) == 0
    capsys.readouterr()
    assert cli.main(["filaments", "duet", "--json"]) == 1
    err = capsys.readouterr().err
    assert "ERROR (unsupported): duet is a Duet" in err and "does not report loaded filament" in err


# ---------------------------------------------------------------- shared helpers


@pytest.mark.parametrize("text,family", [
    ("PLA", "PLA"), ("pla", "PLA"), ("PLA+", "PLA"), ("PLA Silk", "PLA"), ("PLA-CF", "PLA-CF"),
    ("PETG", "PETG"), ("PETG-CF", "PETG-CF"), ("PA6-CF", "PA"), ("Nylon", "PA"), ("TPU-95A", "TPU"),
    ("ASA", "ASA"), ("PC", "PC"), ("PVA", "PVA"), ("HIPS", "HIPS"), ("PPS", "other"), ("", "unknown"),
    ("-1", "unknown"), (None, "unknown"), (42, "unknown"),
])
def test_material_family(text, family):
    assert material_family(text) == family


@pytest.mark.parametrize("value,out", [
    ("ffffff", "#ffffff"), ("#00AA11", "#00aa11"), ("fff", ""), ("gggggg", ""), (None, ""), (123456, ""),
])
def test_hex_color(value, out):
    assert hex_color(value) == out


def test_slot_cleans_every_field():
    s = Slot("T1A" * 10, 1, family="Wood", material="x" * 100, brand="\x07bell", color="red",
             percent_left=101, grams_left=-1, nozzle_min_c=200, nozzle_max_c=None, source="s" * 70,
             extra={"a": float("inf"), "b": "\n" * 3, 5: "dropped"})
    assert len(s.slot) == 16 and s.empty is True and s.family == "unknown"
    assert len(s.material) == 64 and s.brand == "?bell" and s.color == ""
    assert (s.percent_left, s.grams_left, s.nozzle_min_c, s.nozzle_max_c) == (None, None, None, None)
    assert len(s.source) == 64 and s.extra == {"b": ""}  # infinity would break --json
