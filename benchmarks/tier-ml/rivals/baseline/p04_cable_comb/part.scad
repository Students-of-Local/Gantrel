// Cable comb for 6 cables of 6 mm, 10 mm apart, cables click in.
cables = 6;
cable_d = 6;
pitch = 10;
slot_d = cable_d + 0.3;     // seat
mouth = cable_d - 0.6;      // narrower mouth so the cable clicks in
lead_in = 1.2;
margin = 6;
length = (cables - 1) * pitch + 2 * margin;   // 62
depth = 16;                 // Y, slots open on +Y
height = 8;                 // Z, plate thickness
seat_y = depth - slot_d / 2 - 1.6;
$fn = 64;

module slot() {
    circle(d = slot_d);
    // mouth straight section then a lead-in chamfer at the edge
    polygon([
        [-mouth / 2, 0], [mouth / 2, 0],
        [mouth / 2, depth - seat_y - lead_in],
        [mouth / 2 + lead_in, depth - seat_y + 0.01],
        [-mouth / 2 - lead_in, depth - seat_y + 0.01],
        [-mouth / 2, depth - seat_y - lead_in]
    ]);
}

linear_extrude(height)
difference() {
    offset(r = 2) offset(delta = -2) square([length, depth]);
    for (i = [0:cables - 1])
        translate([margin + i * pitch, seat_y]) slot();
}
