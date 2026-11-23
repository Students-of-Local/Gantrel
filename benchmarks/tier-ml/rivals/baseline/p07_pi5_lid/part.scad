// Top lid for a Raspberry Pi 5: screws to the board's four M2.5 mounting holes
// (58 x 49 mm apart) on 14 mm standoffs, vent grille over the cooler.
// 2 mm top plate, 3 mm past the 85 x 56 mm board on every side.
// Printed upside down: plate on the bed, standoffs up.
board = [85, 56];
holes = [[3.5, 3.5], [61.5, 3.5], [3.5, 52.5], [61.5, 52.5]];   // from the board corner
over = 3;
plate_t = 2;
standoff_h = 14;
standoff_d = 6;
screw_d = 2.7;          // M2.5 clearance
corner_r = 3;
vent = [40, 30];        // grille area
slot_w = 2;
slot_pitch = 4;
$fn = 48;

plate = board + [2 * over, 2 * over];
center = [(holes[0][0] + holes[1][0]) / 2, (holes[0][1] + holes[2][1]) / 2];

translate([over, over, 0])
difference() {
    union() {
        translate([-over, -over, 0])
            linear_extrude(plate_t) offset(corner_r) offset(delta = -corner_r) square(plate);
        for (h = holes) translate([h[0], h[1], 0]) cylinder(d = standoff_d, h = plate_t + standoff_h);
    }
    for (h = holes) translate([h[0], h[1], -1]) cylinder(d = screw_d, h = plate_t + standoff_h + 2);
    // vent slots
    n = floor(vent[0] / slot_pitch);
    for (i = [0:n - 1])
        translate([center[0] - vent[0] / 2 + slot_pitch / 2 + i * slot_pitch - slot_w / 2, center[1] - vent[1] / 2, -1])
            cube([slot_w, vent[1], plate_t + 2]);
}
