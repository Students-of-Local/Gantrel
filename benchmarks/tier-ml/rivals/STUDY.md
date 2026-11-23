# Gantrel rival study

Prepared 2026-11-17 by the research delegate for the flagship plugins lead. Protocol and bars: EVAL-PLAN.md. Fixed part set: PARTSET.md. No accounts, sign-ins, API keys or money were used; every account-only rival is **not measured hands-on**. Labels: **settled** (the vendor's or authors' own current page, paper or repo), **in flux** (beta, changing or conflicting sources), **judgment** (this study's inference), **unknown** (not found). Source tags in brackets point to "Sources" at the end; every source was read 2026-11-17.

## Verdict

No rival combines what Gantrel aims at: a request's millimeter numbers carried exactly into the part, a printability check on every build, an editable parametric source, and a model that runs locally at 1.7B (judgment from the table below). The strongest rival at tier L is the plain frontier model writing OpenSCAD: in this study's one-pass run it passed both `stone check` and every dimension check on 8 of 12 parts, an upper bound because the same session wrote the checks (`baseline/measure.txt`); it failed the spur gear, twisted vase, Gridfinity bin and lithophane, each on a construction error a render-and-check loop would catch (judgment). Mesh generators (Meshy, Tripo, Bambu's and Creality's AI tools) win on organic shapes from a picture but take no millimeter input and publish no printability number beyond Meshy's own figurine claim [M4][M5]. Zoo is the only B-Rep rival and publishes no accuracy numbers [Z3][Z4]. No rival product runs a local model at tiers S or M; there the rivals are research weights that check code validity, not printing [R3][R5][R6]. Gantrel's bar at L: at least 11 of 12 on the stone, slicer and dimension checks, and 8 of 12 blind looks wins (EVAL-PLAN.md). The lead then ran the pinned slicer check (OrcaSlicer 2.5.0, EVAL-PLAN.md): the baseline passes it on 9 of 12, failing the phone stand (floating cantilever), the vase (inside out) and the fan guard (its solid slice extrudes 5.4% less than the mesh, cause not yet traced), so the baseline passes all three checks on 6 of 12.

## Comparison

Each cell is a cited fact, a measured result with its file, or unknown. "Exact mm" means the request's millimeter numbers become the part's numbers. "Printable as made" means the output passes watertight, one-body and overhang checks without a repair step.

| Rival | Output | Exact mm | Printable as made | Editable after | Price | Account | Offline or local | Platforms | Published number | Hands-on |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Gantrel with smoothstone (reference) | OpenSCAD recipe source and STL [L1][L2] | Recipe parameters in mm, size checked by `stone build` [L2] | Every build runs watertight, one body, first layer, wall, overhang and size checks [L2] | Recipe parameters, re-set with `stone set` [L2] | Free, MIT [L1] | None [L1] | Yes, local model [L1] | Local Studio plugin [L1] | LFM3-1.7B, blind set 6: 39/40 runs, 38/40 compacted [L3] | Not run here (the lead's M and L runs) |
| Frontier model writing OpenSCAD directly (Opus 5.5, one pass, this study) | OpenSCAD source and STL (`baseline/p*/part.scad`, `part.stl`) | 10 of 12 parts pass every dimension and fit check; gear and lithophane fail (`baseline/measure.txt`) | 8 of 12 pass `stone check` as made (`baseline/p*/check.txt`) | OpenSCAD source (`baseline/p*/part.scad`) | unknown | unknown | unknown | Linux, OpenSCAD 2026.11.14 in this run (`baseline/run.sh`) | P3D-Bench text-to-OpenSCAD valid rate: GPT-5.5 0.995, Gemini 3.1 Pro 0.998, Claude Opus 4.6 0.993 [R10] | Measured, 12 parts, below |
| Frontier model writing CadQuery directly | CadQuery Python, STEP or STL [R1][R9] | unknown | MUSE (CadQuery to STEP, 106 designs): sandbox success GPT-5.5 77.36%, Claude Opus 4.7 76.42%, GPT-4o 36.79% [R9] | Python source [R1] | unknown | unknown | unknown | unknown | MUSE final score: GPT-5.5 52.36, Claude Opus 4.7 39.47, Gemini 3.1 Pro 43.40 [R9] | Not measured (CadQuery not installed; see "Not done") |
| Zoo Text-to-CAD / Zookeeper | B-Rep models [Z4]; KCL code under the hood [Z5] | unknown (B-Rep, prompt dimensions not measured by Zoo) [Z4] | unknown | Feature tree and editable parameters [Z3] | Modeling unlimited; Zookeeper and API metered from a monthly credit, USD prices did not load [Z2] | Implied by plan-gated modes [Z3] (in flux) | Zookeeper relies on LLM reasoning and internet references [Z3] | Mac, Windows, Linux, browser [Z1] | None published [Z3][Z4] | Not measured hands-on |
| Adam / CADAM | OpenSCAD compiled to WASM; exports STL, SCAD, DXF [A1] | unknown | unknown | Parametric sliders and SCAD source [A1] | Hosted tiers Free, Pro, Max, Team, Enterprise; USD prices did not load [A2] | Hosted: unknown; self-hosted needs Supabase and model API keys [A1] | Geometry runs in the browser; generation needs cloud model APIs [A1] | Browser; Onshape and Fusion copilot in preview [A3] | None published (README shows example models, no rates) [A1] | Not measured hands-on |
| Meshy | Mesh: GLB, FBX, OBJ, USDZ, STL; 3MF on request [M1] | No mm input in the API; resize to a height on paid plans [M1][M4] | Print Check finds holes and non-manifold edges; repair and "Make Printable" (10 credits, paid) fix them [M4] | Mesh only; Model Edit 20 credits, Remesh, Smart Topology [M2] | Free 100 credits a month; Pro $20, Premium $40, Ultra $100 a month [M2][M3] | API task creation needs a paid plan [M1] | unknown | Web, API [M1][M2] | Vendor claim: about 97% of figurines passed Bambu Studio validation in internal tests [M5] | Not measured hands-on |
| Tripo | Mesh: GLB, OBJ, FBX, USDZ [T1] | unknown | Vendor says "watertight, production-ready" (marketing page) [T7] (in flux) | Mesh; retopology 10 to 40 credits [T1] | API: 1 credit = $0.01, text-to-3D 10 to 20 credits, image-to-3D 20 to 30 [T1][T2]; web plans conflict ($15.90 vs $19.90) [T5][T6] | unknown | Open-weights relatives TripoSR (MIT) and TripoSG run locally [T3][T4] | Web, API [T1] | None published for TripoSR or TripoSG accuracy [T3][T4] | Not measured hands-on |
| Bambu Lab MakerWorld / MakerLab | Parametric Model Maker: OpenSCAD customizer, STL or 3MF [B2] (in flux); AI image-to-3D through partner models [B4] | Parametric Model Maker: set by the uploaded .scad [B2]; AI tools: unknown | unknown | Customizer parameters [B2]; AI meshes: unknown | Image to 3D 24 to 45 credits per model by engine; 80 points buy 600 credits [B4] (in flux) | unknown | unknown | Web; Bambu Handy app [B4] (in flux) | None published | Not measured hands-on |
| Creality Cloud AI | Photo to chibi figure (CubeMe) [C1]; docs list text-to-3D with OBJ, FBX, STL, GLTF, USDZ [C2] (in flux) | unknown | unknown | unknown | Points ("beans"): 50 per artwork plus model [C1] (in flux); USD unknown | unknown | unknown | Creality Cloud web [C1] | None published | Not measured hands-on |
| Prusa / Printables | No Prusa AI generator found [P1] | unknown | unknown | unknown | unknown | unknown | unknown | unknown | None | Not applicable (no generator) |
| OpenSCAD MCP servers (RobertCoop, jhacksman) | OpenSCAD source; render PNG; export STL, 3MF and more [O1][O2] | unknown | unknown | SCAD source [O1][O2] | Free, MIT [O1][O2] | None [O2] | Yes, with any model the client runs [O2] | Python 3.10+ and OpenSCAD [O1] | None published | Not measured (a tool for the baseline rival, not a rival of its own) |
| CadQuery and build123d MCP servers | CadQuery: SVG render; STEP, STL, 3MF export [O3]; build123d: STEP, STL export and a printability check [O5] | unknown | build123d-mcp runs its own printability check [O5] | Python source [O3][O5] | Free; MIT [O3], Apache-2.0 [O5] | None, open source [O3][O5] | unknown | Python [O3][O5] | build123d-mcp author reports CADGenBench 0.360 to 0.457 and 88% to 100% CAD validity (not independently checked) [O5] | Not measured hands-on |
| Research text-to-CAD models (CAD-Coder, CAD-Recode, Text-to-CadQuery, CADmium, CADFusion) | CadQuery or sketch-extrude sequences [R3][R4][R5][R6][R8] | Parametric; dimensions depend on the model's numbers [R5] | CADmium Qwen2.5-Coder 14B: 83.80% watertight [R6] | Code [R3][R4][R5] | Free weights; licenses vary (Apache-2.0, cc-by-nc-4.0, MIT) [R3][R4][R6][R8] | None, open weights [R3][R4] | Yes, 1.5B to 14B weights [R3][R4][R5][R6] | unknown | CADmium 14B 83.80% watertight [R6]; Text-to-CadQuery 3B 69.30% judged [R5]; more in "Published evidence" | Not measured hands-on |

## Per-rival notes

**Zoo (formerly KittyCAD).** The only rival that produces true B-Rep with a feature tree from a prompt [Z3][Z4]. Everything a user makes is KCL code [Z5], which is the closest product to Gantrel's "parametric source the user can edit". Zookeeper launch timing conflicts between Zoo's blog and changelog (January or February 2026) (in flux). No accuracy or printability numbers are published [Z3][Z4]. Prices: the pricing page renders client-side and showed only "Loading..." to the reader; USD figures are unknown [Z2].

**Adam / CADAM.** Open source (GPL-3.0, 5,436 stars, last push 2026-10-15, read via the GitHub API) [A1]. It writes OpenSCAD and compiles it in the browser with openscad-wasm, so it is the same family as Gantrel's output but with no recipe library and no printability checks found in the README [A1] (judgment: the README lists sliders and exports, not checks). Self-hosting needs cloud model API keys (Anthropic, OpenRouter, OpenAI, Google, FAL) [A1], so it is not a local-model tool today. A PromptZone post calling it "fully local" contradicts the repo; treat the post as wrong (judgment).

**Meshy.** A mesh generator with a 3D-printing path: Print Check, repair, "Make Printable" (thickens thin walls, paid) [M4]. The web app's units default to centimeters and resizing to a printed height needs a paid plan [M4]; there is no way to ask for a 4.3 mm hole. The one printability number is the vendor's own (about 97% of figurines passing Bambu Studio validation) [M5], about figurines, not functional parts. A Hackernoon author reports auto-fix resolving about 70% (not confirmed, third party).

**Tripo.** Mesh only; STL and 3MF are not in the API's format list [T1]. The web pricing page blocked automated reads; third-party listings conflict [T5][T6] (in flux). The API page carries "Retiring 2026-10-23" and "2026-12-01" notices whose meaning was not confirmed [T2] (unknown). Its open-weights relatives TripoSR (MIT, about 6 GB VRAM) and TripoSG (8 GB VRAM) run locally [T3][T4] and are the realistic local image-to-mesh rival for a Gantrel user with a GPU; neither publishes accuracy numbers.

**Bambu Lab MakerWorld and MakerLab.** Two different things. The Parametric Model Maker is an OpenSCAD customizer in the browser [B2]: exact when the uploaded .scad is good, but a person picks a model and moves sliders; no AI writes it (judgment). The AI side (Aimia hub, image-to-3D with Tripo, Hunyuan, Meshy, Rodin and others) charges 24 to 45 credits per model [B4] and was "coming soon" with a waitlist in All3DP's coverage [B3] (in flux). MakerLab's 2024 post lists Make My Sign, Make My Vase, Pixel Puzzle Maker, relief and an AI Scanner [B1]. Its official credit guide blocked automated reads; the numbers come from the zh-tw copy and a search summary (in flux).

**Creality.** CubeMe turns a photo into a chibi figure on Creality Cloud [C1]; docs mention text-to-3D with hole filling and retopology [C2] but no live product page was found (in flux). The SPARKX i7 (CES 2026) announces planned text or image generation [C3]. Its likely base, Hunyuan3D-2, is open weights (6 GB VRAM for shape, 16 GB with texture) [C4] (judgment on the link).

**Prusa / Printables.** No AI generator found [P1]. Printables lets users filter out AI-generated models and labels flagged ones [P1][P2] (not confirmed on Printables' own terms page, which blocked automated reads).

**OpenSCAD, CadQuery and build123d MCP servers.** Tools that let any model render and export what it writes: RobertCoop/openscad-mcp (MIT, 173 stars, pushed 2026-09-10), jhacksman/OpenSCAD-MCP-Server (MIT, 216 stars, pushed 2026-11-11), CadQuery's own `mcp-server` in cadquery-contrib (MIT repo, 77 stars, pushed 2026-10-20; PyPI lists Apache-2.0, a conflict), pzfreo/build123d-mcp (Apache-2.0, 154 stars, pushed 2026-11-16) (all read via the GitHub API) [O1][O2][O3][O4][O5]. They add a render loop to the "frontier model writes code" rival; the baseline below has no such loop, so a model with one of these servers should do somewhat better on looks and on errors it can see (judgment). build123d-mcp is the only one with a printability check of its own [O5].

**Plain frontier model writing OpenSCAD or CadQuery.** Measured below for OpenSCAD. Published: frontier models write OpenSCAD that runs 99% of the time (P3D-Bench) [R10]; whether it matches the requested dimensions and prints is a different, much lower number (MUSE final scores 39 to 52 for the best models on multi-part CadQuery) [R9].

## Published evidence

Numbers exactly as the papers report them. None of these slices or prints a part (judgment: they check code execution, mesh validity or shape distance, not a slicer).

- **CADCodeVerify / CADPrompt** (arXiv 2410.05340, 200 objects, CadQuery): GPT-4 zero-shot compile rate 92.0%, 94.0% with CADCodeVerify; few-shot 96.0% and 96.5%; Gemini zero-shot 85.0%; CodeLlama 64.5%; GPT-4 IoGT 0.935 to 0.941 [R1].
- **Text2CAD** (arXiv 2409.17106, NeurIPS 2024, sketch-extrude): invalidity ratio 0.93% vs DeepCAD 10.00%; median Chamfer distance 0.37 (x10^3) [R2].
- **CAD-Coder** (arXiv 2505.14646, image to CadQuery, 100 test samples): valid syntax 100% and IoU 0.675 for CAD-Coder; GPT-4.5 84% and 0.524; o1 88% and 0.494; Gemini 2.0 Pro 82% and 0.445 [R3].
- **CAD-Recode** (arXiv 2412.14042, point cloud to CadQuery, about 2B): DeepCAD IoU 92.0%, invalidity 0.4%; cc-by-nc-4.0 weights [R4].
- **Text-to-CadQuery** (arXiv 2505.06507): Qwen2.5-3B Gemini-judged 69.30% vs Text2CAD 58.80% [R5].
- **CADmium** (arXiv 2507.09792): Qwen2.5-Coder 14B 83.80% watertight vs Text2CAD 95.74% (table as extracted, in flux) [R6].
- **CAD-Llama** (arXiv 2505.04481): 84.72 accuracy vs GPT-4 two-shot 20.03 [R7].
- **CADFusion** (arXiv 2501.19054, ICML 2025, 8B): invalidity 6.20% vs GPT-4o 8-shot 74.26% [R8].
- **MUSE** (arXiv 2605.28579, May 2026, 106 multi-part designs, CadQuery to STEP, watertight, manifold and self-intersection checks plus a judge): sandbox success and final score GPT-5.5 77.36 and 52.36, Claude Opus 4.7 76.42 and 39.47, Gemini 3.1 Pro 65.09 and 43.40, GPT-4o 36.79 and 5.03 [R9].
- **P3D-Bench** (arXiv 2606.11152, June 2026): text-to-OpenSCAD valid rate GPT-5.5 0.995, Gemini 3.1 Pro 0.998, Claude Opus 4.6 0.993; judge scores 0.945, 0.947, 0.921 [R10].
- **Text2CAD-Bench** (arXiv 2605.18430, May 2026, 600 examples): level 1 invalidity GPT-5.2 11.1%, Claude 4.5 Sonnet 20.3% [R11].
- **CadEval** (Will Patrick, April 2025, 25 OpenSCAD tasks, 15 models): Sonnet models best; per-model rates only in a chart (unknown as numbers) [R12].
- **Vendors:** Meshy's about 97% of figurines passing Bambu Studio validation (internal) [M5]. No other vendor publishes an accuracy or printability number (Zoo, Adam, Tripo, Bambu, Creality: none found).
- **smoothstone (Gantrel's engine):** LFM3-1.7B, sealed blind set 6, 5 seeds: S card 39/40, compacted 38/40, no card 37/40; LFM3-1.7B writing OpenSCAD itself: 0/15 [L2][L3].

## Hands-on: frontier model writes OpenSCAD directly

**What was run.** Opus 5.5 (this delegate) wrote each part's OpenSCAD in one pass from the PARTSET.md request, with no smoothstone recipes and no BOSL2, plain OpenSCAD only. Nothing was changed after rendering: no file needed a syntax fix, and the geometry bugs below are left in on purpose. Each file was rendered with OpenSCAD 2026.11.14 (Manifold backend) to STL by `baseline/run.sh` after the 1-minute load dropped under 8 (renders ran 13:50 to 14:21; the load sat at 15 to 35 from other sessions before that). Then `stone check` ran (`p*/check.txt`), then `baseline/measure.py` (`p*/measure.json`, `measure.txt`), then `baseline/views.sh` made the fixed renders (`p*/view_iso.png`, `view_top.png`, `view_front.png`; all 12 iso views in `baseline/contact_iso.jpg`). All 36 renders were looked at.

**Bias to discount.** The writer of these 12 files also wrote the checks, in the same session, so it knew what would be measured. Treat 8 of 12 as an upper bound for "a frontier model given only the request". For a clean number the lead runs a fresh session per part with only the request text (EVAL-PLAN.md's rival check).

| # | Part | stone check | Dimension and fit checks | Render (s) | What went wrong | Files |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Fan guard | PASS | 8/8: 120.0 x 120.0 x 2.5 mm, four 4.3 mm holes on 105.0 mm, largest opening 8.97 mm, 71.6% open | 0.2 | Nothing; partial hex cells at the rim | `baseline/p01_fan_guard/` |
| 2 | Dowel hook | PASS | 4/4: width 30.0, cradle 26.4 mm, holes 4.49 mm, tip 11.6 mm past center | 0.1 | Nothing | `baseline/p02_rod_hook/` |
| 3 | Gridfinity bin | FAIL: open surface, 2 bodies | 6/6: 83.5 x 41.5 x 46.4 mm, feet 35.7 mm, 2 cavities, label shelf 7.8 mm | 0.2 | A zero-volume sheet at z = 40.2 mm where the shelf top, divider top and lip chamfer meet on one plane | `baseline/p03_gridfinity/` |
| 4 | Cable comb | PASS | 4/4: 6 slots at 10.0 mm, seats 6.23 mm, mouths 5.4 mm | 0.1 | Nothing | `baseline/p04_cable_comb/` |
| 5 | Spur gear | FAIL: 49 bodies, open surface, 0.07 mm wall | 4/8: thickness 8.0, root 32.24 mm and bore 5.2 / 4.6 mm pass; tip 33.79 mm (want 39.0), 48 slivers counted as teeth, tooth 0.34 mm thick (want 2.356), 0.485 mm2 overlap with its mate | 0.1 | Tooth polygon points in the wrong order: each tooth is a bow-tie that splits into slivers; the render shows spikes | `baseline/p05_spur_gear/` |
| 6 | Phone stand | PASS | 5/5: width 80.0, rest at 70 degrees, 9 mm phone seats with 2.2 mm play, cable slot 145.4 mm2 | 0.0 | Nothing (heavy solid web, 188 cm3) | `baseline/p06_phone_stand/` |
| 7 | Pi 5 lid (off-catalog) | PASS | 9/9: 91.0 x 62.0 x 16.0 mm, plate 1.95 mm, holes 2.7 mm on 58.0 x 49.0, vents 600 mm2 centered | 0.1 | Nothing | `baseline/p07_pi5_lid/` |
| 8 | L bracket (off-catalog) | PASS | 9/9: legs 50.0, width 25.0, holes 4.49 mm at 15.0 and 40.0 mm, countersinks 8.87 mm, gussets | 0.1 | Nothing | `baseline/p08_l_bracket/` |
| 9 | Twisted vase | FAIL: volume -522083 mm3, first layer 0 mm2, overhang 5.6% | 4/4: height 180.0, widest 79.97 mm, solid, base 2004 mm2 | 0.6 | Polyhedron faces wound the wrong way (inside out); stone reads no volume and no first layer | `baseline/p09_vase/` |
| 10 | Ribbed planter | PASS | 3/3: inner rim 105.14 mm, depth 95.0 mm, drain 11.98 mm | 0.1 | Nothing | `baseline/p10_planter/` |
| 11 | Name keychain | PASS | 4/4: width 48.73 mm, ring hole 4.99 mm, letters 1.2 mm proud, OCR reads EMMA (`ocr_cut.png`) | 0.9 | Nothing | `baseline/p11_keychain/` |
| 12 | Lithophane | FAIL: 124 bodies, lowest point z = -8.0 mm, 0.47 mm wall | 4/6: picture 100.0 mm wide (frame registered at 4.0), r 0.80 against the picture vs 0.16 mirrored, foot 25.0 mm; thinnest 1.0 mm (want 0.8), thickest 2.2 mm (want 3.0) | 5.1 | The relief broke into 123 loose islands off the plate, the foot sits 8 mm below the bed, and the thickness range came out 1.0 to 2.2 mm instead of 0.8 to 3.0 (cause not traced; the surface() scaling is the likely spot, judgment) | `baseline/p12_lithophane/` |

**Totals.** stone check: 8 of 12 PASS (`baseline/p*/check.txt`). Dimension and fit checks all passing: 10 of 12 (gear and lithophane fail) (`baseline/measure.txt`). Both: 8 of 12: fan guard, hook, comb, phone stand, Pi 5 lid, L bracket, planter, keychain. Slicer (lead, 2026-11-23, OrcaSlicer 2.5.0 pinned profile, `../slicer/slicecheck.json`): 9 of 12 (fails p01 fan guard at 0.946 solid/mesh, p06 phone stand floating cantilever, p09 vase inside out). All three checks: 6 of 12: hook, comb, Pi 5 lid, L bracket, planter, keychain. Looks: renders made, not judged (the judge needs Gantrel's parts to pair against).

**Reading.** The direct baseline is strong on prismatic parts with stated numbers (fan guard, hook, comb, stand, Pi lid, bracket, planter, keychain): every exact millimeter number in a request landed within 0.05 mm of it, because OpenSCAD takes the request's numbers literally. It fails where the part needs a construction a model must get exactly right in one pass with no feedback: an involute tooth outline, polyhedron face winding, coplanar unions, and a picture surface. Each of those four failures is one a render-and-check loop would catch in one more step (judgment). That is what smoothstone's recipes remove for small models and what `stone build` catches for large ones.

## Where the bar is

What Gantrel must beat at each tier, using only the evidence above. Numbers from this study carry their file; the rest are cited or labeled judgment.

**Tier L (frontier models).** The rival is the same frontier model writing OpenSCAD or CadQuery without Gantrel. Measured here: 8 of 12 pass both stone and every dimension check, with the bias noted above (`baseline/measure.txt`). Published: frontier OpenSCAD runs 99% of the time [R10], but the best multi-part CadQuery final scores are 39 to 52 [R9]. Bar for Gantrel at L:
- Pass stone, slicer and dimension checks on at least 11 of 12, so it clearly beats 8 of 12. The four baseline failures (gear, vase, Gridfinity bin, lithophane) all have catalog recipes, so tier L should pass them through `stone build` (judgment until the lead's run).
- The two off-catalog parts (Pi 5 lid, L bracket) passed as plain OpenSCAD in one shot. Gantrel can only tie there on checks. It wins them only on looks (rounded edges, chamfers, a finished feel) or by its new-recipe path passing `tools/sheet.py`.
- Looks: EVAL-PLAN.md asks for 8 of 12 blind wins against the best rival. On the eight prismatic parts the baseline is plain but correct, so the looks judge decides those; this is the hardest part of the L bar (judgment).

**Tier M (4B to 30B local models).** No rival product runs a local model at this size for parts. The rivals are open-weights research models writing CadQuery or sketch sequences: CADmium Qwen2.5-Coder 14B is 83.80% watertight on CADPrompt [R6], Text-to-CadQuery Qwen2.5-3B is 69.30% judged correct [R5], and CAD-Coder 13B reaches 100% valid syntax from images [R3]. None checks dimensions against a request or slices. Bar: EVAL-PLAN.md's 45 of 50 runs, plus 8 of 10 picture-to-part specs. Not measured head to head here (these models need a GPU; chapel1 has none).

**Tier S (about 3B and under).** No rival exists at this size for printable parts. LFM3-1.7B writing OpenSCAD itself went 0 for 15, and with smoothstone 39 of 40 [L2][L3]. The bar is smoothstone's own sealed-set numbers; holding them through Gantrel's plugin wrapper is the test.

**Where Gantrel cannot win today.**
- Organic and sculpted shapes (a figurine, a pet, a face, a character keychain in 3D): Meshy, Tripo and the Hunyuan3D-2 and TripoSG open weights make meshes from a picture [M1][T1][T4][C4]; Gantrel's recipes cover outlines and height maps only [L2].
- Image to full 3D mesh: Gantrel traces a silhouette or makes a height map [L2]. It has no image-to-volume path.
- B-Rep STEP with a feature tree for a CAD workflow: Zoo produces it [Z3][Z4]. Gantrel's output is OpenSCAD and STL [L2] (build123d or CadQuery is planned, not built [L1]).
- Off-catalog parts at tiers S and M: only tier L writes new recipes [L2], so a small local model asked for a Pi lid or a bracket has no recipe to pick.
- Multi-part assemblies (MUSE's whole benchmark [R9]): no recipe makes mating parts as one job (judgment from the recipe list [L2]).

## Slicer check needs (decided)

Decided 2026-11-22: OrcaSlicer 2.5.0, the Linux AppImage extracted in the user's folder (no system install), pinned by SHA-256 d13eb0a9cd618ed080ab6b82c64c0a60c4501aac91e88d5ac2c3b526fb141a7c, matching GitHub's digest. 2.5.0 over 2.5.1 because it had been out longer. PrusaSlicer ships no Linux binary on GitHub. Profiles and runner: [../slicer/](../slicer/). The pinned rule is in [../README.md](../README.md#slicer-check-pinned-2026-11-22).

## Not done

- No rival ran hands-on except the direct-OpenSCAD baseline: Zoo, Adam (hosted), Meshy, Tripo, Bambu and Creality need accounts.
- CadQuery baseline: not run. CadQuery and build123d are not installed on chapel1 and installing a package stack was out of scope.
- Open-source local models (CAD-Recode, Text-to-CadQuery, CADmium, TripoSR): not run; chapel1 has no GPU and the job did not ask for it.
- Vendor galleries were not collected as files; their pages are cited where read.
- Blind looks judging: renders for the baseline are made (`baseline/p*/view_*.png`); the pairwise judge runs only once Gantrel's parts exist.

## Sources

All read 2026-11-17.

Gantrel and smoothstone
- [L1] ~/Projects/Gantrel/README.md and docs/STACK.md
- [L2] ~/Projects/smoothstone/README.md
- [L3] ~/Projects/smoothstone/LOG.md (blind sets 5 and 6)
- [L4] https://github.com/kennetek/gridfinity-rebuilt-openscad/blob/main/src/core/standard.scad (read via GitHub API)
- [L5] https://pip-assets.raspberrypi.com/categories/892-raspberry-pi-5/documents/RP-008347-DS-1-raspberry-pi-5-mechanical-drawing.pdf
- [L6] https://images.nasa.gov/details/S99-00858 (image https://images-assets.nasa.gov/image/S99-00858/S99-00858~medium.jpg)
- [L7] https://api.github.com/repos/SoftFever/OrcaSlicer/releases/latest and https://api.github.com/repos/prusa3d/PrusaSlicer/releases
- [L8] https://github.com/OrcaSlicer/OrcaSlicer/wiki/cli_mode
- [L9] https://dyn.manpages.debian.org/trixie/prusa-slicer/prusa-slicer.1

Mesh generators
- [M1] https://docs.meshy.ai/en/api/text-to-3d
- [M2] https://docs.meshy.ai/en/webapp/pricing
- [M3] https://help.meshy.ai/en/articles/12062933-what-are-your-prices-and-plans-offered-and-do-you-have-monthly-annual-plans
- [M4] https://docs.meshy.ai/en/webapp/guides/use-cases/3d-printing
- [M5] https://www.3dnatives.com/en/meshys-3d-agent-21072026/
- [T1] https://developers.tripo3d.ai/
- [T2] https://developers.tripo3d.ai/pricing
- [T3] https://github.com/VAST-AI-Research/TripoSR
- [T4] https://github.com/VAST-AI-Research/TripoSG
- [T5] https://www.costbench.com/software/ai-3d-generation/tripo-ai/
- [T6] https://toolradar.com/tools/tripo-ai/pricing
- [T7] https://www.tripo3d.ai/3d-print/ai-3d-printable-model-generator-with-high-polygon-count-accuracy-a-2026-guide (search snippet; page not opened)
- [B1] https://blog.bambulab.com/new-launch-of-makerlab-experiments
- [B2] https://all3dp.com/4/bambu-labs-parametric-model-maker-brings-openscad-to-makerworld/
- [B3] https://all3dp.com/4/bambu-lab-unites-top-ai-3d-generators-in-makerworld-with-the-new-aimia-hub/
- [B4] https://makerworld.com/zh-tw/makerlab-credit-full-guide
- [C1] https://wiki.creality.com/en/printers-general-documents/2d-image-into-3d-model
- [C2] https://creality-docs.gitbook.io/global/intro/key-features
- [C3] https://www.3dprintingindustry.com/news/creality-showcases-ai-enhanced-desktop-3d-printing-with-sparkx-i7-247970/
- [C4] https://github.com/Tencent-Hunyuan/Hunyuan3D-2
- [P1] https://forum.prusa3d.com/forum/english-forum-general-discussion-announcements-and-releases/printables-filter-out-ai-models
- [P2] https://www.fabbaloo.com/?p=241612

CAD generators and tools
- [Z1] https://zoo.dev/text-to-cad
- [Z2] https://zoo.dev/zoo-pricing
- [Z3] https://zoo.dev/docs/zoo-design-studio/zookeeper
- [Z4] https://zoo.dev/research/introducing-text-to-cad
- [Z5] https://github.com/KittyCAD/modeling-app
- [A1] https://github.com/Adam-CAD/CADAM (README and repo metadata via GitHub API)
- [A2] https://adam.new/pricing
- [A3] https://docs.adam.new
- [O1] https://github.com/RobertCoop/openscad-mcp
- [O2] https://github.com/jhacksman/OpenSCAD-MCP-Server
- [O3] https://github.com/CadQuery/cadquery-contrib
- [O4] https://pypi.org/project/cadquery-mcp/
- [O5] https://github.com/pzfreo/build123d-mcp

Research
- [R1] https://arxiv.org/html/2410.05340v2
- [R2] https://arxiv.org/html/2409.17106v1
- [R3] https://arxiv.org/html/2505.14646v1 and https://github.com/anniedoris/CAD-Coder
- [R4] https://arxiv.org/html/2412.14042v2 and https://huggingface.co/filapro/cad-recode
- [R5] https://arxiv.org/html/2505.06507v1 and https://github.com/Text-to-CadQuery/Text-to-CadQuery
- [R6] https://arxiv.org/html/2507.09792v3
- [R7] https://arxiv.org/html/2505.04481v2
- [R8] https://arxiv.org/html/2501.19054v2 and https://github.com/microsoft/CADFusion
- [R9] https://arxiv.org/html/2605.28579v1
- [R10] https://arxiv.org/html/2606.11152v1
- [R11] https://arxiv.org/html/2605.18430v1
- [R12] https://willpatrick.xyz/technology/2025/04/23/teaching-llms-how-to-solid-model.html and https://github.com/wgpatrick/cadeval
