"""pack check: schema, ranges, filament rules, privacy scan and the G-code policy."""

import json

import pytest

from gantrel_printer import packs


def check(pack):
    data = pack if isinstance(pack, bytes) else json.dumps(pack).encode()
    return packs.check_bytes(data)[1]


def refused_at(rep, path, line=None, word=""):
    hits = [i for i in rep.refusals if i.path == path and (line is None or i.line == line) and word in i.reason]
    assert hits, f"no refusal at {path} line {line} with {word!r}: {[(i.path, i.line, i.reason) for i in rep.items]}"
    return hits[0]


def test_ordinary_pack_passes(good):
    rep = check(good)
    assert rep.result == "pass", [(i.path, i.reason) for i in rep.items if i.level != "note"]
    assert len(rep.sha256) == 64


# ---------------------------------------------------------------- schema


@pytest.mark.parametrize("where, path", [
    (lambda p: p, "extra"),
    (lambda p: p["printer"], "printer.extra"),
    (lambda p: p["materials"][0], "materials[0].extra"),
    (lambda p: p["materials"][0]["calibrated"][0], "materials[0].calibrated[0].extra"),
    (lambda p: p["proof"][0], "proof[0].extra"),
])
def test_unknown_keys_refused_at_every_level(good, where, path):
    where(good)["extra"] = 1
    refused_at(check(good), path, word="unknown key")


@pytest.mark.parametrize("key", ["host", "serial", "access_code", "tls_fingerprint", "camera_url", "port"])
def test_pairing_fields_are_not_part_of_a_pack(good, key):
    good["printer"][key] = "x"
    refused_at(check(good), f"printer.{key}", word="unknown key")


def test_missing_and_wrong_types(good):
    del good["printer"]["bed_max_c"]
    good["materials"][0]["fan"] = [100]
    good["printer"]["enclosed"] = "yes"
    good["printer"]["max_speed"] = True
    rep = check(good)
    refused_at(rep, "printer.bed_max_c", word="missing")
    refused_at(rep, "materials[0].fan", word="two numbers")
    refused_at(rep, "printer.enclosed", word="true or false")
    refused_at(rep, "printer.max_speed", word="whole number")


@pytest.mark.parametrize("field, value", [("id", "Bad_ID"), ("id", "ab"), ("version", "v1"), ("created", "2026-13-01"),
                                          ("format", "gantrel-pack/2")])
def test_top_level_formats(good, field, value):
    good[field] = value
    refused_at(check(good), field)


@pytest.mark.parametrize("keys, value, path", [
    (("printer", "nozzle"), 5.0, "printer.nozzle"), (("printer", "bed"), [10, 260], "printer.bed[0]"),
    (("printer", "hotend_max_c"), 900, "printer.hotend_max_c"), (("printer", "accel"), 0, "printer.accel"),
    (("printer", "filament_mm"), 2.0, "printer.filament_mm"), (("printer", "flavor"), "bash", "printer.flavor"),
    (("printer", "adapter"), "telnet", "printer.adapter"),
    (("printer", "top_speed"), 5000, "printer.top_speed"), (("printer", "top_speed"), 400.5, "printer.top_speed"),
    (("printer", "default_ports"), {"creality": 70000}, "printer.default_ports.creality"),
    (("materials", 0, "retraction_mm"), 30, "materials[0].retraction_mm"),
    (("materials", 0, "flow_ratio"), 3, "materials[0].flow_ratio"),
    (("materials", 0, "family"), "UNOBTAINIUM", "materials[0].family"),
    (("materials", 0, "fan"), [100, 50], "materials[0].fan"),
])
def test_physical_ranges(good, keys, value, path):
    target = good
    for k in keys[:-1]:
        target = target[k]
    target[keys[-1]] = value
    refused_at(check(good), path)


def test_oversize_file_refused():
    rep = check(b" " * (packs.MAX_FILE + 1))
    refused_at(rep, "(file)", word="limit")


def test_oversize_strings_refused(good):
    good["notes"] = "x" * 2001
    good["name"] = "y" * 4097
    good["printer"]["start_gcode"] = "G28\n" * 2100   # 8400 characters
    rep = check(good)
    refused_at(rep, "notes", word="limit is 2000")
    refused_at(rep, "name", word="limit")
    refused_at(rep, "printer.start_gcode", word=f"limit is {packs.MAX_GCODE}")


