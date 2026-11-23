// Spur gear: module 1.5, 24 teeth, 20 degree pressure angle, 8 mm thick,
// bore for a 5 mm D shaft (4.5 mm across the flat).
m = 1.5;
z = 24;
pa = 20;
thickness = 8;
bore = 5.2;          // 5 mm shaft plus clearance
flat = 4.6;          // 4.5 mm across the flat plus clearance

rp = m * z / 2;                 // pitch radius 18
ra = rp + m;                    // tip radius 19.5
rf = rp - 1.25 * m;             // root radius 16.125
rb = rp * cos(pa);              // base radius

function inv_deg(a) = (tan(a) - a * PI / 180) * 180 / PI;   // involute function, degrees
half = 90 / z + inv_deg(pa);    // half tooth angle at the base circle
function inv_pt(t, s) =         // t = roll angle in radians, s = +1 or -1 flank
    let(r = rb * sqrt(1 + t * t), phi = (t - atan(t) * PI / 180) * 180 / PI)
    r * [cos(s * (half - phi)), sin(s * (half - phi))];

tmax = sqrt(pow(ra / rb, 2) - 1);
steps = 12;
r0 = min(rf, rb);

module tooth() {
    upper = [for (k = [0:steps]) inv_pt(tmax * k / steps, 1)];
    lower = [for (k = [steps:-1:0]) inv_pt(tmax * k / steps, -1)];
    polygon(concat(
        [r0 * [cos(-half), sin(-half)] * 0.98],
        lower,
        upper,
        [r0 * [cos(half), sin(half)] * 0.98]
    ));
}

module gear2d() {
    circle(r = rf, $fn = 120);
    for (i = [0:z - 1]) rotate(i * 360 / z) tooth();
}

module d_bore() {
    intersection() {
        circle(d = bore, $fn = 48);
        translate([-bore / 2 + flat - bore / 2, 0]) square([bore, bore + 1], center = true);
    }
}

linear_extrude(thickness)
difference() {
    gear2d();
    d_bore();
}
