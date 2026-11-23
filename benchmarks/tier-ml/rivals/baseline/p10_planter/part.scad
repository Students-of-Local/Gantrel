// Ribbed planter for a 4 inch nursery pot (102 mm across the top, 90 mm tall), drain hole.
pot_top = 102;
pot_h = 90;
clear = 2;            // room around the nursery pot on each side
wall = 2.4;
floor_t = 3;
H = pot_h + floor_t + 5;
inner_top = pot_top + 2 * clear;   // 106
inner_base = 82;
ribs = 36;
rib_depth = 1.6;
drain_d = 12;

module ribbed(d) {
    n = ribs * 8;
    polygon([for (k = [0:n - 1])
        let(a = 360 * k / n, r = d / 2 + rib_depth * (0.5 + 0.5 * cos(ribs * a)))
        [r * cos(a), r * sin(a)]]);
}

difference() {
    linear_extrude(H, scale = (inner_top + 2 * wall) / (inner_base + 2 * wall), convexity = 4)
        ribbed(inner_base + 2 * wall);
    translate([0, 0, floor_t]) cylinder(d1 = inner_base, d2 = inner_base + (inner_top - inner_base) * (H - floor_t + 0.01) / H + 0.01, h = H - floor_t + 0.01, $fn = 180);
    translate([0, 0, -1]) cylinder(d = drain_d, h = floor_t + 2, $fn = 48);
}