def test_bad_json_refused():
    refused_at(check(b'{"id": "a", "id": "b"}'), "id", word="twice")
    refused_at(check(b'{"x": NaN}'), "(file)", word="not valid JSON")
    refused_at(check(b"[1, 2]"), "(file)", word="one JSON object")
    refused_at(check(b"\xff\xfe"), "(file)", word="UTF-8")
    refused_at(check(b"[" * 100000), "(file)")


def test_invisible_characters_refused(good):
    good["name"] = "Nice pack\u202e"
    good["notes"] = "line\x1b[2J"
    rep = check(good)
    refused_at(rep, "name", word="invisible")
    refused_at(rep, "notes", word="invisible")


# ---------------------------------------------------------------- privacy


@pytest.mark.parametrize("path, value, word", [
    ("notes", "My K2 is at 192.168.1.50 on the shelf", "IP address"),
    ("notes", "reach it at fe80::1ff:fe23:4567:890a", "IPv6"),
    ("notes", "mac aa:bb:cc:dd:ee:ff", "MAC"),
    ("author", "maker@example.com", "email"),
    ("notes", "upload via http://maker:hunter2@k2box/server", "user or password"),
    ("notes", "profile saved in /home/alice/printer_data", "home folder"),
    ("notes", "copied from /Users/bob/Library/Orca", "home folder"),
    ("notes", "copied from C:\\Users\\carol\\AppData", "home folder"),
    ("notes", "printer 01P00A123456789 works fine", "serial"),
    ("notes", "the lan access code is 12345678 today", "8 character code"),
    ("notes", "token ghp_A1b2C3d4E5f6G7h8I9j0K1l2", "key or token"),
    ("notes", "pinned a31c95e273d3423abf185a444d70beae4941c697", "hex"),
    ("notes", "camera http://cam.example.com/webcam/?action=snapshot", "camera"),
    ("notes", "stream rtsp://cam.example.com/live", "camera"),
    ("notes", "k2.local answers", "local network host"),
    ("notes", "Wi-Fi: HomeNet5G", "Wi-Fi"),
    ("notes", "username = alice", "user name"),
])
def test_privacy_patterns_refused_with_field_path(good, path, value, word):
    good[path] = value
    item = refused_at(check(good), path, word=word)
    assert "[hidden]" in item.text


def test_privacy_scan_reaches_gcode_and_material_fields(good):
    good["printer"]["start_gcode"] += "\n; host 10.0.0.7"
    good["materials"][0]["brand"] = "me@example.org"
    good["materials"][0]["calibrated"][0]["result"] = "done on /home/bob/k2"
    rep = check(good)
    n = good["printer"]["start_gcode"].count("\n") + 1
    refused_at(rep, "printer.start_gcode", n, "IP address")
    refused_at(rep, "materials[0].brand", word="email")
    refused_at(rep, "materials[0].calibrated[0].result", word="home folder")


def test_privacy_does_not_flag_ordinary_text(good):
    good["notes"] = ("Version 1.2.3 of the profile, tested 2026-11-20 at 12:30:45. Uses BED_MESH_CALIBRATE and "
                     "first_layer_bed_temperature. Orca profile Creality K2 0.4 nozzle. "
                     "https://github.com/baney75/Gantrel")
    rep = check(good)
    assert rep.result == "pass", [(i.path, i.reason) for i in rep.refusals]


# ---------------------------------------------------------------- G-code policy


REFUSED = ["M500", "M501", "M502", "M504", "M997", "M999", "M303 E0 S200 C8", "M92 E400", "M201 X5000", "M203 X500",
           "M206 X1", "M301 P1 I2 D3", "M304 P1", "M851 Z-1", "M906 X800", "M913 X100", "M302 S0", "M211 S0",
           "SAVE_CONFIG", "FIRMWARE_RESTART", "RESTART", "RUN_SHELL_COMMAND CMD=update", "SET_KINEMATIC_POSITION Z=0",
           "SDCARD_PRINT_FILE FILENAME=a.gcode", "SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=0",
           "save_config", "N10 M500*42", "M500 ; with a comment", "m502"]


@pytest.mark.parametrize("cmd", REFUSED)
def test_every_refused_command_is_refused_with_its_line(good, cmd):
    good["printer"]["end_gcode"] = f"M107\n{cmd}\nM84"
    rep = check(good)
    item = refused_at(rep, "printer.end_gcode", 2)
    assert item.text == cmd.strip()


