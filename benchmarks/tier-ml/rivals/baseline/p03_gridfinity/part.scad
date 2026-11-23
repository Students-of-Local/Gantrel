// Gridfinity bin 2 x 1 x 6 units, stacking lip, label shelf, one divider.
pitch = 42;
gx = 2;
gy = 1;
hu = 6;
H = 7 * hu;              // 42 mm, top of the wall below the lip
W = gx * pitch - 0.5;    // 83.5
D = gy * pitch - 0.5;    // 41.5
R = 3.75;                // outer corner radius
wall = 1.2;
floor_top = 7;           // inside floor height
lip_h = 4.4;
$fn = 48;

module rr(w, d, r, h = 0.01) {
    rr_ = max(r, 0.3);
    linear_extrude(h) offset(rr_) square([w - 2 * rr_, d - 2 * rr_], center = true);
}

// one base foot, standard profile: 0.8 chamfer, 1.8 straight, 2.15 chamfer (4.75 total)
module foot() {
    hull() {
        rr(35.6, 35.6, 0.8);
        translate([0, 0, 0.8]) rr(37.2, 37.2, 1.6);
    }
    hull() {
        translate([0, 0, 0.8]) rr(37.2, 37.2, 1.6);
        translate([0, 0, 2.6]) rr(37.2, 37.2, 1.6);
    }
    hull() {
        translate([0, 0, 2.6]) rr(37.2, 37.2, 1.6);
        translate([0, 0, 4.75 - 0.01]) rr(41.5, 41.5, R);
    }
}

// inside cavity at inset i from the outer wall
module slice(i, z) { translate([0, 0, z]) rr(W - 2 * i, D - 2 * i, R - i); }

union() {
    for (x = [0:gx - 1], y = [0:gy - 1])
        translate([(x - (gx - 1) / 2) * pitch, (y - (gy - 1) / 2) * pitch, 0]) foot();

    difference() {
        translate([0, 0, 4.75]) rr(W, D, R, H + lip_h - 4.75);
        // main cavity
        hull() { slice(wall, floor_top); slice(wall, H - 1.8); }
        // support chamfer under the lip
        hull() { slice(wall, H - 1.8); slice(3.0, H); }
        // lip profile: 0.7 chamfer, 1.8 straight, 1.9 chamfer to a 0.4 top edge
        hull() { slice(3.0, H); slice(2.3, H + 0.7); }
        hull() { slice(2.3, H + 0.7); slice(2.3, H + 2.5); }
        hull() { slice(2.3, H + 2.5); slice(0.4, H + lip_h + 0.01); }
    }

    // divider across the middle
    translate([-wall / 2, -D / 2 + 1, floor_top]) cube([wall, D - 2, H - floor_top - 1.8]);

    // label shelf on the back wall, 45 degree underside
    shelf = 10;
    yb = D / 2 - wall;
    translate([W / 2 - wall, 0, 0]) rotate([0, -90, 0])
        linear_extrude(W - 2 * wall)
            polygon([[H - 1.8, yb], [H - 1.8, yb - shelf], [H - 1.8 - shelf, yb]]);
}
