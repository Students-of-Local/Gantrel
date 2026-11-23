// L bracket for a shelf: legs 50 mm on the outside, 25 mm wide, 5 mm thick,
// a triangular gusset on each side, two 4.5 mm holes per leg 15 and 40 mm from the
// outside corner, countersunk for #8 flat-head screws on the inside faces.
// Printed with one leg flat on the bed.
leg = 50;
w = 25;
t = 5;
hole_d = 4.5;
head_d = 9;             // #8 flat head is about 8.4 mm
hole_at = [15, 40];
gusset = 30;
gusset_t = 3;
$fn = 48;

module countersunk_hole(len) {
    // axis along +Z, head at z = len
    translate([0, 0, -1]) cylinder(d = hole_d, h = len + 2);
    translate([0, 0, len - (head_d - hole_d) / 2]) cylinder(d1 = hole_d, d2 = head_d, h = (head_d - hole_d) / 2 + 0.01);
    translate([0, 0, len]) cylinder(d = head_d, h = 1);
}

difference() {
    union() {
        cube([leg, w, t]);                  // leg on the bed
        cube([t, w, leg]);                  // upright leg
        for (y = [0, w - gusset_t])
            translate([0, y + gusset_t, 0]) rotate([90, 0, 0])
                linear_extrude(gusset_t) polygon([[t, t], [gusset, t], [t, gusset]]);
    }
    for (x = hole_at) translate([x, w / 2, 0]) countersunk_hole(t);
    for (zz = hole_at) translate([0, w / 2, zz]) rotate([0, 90, 0]) countersunk_hole(t);
}