@pytest.mark.parametrize("line, heater", [
    ("M104 S320", "hotend"), ("M109 R301", "hotend"), ("M140 S110", "bed"), ("M190 S101", "bed"),
    ("START_PRINT EXTRUDER_TEMP=310 BED_TEMP=60", "hotend"), ("PRINT_START BED_TEMP=120", "bed"),
    ("SET_HEATER_TEMPERATURE HEATER=extruder TARGET=350", "hotend"),
])
def test_temperature_above_the_packs_limit_refused(good, line, heater):
    good["printer"]["start_gcode"] = f"G28\n{line}"
    item = refused_at(check(good), "printer.start_gcode", 2, f"sets the {heater}")
    assert "_max_c" in item.reason


def test_temperatures_at_the_limit_and_placeholders_pass(good):
    good["printer"]["start_gcode"] = ("M104 S300\nM140 S100\nM109 S{first_layer_temperature[0]}\n"
                                      "M190 S[first_layer_bed_temperature]\nSTART_PRINT EXTRUDER_TEMP=300")
    rep = check(good)
    assert not [i for i in rep.items if i.path.startswith("printer.start")], rep.items


@pytest.mark.parametrize("line", ["G0 X1", "G1 X5 Y20 E1 F600", "G2 X1 Y1 I1 J0", "G3 X1 Y1 I1 J0", "G4 P100", "G28",
                                  "G29", "G90", "G91", "G92 E0", "M82", "M83", "M84", "M104 S200", "M105", "M106 S255",
                                  "M107", "M109 S200", "M140 S60", "M190 S60", "M141 S40", "M191 S40", "M204 S5000",
                                  "M205 X8", "M220 S100", "M221 S100", "M400", "M117 Printing", "M73 P0", "T0", "T15",
                                  "; just a comment", "", "START_PRINT", "END_PRINT", "PRINT_START", "PRINT_END",
                                  "BED_MESH_CALIBRATE", "CLEAN_NOZZLE", "PAUSE", "RESUME", "CANCEL_PRINT",
                                  "start_print EXTRUDER_TEMP={temperature[0]}", "G1 Z{layer_height}"])
def test_allowed_lines_pass_without_remark(good, line):
    good["printer"]["start_gcode"] = line
    rep = check(good)
    assert not [i for i in rep.items if i.path == "printer.start_gcode"], rep.items


@pytest.mark.parametrize("line, word", [
    ("G32", "G32 is not on"), ("M118 hello", "M118 is not on"), ("T16", "past T15"),
    ("LOAD_MATERIAL", "LOAD_MATERIAL is a printer macro"), ("M104 S{temperature[0]+10}", "placeholder"),
    ("{if first_layer_temperature[0] > 200}G1 X1{endif}", "not a G-code command"),
    ("SET_HEATER_TEMPERATURE HEATER=extruder TARGET=200", "SET_HEATER_TEMPERATURE"),
])
def test_other_commands_are_findings_not_refusals(good, line, word):
    good["printer"]["start_gcode"] = f"G28\n{line}"
    rep = check(good)
    assert not [i for i in rep.refusals if i.path == "printer.start_gcode"], rep.refusals
    hits = [i for i in rep.findings if i.path == "printer.start_gcode" and i.line == 2 and word in i.reason]
    assert hits, rep.items
    assert rep.result == "findings"


@pytest.mark.parametrize("line", ["G28 M500", "T0 SAVE_CONFIG", "START_PRINT X=1 RUN_SHELL_COMMAND",
                                  "{\"SAVE\" + \"_CONFIG\"}", "M104 S{'1'}"])
def test_refused_commands_hidden_later_in_a_line_or_built_by_the_slicer(good, line):
    good["printer"]["start_gcode"] = f"G28\n{line}"
    refused_at(check(good), "printer.start_gcode", 2)


def test_display_text_may_mention_commands(good):
    good["printer"]["start_gcode"] = "M117 Restart is not needed; no M500 either"
    rep = check(good)
    assert not rep.refusals, rep.refusals


def test_gcode_must_be_ascii(good):
    good["printer"]["end_gcode"] = "M117 Düse"
    refused_at(check(good), "printer.end_gcode", word="ASCII")


# ---------------------------------------------------------------- filament rules


