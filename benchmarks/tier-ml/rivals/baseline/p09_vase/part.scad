// Twisted vase about 180 mm tall, 80 mm across at the widest, solid for spiral vase mode.
H = 180;
max_d = 80;
lobes = 7;
lobe_depth = 0.07;     // fraction of the radius
twist = 120;           // degrees over the height
nz = 90;
na = 140;

// radius profile: base 54, belly 80 at 40 percent, neck 50, lip 58 (diameters)
function prof(f) =
    let(k = [[0, 54], [0.4, 80], [0.85, 50], [1, 58]])
    f <= 0.4 ? lerp(k[0], k[1], f) : f <= 0.85 ? lerp(k[1], k[2], f) : lerp(k[2], k[3], f);
function lerp(a, b, f) = let(s = (f - a[0]) / (b[0] - a[0]), e = (1 - cos(180 * s)) / 2) a[1] + (b[1] - a[1]) * e;

// keep the widest point at max_d: lobes push out, so scale the base radius down
function r_at(f, a) = prof(f) / 2 / (1 + lobe_depth) * (1 + lobe_depth * cos(lobes * (a - twist * f)));

pts = [for (i = [0:nz]) for (j = [0:na - 1])
    let(f = i / nz, a = 360 * j / na, r = r_at(f, a)) [r * cos(a), r * sin(a), H * f]];
function id(i, j) = i * na + (j % na);
faces = concat(
    [[for (j = [na - 1:-1:0]) id(0, j)]],
    [[for (j = [0:na - 1]) id(nz, j)]],
    [for (i = [0:nz - 1]) for (j = [0:na - 1]) [id(i, j), id(i, j + 1), id(i + 1, j + 1), id(i + 1, j)]]
);
polyhedron(points = pts, faces = faces, convexity = 4);
