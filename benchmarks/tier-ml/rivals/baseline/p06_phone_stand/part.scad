// Phone stand: phone 9 mm thick in its case, leans back at 70 degrees, 80 mm wide,
// slot for a charging cable. Printed on its side: profile in XY, extruded by the width.
phone_t = 9;
angle = 70;            // back rest angle from the table
width = 80;
t = 6;                 // bar thickness
rest_len = 100;
base_t = 6;
lip_h = 10;
gap = 1;               // clearance in front of the phone
cable_w = 14;

ax = 25;                               // where the rest's front face meets the base top
u = [-cos(angle), sin(angle)];         // up the rest
nb = [-sin(angle), -cos(angle)];       // toward the back of the rest
A = [ax, base_t];
top = A + rest_len * u;
lip_x = ax + (phone_t + gap) / sin(angle);
back_x = -30;

module profile() {
    // base
    translate([back_x, 0]) square([lip_x + 4 - back_x, base_t]);
    // rest
    polygon([A, top, top + t * nb, A + t * nb]);
    // solid web between rest and base behind it
    polygon([A + t * nb, top + t * nb + 0.4 * rest_len * (-u), [back_x, base_t], [back_x, 0.1]]);
    // front lip
    translate([lip_x, 0]) square([4, base_t + lip_h]);
}

difference() {
    linear_extrude(width) profile();
    // cable slot through the lip and the base under the phone, centered on the width
    translate([ax - 2, -1, (width - cable_w) / 2]) cube([lip_x + 6 - ax + 2, base_t + lip_h + 2, cable_w]);
}
