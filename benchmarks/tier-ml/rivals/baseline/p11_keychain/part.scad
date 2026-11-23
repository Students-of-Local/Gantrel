// Keychain that says EMMA in bold letters, about 50 mm wide, hole for a key ring.
name = "EMMA";
font = "Liberation Sans:style=Bold";
width = 50;          // overall
text_size = 11;
plate_t = 3;
relief = 1.2;
border = 3;
ring_d = 5;
$fn = 48;

module label() {
    resize([width - 2 * border - ring_d - 5, 0], auto = true)
        text(name, size = text_size, font = font, halign = "center", valign = "center");
}

tab_x = -(width - 2 * border - ring_d - 5) / 2 - border - ring_d / 2 - 1;

translate([width / 2, 0, 0]) {
    difference() {
        linear_extrude(plate_t)
            union() {
                offset(r = border) label();
                hull() {
                    translate([tab_x, 0]) circle(d = ring_d + 2 * 2.5);
                    translate([tab_x + 6, 0]) circle(d = ring_d + 2 * 2.5);
                }
            }
        translate([tab_x, 0, -1]) cylinder(d = ring_d, h = plate_t + 2);
    }
    translate([0, 0, plate_t - 0.01]) linear_extrude(relief) label();
}
