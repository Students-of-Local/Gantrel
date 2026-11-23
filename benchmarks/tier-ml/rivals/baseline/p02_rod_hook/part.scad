// Wall hook for a 1 inch (25.4 mm) round dowel, 30 mm wide, two holes for #8 wood screws.
// Printed on its side: the profile is drawn in XY and extruded by the hook width.
rod_d = 25.4;
clear = 1.0;            // so the dowel drops in easily
width = 30;
t = 6;                  // bar thickness
back_h = 90;            // back plate height
tip_rise = 12;          // how far the tip rises above the cradle center
screw_d = 4.5;          // #8 wood screw clearance
ri = (rod_d + clear) / 2;
cx = t + ri;            // cradle center
cy = t + ri;
$fn = 96;

module profile() {
    // back plate
    translate([0, cy]) square([t, back_h - cy]);
    // lower half ring (the cradle)
    translate([cx, cy])
    difference() {
        circle(r = ri + t);
        circle(r = ri);
        translate([-(ri + t) - 1, 0]) square([2 * (ri + t) + 2, ri + t + 1]);
    }
    // tip
    translate([cx + ri, cy]) square([t, tip_rise]);
}

difference() {
    linear_extrude(width) profile();
    for (y = [cy + 25, back_h - 12])
        translate([-1, y, width / 2]) rotate([0, 90, 0]) cylinder(d = screw_d, h = t + 2, $fn = 32);
}
