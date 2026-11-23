// Finger guard for a 120 mm PC fan, honeycomb grille
// Screw holes 105 mm apart, 4.3 mm; 2.5 mm thick; no opening a finger fits through.
size = 120;
hole_spacing = 105;
hole_d = 4.3;
thickness = 2.5;
corner_r = 6;
opening_d = 116;   // fan blade sweep
cell = 9;          // hex flat-to-flat; corner-to-corner 10.4 mm < 12 mm finger probe
bar = 1.6;
$fn = 64;

module rounded_square(s, r) {
    offset(r) square(s - 2 * r, center = true);
}

module hex_grid() {
    dx = cell + bar;
    dy = dx * sqrt(3) / 2;
    n = ceil(opening_d / dx) + 1;
    for (i = [-n:n], j = [-n:n])
        translate([i * dx + (j % 2) * dx / 2, j * dy])
            rotate(30) circle(d = cell / cos(30), $fn = 6);
}

linear_extrude(thickness)
difference() {
    rounded_square(size, corner_r);
    intersection() {
        circle(d = opening_d);
        hex_grid();
    }
    for (x = [-1, 1], y = [-1, 1])
        translate([x, y] * hole_spacing / 2) circle(d = hole_d);
}
