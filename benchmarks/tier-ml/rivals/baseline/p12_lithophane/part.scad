// Lithophane of lithophane_photo.png, 100 mm wide, 0.8 mm at the brightest and 3 mm at
// the darkest, standing on its own foot.
img = "lithophane_photo.png";
px = [300, 375];        // image size in pixels
width = 100;
min_t = 0.8;
max_t = 3.0;
frame = 4;
foot_d = 25;
foot_h = 4;

s = width / px[0];
h = px[1] * s;

module picture() {
    // surface(): white = 100, black = 0; invert makes dark thick
    translate([0, 0, min_t])
        scale([s, s, (max_t - min_t) / 100])
            surface(file = img, invert = true, convexity = 5);
    cube([width, h, min_t]);
}

// stand it up: picture in the XZ plane, viewed from -Y
rotate([90, 0, 0]) mirror([0, 0, 1]) {
    picture();
    // frame
    difference() {
        translate([-frame, -frame, 0]) cube([width + 2 * frame, h + 2 * frame, max_t]);
        cube([width, h, max_t + 1]);
    }
}
// foot
translate([-frame, -foot_d / 2 + max_t / 2, -frame - foot_h]) cube([width + 2 * frame, foot_d, foot_h]);
