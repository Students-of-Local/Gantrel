import * as THREE from "three";
import { OrbitControls } from "/static/vendor/three/OrbitControls.js";
import { STLLoader } from "/static/vendor/three/STLLoader.js";
import { RoomEnvironment } from "/static/vendor/three/RoomEnvironment.js";

const token = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);
const CLAY = new THREE.Color("#d6a25e").convertSRGBToLinear();
const OVERHANG = new THREE.Color("#e0533f").convertSRGBToLinear();
const PLATE_MM = 220;

const ui = {
  state: null,
  recipe: null,
  schemaKey: "",
  partUrl: "",
  partDiag: 0,
  overhangs: false,
  sentAt: 0,
};
window.__studio = ui;

function api(path) {
  return `${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(token)}`;
}

async function post(path, body) {
  let problem = "";
  try {
    const response = await fetch(api(path), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Gantrel-Token": token },
      body: JSON.stringify(body || {}),
    });
    if (!response.ok) problem = await response.text();
  } catch (error) {
    problem = String(error);
  }
  if (!problem) return;
  if (ui.state) renderState(ui.state);
  showError("The studio refused that change.", problem);
}

const canvas = $("view");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;

const scene = new THREE.Scene();
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 10000);
camera.up.set(0, 0, 1);
camera.position.set(160, -200, 150);
const controls = new OrbitControls(camera, canvas);
controls.addEventListener("change", render);

scene.add(new THREE.HemisphereLight(0xffffff, 0x444033, 0.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.6);
sun.position.set(120, -160, 260);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
sun.shadow.radius = 4;
scene.add(sun, sun.target);

const plate = new THREE.Group();
const grid = new THREE.GridHelper(PLATE_MM, PLATE_MM / 10);
grid.rotation.x = Math.PI / 2;
grid.material.transparent = true;
grid.material.opacity = 0.35;
const ground = new THREE.Mesh(new THREE.PlaneGeometry(PLATE_MM * 4, PLATE_MM * 4), new THREE.ShadowMaterial({ opacity: 0.28 }));
ground.receiveShadow = true;
plate.add(ground, grid);
scene.add(plate);

const material = new THREE.MeshPhysicalMaterial({ color: 0xffffff, vertexColors: true, roughness: 0.42, metalness: 0, clearcoat: 0.25, clearcoatRoughness: 0.5 });
let mesh = null;

function themeColors() {
  const css = getComputedStyle(document.documentElement);
  scene.background = new THREE.Color(css.getPropertyValue("--view").trim() || "#1a1613");
  const line = new THREE.Color(css.getPropertyValue("--muted").trim() || "#a8907b");
  grid.material.color = line;
  render();
}

function render() {
  renderer.render(scene, camera);
}

function resize() {
  const box = canvas.parentElement.getBoundingClientRect();
  if (!box.width || !box.height) return;
  renderer.setSize(box.width, box.height, false);
  camera.aspect = box.width / box.height;
  camera.updateProjectionMatrix();
  render();
}
new ResizeObserver(resize).observe(canvas.parentElement);
matchMedia("(prefers-color-scheme: light)").addEventListener("change", themeColors);
themeColors();

function paint(geometry) {
  const normals = geometry.getAttribute("normal");
  const positions = geometry.getAttribute("position");
  const colors = new Float32Array(positions.count * 3);
  const limit = -Math.SQRT1_2;
  for (let i = 0; i < positions.count; i += 3) {
    const lowest = Math.min(positions.getZ(i), positions.getZ(i + 1), positions.getZ(i + 2));
    const hangs = ui.overhangs && normals.getZ(i) < limit && lowest > 0.2;
    const color = hangs ? OVERHANG : CLAY;
    for (let k = 0; k < 3; k += 1) color.toArray(colors, (i + k) * 3);
  }
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
}

function view(name) {
  if (!mesh) return;
  const sphere = mesh.geometry.boundingSphere;
  const center = sphere.center.clone();
  const fov = THREE.MathUtils.degToRad(camera.fov);
  const half = Math.atan(Math.tan(fov / 2) * Math.min(camera.aspect, 1));
  const distance = (sphere.radius / Math.sin(half)) * 1.08;
  const directions = {
    fit: camera.position.clone().sub(controls.target).normalize(),
    iso: new THREE.Vector3(0.9, -1.3, 1.0).normalize(),
    top: new THREE.Vector3(0, -0.0001, 1).normalize(),
    front: new THREE.Vector3(0, -1, 0.0001).normalize(),
  };
  const direction = directions[name] || directions.iso;
  if (direction.lengthSq() === 0) direction.copy(directions.iso);
  camera.position.copy(center).addScaledVector(direction, distance);
  camera.near = Math.max(0.1, distance / 100);
  camera.far = distance * 100;
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.update();
  sun.target.position.copy(center);
  sun.shadow.camera.left = -sphere.radius * 1.5;
  sun.shadow.camera.right = sphere.radius * 1.5;
  sun.shadow.camera.top = sphere.radius * 1.5;
  sun.shadow.camera.bottom = -sphere.radius * 1.5;
  sun.shadow.camera.far = 1000 + sphere.radius * 4;
  sun.shadow.camera.updateProjectionMatrix();
  render();
}

async function loadPart(url) {
  const response = await fetch(api(url), { headers: { "X-Gantrel-Token": token } });
  if (!response.ok) return;
  const geometry = new STLLoader().parse(await response.arrayBuffer());
  geometry.computeBoundingBox();
  const box = geometry.boundingBox;
  geometry.translate(-(box.min.x + box.max.x) / 2, -(box.min.y + box.max.y) / 2, -box.min.z);
  geometry.computeBoundingBox();
  geometry.computeBoundingSphere();
  paint(geometry);
  const first = !mesh;
  if (mesh) {
    scene.remove(mesh);
    mesh.geometry.dispose();
  }
  mesh = new THREE.Mesh(geometry, material);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  scene.add(mesh);
  ui.faces = geometry.getAttribute("position").count / 3;
  const diag = geometry.boundingSphere.radius;
  const jump = ui.partDiag && Math.abs(diag - ui.partDiag) / ui.partDiag > 0.3;
  ui.partDiag = diag;
  if (first) view("iso");
  else if (jump) view("fit");
  else render();
}

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => view(button.dataset.view));
});
$("overhangs").addEventListener("click", (event) => {
  ui.overhangs = !ui.overhangs;
  event.currentTarget.setAttribute("aria-pressed", String(ui.overhangs));
  if (mesh) {
    paint(mesh.geometry);
    render();
  }
});