@pytest.mark.parametrize("change, word", [
    (lambda p: p["materials"][0].update(nozzle_c=260), "nozzle temperature in the PLA range"),
    (lambda p: p["materials"][0].update(first_layer_bed_c=90), "first layer bed temperature in the PLA range"),
    (lambda p: (p["materials"][0].update(family="ABS", nozzle_c=255, first_layer_nozzle_c=260, bed_c=100,
                                         first_layer_bed_c=100, fan=[10, 40]),
                p["printer"].update(enclosed=False)), "enclosure"),
    (lambda p: (p["materials"][0].update(family="PLA-CF"), p["printer"].update(hardened_nozzle=False)), "hardened nozzle"),
    (lambda p: (p["materials"][0].update(family="TPU", nozzle_c=225, first_layer_nozzle_c=225, bed_c=40,
                                         first_layer_bed_c=40, retraction_mm=2.5),
                p["printer"].update(direct_drive=False)), "direct drive"),
    (lambda p: (p["printer"].update(hotend_max_c=220)), "within the hotend"),
    (lambda p: (p["printer"].update(bed_max_c=55)), "within the bed heater"),
    (lambda p: p["materials"][0].update(max_flow=30), "max_flow"),
    (lambda p: p["materials"][0].update(fan=[50, 100]), "part fan"),
    (lambda p: p["materials"][0].update(retraction_mm=5), "retraction"),
    (lambda p: p["printer"].update(accel=30000), "max_accel"),
])
def test_materials_breaking_check_rules_are_refused(good, change, word):
    change(good)
    rep = check(good)
    assert any(word in i.reason or word in i.path for i in rep.refusals), [(i.path, i.reason) for i in rep.items]


def test_duplicate_material_refused(good):
    good["materials"].append(dict(good["materials"][0]))
    refused_at(check(good), "materials[1].name", word="same brand and name")


# ---------------------------------------------------------------- a pack's limits are cited


def test_a_packs_limits_are_cited_with_the_pack_as_source(good):
    from gantrel_printer.plan import check as pcheck
    prof = packs.to_profile(good, "ab" * 32)
    assert set(packs.LIMITS) <= set(prof.cited) and "top_speed" not in prof.cited and prof.top_speed == 0
    assert prof.limits_source == "pack test-k2 abababababab (the pack author's values)"
    good["printer"]["top_speed"] = 800
    prof = packs.to_profile(good)
    assert prof.top_speed == 800 and prof.is_cited("top_speed")
    # Main's check semantics: past a cited limit the hotend, bed and nozzle rules fail; speed and accel only warn.
    from gantrel_printer import materials
    s = {"nozzle_c": 305, "bed_c": 60, "layer_height_mm": 0.2, "first_layer_height_mm": 0.25, "perimeters": 2,
         "outer_wall_speed_mms": 150, "inner_wall_speed_mms": 200, "infill_speed_mms": 200,
         "first_layer_speed_mms": 30, "travel_speed_mms": 700, "accel_mms2": 30000}
    c = pcheck.check(s, prof, materials.get("PETG"))
    by = {r["rule"]: r for r in c["rules"]}
    assert by["nozzle temperature within the hotend"]["status"] == "fail"
    assert by["nozzle temperature within the hotend"]["values"]["cited"] is True
    assert by["travel speed within the printer"]["status"] == "pass"
    assert by["travel speed within the printer"]["values"]["top_speed_mms"] == 800
    assert by["acceleration within the printer"]["status"] == "warn"
    s["travel_speed_mms"] = 900
    c = pcheck.check(s, prof, materials.get("PETG"))
    r = next(r for r in c["rules"] if r["rule"] == "travel speed within the printer")
    assert r["status"] == "warn" and r["values"]["cited"] is True and "travel speed within the printer" not in c["fails"]


def test_top_speed_is_optional_and_bounds_max_speed(good):
    good["printer"]["top_speed"] = 500
    assert check(good).result == "pass"
    good["printer"]["top_speed"] = 150
    refused_at(check(good), "printer.max_speed", word="above top_speed")


@pytest.mark.parametrize("line", ["G92 Z20", "G92 X0 Y0", "G92 E0 Z5", "G92 Z{layer_height}", "G92", "g92 z1"])
def test_g92_that_moves_an_axis_is_a_finding(good, line):
    # G92 Z20 then G1 Z0 drives the nozzle 20 mm below where the printer thinks it is: into the bed.
    good["printer"]["start_gcode"] = f"G28\n{line}\nG1 Z0 F600"
    rep = check(good)
    assert not [i for i in rep.refusals if i.path == "printer.start_gcode"], rep.refusals
    hits = [i for i in rep.findings if i.path == "printer.start_gcode" and i.line == 2 and "G92" in i.reason]
    assert hits and rep.result == "findings", rep.items


@pytest.mark.parametrize("line", ["G92 E0", "G92 E-1.5", "G92 E{retract_length[0]}", "N3 G92 E0*17"])
def test_g92_that_resets_only_the_extruder_passes(good, line):
    good["printer"]["start_gcode"] = f"G28\n{line}"
    rep = check(good)
    assert not [i for i in rep.items if i.path == "printer.start_gcode"], rep.items