const SUFFIX = { d: "diameter", r: "radius", w: "width", h: "height", t: "thickness" };
function humanize(name) {
  const words = name.split("_").map((word) => SUFFIX[word] || word);
  const text = words.join(" ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function conditions(schema) {
  const choices = new Map(schema.filter((p) => p.type === "choice").map((p) => [p.name, p.choices || []]));
  return schema.map((param) => {
    const match = /^([\w-]+) (\w+): (.*)$/.exec(param.help || "");
    if (match && choices.has(match[2]) && choices.get(match[2]).includes(match[1])) {
      return { param, when: { name: match[2], value: match[1] }, help: match[3] };
    }
    return { param, when: null, help: param.help || "" };
  });
}

function trim(value) {
  if (typeof value !== "number") return String(value);
  return String(Math.round(value * 100) / 100);
}

function stepFor(param) {
  if (param.step) return param.step;
  if (param.type === "int") return 1;
  return Number.isFinite(param.min) && Number.isFinite(param.max) && param.max - param.min > 50 ? 1 : 0.1;
}

function control(entry, value) {
  const { param } = entry;
  let help = entry.help;
  const id = `p-${param.name}`;
  const wrap = document.createElement("div");
  wrap.className = "param";
  wrap.dataset.name = param.name;
  const row = document.createElement("div");
  row.className = "row";
  const label = document.createElement("label");
  label.htmlFor = id;
  label.textContent = humanize(param.name);
  row.append(label);
  wrap.append(row);
  if (param.type === "choice") {
    const select = document.createElement("select");
    select.id = id;
    select.name = param.name;
    for (const choice of param.choices || []) {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = humanize(String(choice));
      select.append(option);
    }
    select.value = value;
    select.addEventListener("change", () => {
      applyConditions();
      send(param);
    });
    row.append(select);
  } else if (param.type === "bool") {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.id = id;
    box.name = param.name;
    box.checked = Boolean(value);
    box.addEventListener("change", () => send(param));
    row.append(box);
  } else if (param.type === "number" || param.type === "int") {
    const out = document.createElement("span");
    out.className = "value";
    const field = document.createElement("input");
    field.type = "number";
    field.id = id;
    field.name = param.name;
    const bounded = Number.isFinite(param.min) && Number.isFinite(param.max);
    if (Number.isFinite(param.min)) field.min = param.min;
    if (Number.isFinite(param.max)) field.max = param.max;
    field.step = stepFor(param);
    field.value = trim(value);
    field.inputMode = "decimal";
    out.append(field);
    if (param.unit) out.append(document.createTextNode(param.unit));
    row.append(out);
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = param.min;
    slider.max = param.max;
    slider.step = stepFor(param);
    slider.value = value;
    slider.tabIndex = -1;
    slider.setAttribute("aria-label", `${humanize(param.name)} slider`);
    slider.addEventListener("input", () => {
      field.value = trim(Number(slider.value));
    });
    slider.addEventListener("change", () => send(param));
    let last = Number(value);
    field.addEventListener("change", () => {
      const typed = Number(field.value);
      if (field.value.trim() === "" || !Number.isFinite(typed)) {
        field.value = trim(last);
        return;
      }
      let number = typed;
      if (Number.isFinite(param.min)) number = Math.max(param.min, number);
      if (Number.isFinite(param.max)) number = Math.min(param.max, number);
      last = number;
      field.value = trim(number);
      slider.value = number;
      send(param);
    });
    if (bounded) wrap.append(slider);
  } else if (param.type === "text" || param.type === "string") {
    const field = document.createElement("input");
    field.type = "text";
    field.id = id;
    field.name = param.name;
    field.maxLength = 200;
    field.value = value ?? "";
    field.addEventListener("change", () => send(param));
    row.append(field);
  } else {
    // Pictures name files on disk, so they change only from the chat (stone image).
    const shown = document.createElement("span");
    shown.className = "fixed";
    shown.textContent = value ? String(value).split("/").pop() : "none";
    row.append(shown);
    help = help ? `${help}. Change it from the chat.` : "Change it from the chat.";
    wrap.classList.add("from-chat");
  }
  if (help) {
    const note = document.createElement("p");
    note.className = "help";
    note.textContent = help;
    wrap.append(note);
  }
  return wrap;
}

function buildForm(state) {
  const form = $("params");
  form.replaceChildren();
  const entries = conditions(state.schema || []);
  ui.entries = entries;
  const main = document.createElement("fieldset");
  const legend = document.createElement("legend");
  legend.textContent = "Shape";
  main.append(legend);
  const groups = new Map();
  for (const entry of entries) {
    const value = state.params?.[entry.param.name] ?? entry.param.default;
    if (!entry.when) {
      main.append(control(entry, value));
      continue;
    }
    const key = `${entry.when.name}=${entry.when.value}`;
    if (!groups.has(key)) {
      const set = document.createElement("fieldset");
      set.dataset.when = key;
      const title = document.createElement("legend");
      title.textContent = `${humanize(entry.when.value)} options`;
      set.append(title);
      groups.set(key, set);
    }
    groups.get(key).append(control(entry, value));
  }
  form.append(main, ...groups.values());
  applyConditions();
  $("reset").hidden = entries.length === 0;
}

function applyConditions() {
  const form = $("params");
  form.querySelectorAll("fieldset[data-when]").forEach((set) => {
    const [name, value] = set.dataset.when.split("=");
    const field = form.elements.namedItem(name);
    set.hidden = !field || field.value !== value;
  });
}

function readField(param) {
  const field = $("params").elements.namedItem(param.name);
  if (param.type === "bool") return field.checked;
  if (param.type === "number") return Number(field.value);
  if (param.type === "int") return Math.round(Number(field.value));
  return field.value;
}

// Only the changed setting goes to the server; stone keeps every other value in plan.json.
function send(param) {
  ui.sentAt = Date.now();
  setStatus("Building", "");
  post("/api/params", { params: { [param.name]: readField(param) } });
}

function syncForm(state) {
  const form = $("params");
  for (const { param } of ui.entries || []) {
    if (!(param.name in (state.params || {}))) continue;
    const value = state.params[param.name];
    const wrap = form.querySelector(`.param[data-name="${CSS.escape(param.name)}"]`);
    if (!wrap || wrap.contains(document.activeElement)) continue;
    const field = form.elements.namedItem(param.name);
    if (!field) {
      const shown = wrap.querySelector(".fixed");
      if (shown) shown.textContent = value ? String(value).split("/").pop() : "none";
      continue;
    }
    if (param.type === "bool") field.checked = Boolean(value);
    else if (param.type === "number" || param.type === "int") {
      field.value = trim(Number(value));
      const slider = wrap.querySelector('input[type="range"]');
      if (slider) slider.value = value;
    } else field.value = value;
  }
  applyConditions();
}

function setStatus(text, tone) {
  const status = $("status");
  status.textContent = text;
  status.className = `status ${tone}`.trim();
}

function showError(text, log) {
  $("error").hidden = !text;
  $("error-text").textContent = text || "";
  $("error-log").textContent = log || "";
}

const CHECK_NAMES = { loads: "Loads", watertight: "Watertight", bodies: "One piece", volume: "Volume", base: "Flat base", flat_base: "Flat base", walls: "Walls", overhang: "Overhangs", size: "Size", dims: "Matches the recipe" };

function renderChecks(check) {
  const list = $("checks");
  list.replaceChildren();
  const results = check?.results || [];
  for (const result of results) {
    const item = document.createElement("li");
    item.className = result.ok ? "" : "fail";
    const verdict = document.createElement("b");
    verdict.textContent = result.ok ? "Pass" : "Fail";
    const text = document.createElement("span");
    text.textContent = `${CHECK_NAMES[result.name] || humanize(result.name)}: ${result.detail || ""}`;
    if (!result.ok && result.advice) {
      const advice = document.createElement("span");
      advice.className = "advice";
      advice.textContent = result.advice;
      text.append(advice);
    }
    item.append(verdict, text);
    list.append(item);
  }
  const failing = results.filter((r) => !r.ok);
  const failed = failing.length;
  $("checks-summary").textContent = results.length ? `Print checks (${results.length - failed} of ${results.length} pass)` : "Print checks";
  if (failed) $("checks-box").open = true;
  const first = failing[0] ? CHECK_NAMES[failing[0].name] || humanize(failing[0].name) : "";
  return { total: results.length, failed, first };
}

function renderState(state) {
  ui.state = state;
  const exists = Boolean(state.exists && state.recipe);
  $("empty").hidden = exists || Boolean(state.part);
  $("title").textContent = exists ? `${state.title || humanize(state.recipe)} · ${state.recipe}` : "Gantrel Studio";
  const size = state.part?.size_mm || state.check?.size_mm;
  $("dims").textContent = size ? `${size.map(trim).join(" × ")} mm` : " ";
  $("request").textContent = state.request ? `Asked for: ${state.request}` : "";
  const key = `${state.recipe}|${(state.schema || []).map((p) => p.name).join(",")}`;
  if (key !== ui.schemaKey) {
    ui.schemaKey = key;
    buildForm(state);
  } else {
    syncForm(state);
  }
  const { total, failed, first } = renderChecks(state.check);
  showError(state.error ? "The last build failed. The part shown is the last one that built." : "", state.error || "");
  if (!exists) setStatus("No part yet", "");
  else if (state.building) setStatus("Building", "");
  else if (state.error) setStatus("Build failed", "bad");
  else if (failed) setStatus(`Not ready to print · ${first}${failed > 1 ? ` and ${failed - 1} more` : ""} failed`, "bad");
  else if (state.check?.passed) setStatus(`Ready to print · all ${total} checks pass`, "ok");
  else setStatus("Not built yet", "");
  const hasPart = Boolean(state.part);
  for (const id of ["export-stl", "export-3mf"]) {
    const link = $(id);
    link.setAttribute("aria-disabled", String(!hasPart));
    link.href = hasPart ? api(`/api/export/part.${id.endsWith("3mf") ? "3mf" : "stl"}`) : "#";
  }
  if (state.part && state.part.url !== ui.partUrl) {
    ui.partUrl = state.part.url;
    loadPart(state.part.url);
  }
}

$("reset").addEventListener("click", () => {
  setStatus("Building", "");
  post("/api/reset");
});
$("params").addEventListener("submit", (event) => event.preventDefault());

if (!token) {
  setStatus("Open the link that gantrel-studio printed; it carries the access key.", "bad");
} else {
  const events = new EventSource(api("/api/events"));
  events.addEventListener("state", (event) => renderState(JSON.parse(event.data)));
  events.onerror = () => setStatus("Reconnecting to the studio", "");
}
