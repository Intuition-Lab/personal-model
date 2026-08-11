import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";
import { MODEL_COLORS as COLORS, MODEL_PALETTE } from "./palette.mjs";
import {
  coalesceProjectedLines,
  computeClusterLayout,
  fittedOverviewPose,
  pickScreenTarget,
  zoomMath,
} from "./layout.mjs";
import {
  focusKeysForSelection,
  handleSearchShortcut,
  lineKnownAt,
  modelPollingFingerprint,
  pointKnownAt,
  pointSearchMetadata,
  pointVisibleAt,
  pointerUpOutcome,
  prepareSearchEntries,
  rankSearchEntries,
  reconcileSceneSelection,
  recoverInvalidSceneSelection,
  shouldHandleModelGesture,
} from "./explore.mjs";
import {
  evidenceBreadcrumb,
  evidenceRequestPath,
  evidenceOverview,
  indexLinePresentations,
  modelNodeLabelIndex,
  nodeEvidenceCards,
  nodeHistoryCards,
  relationLabel,
} from "./evidence.mjs";
import {
  CONSTELLATION_CARD_HEIGHT,
  CONSTELLATION_CARD_WIDTH,
  CONSTELLATION_FILE_NAME,
  HUMAN_CARD_HEIGHT,
  HUMAN_CARD_WIDTH,
  HUMAN_CARD_FILE_NAME,
  buildXIntentUrl,
  drawConstellationCard,
  drawHumanCard,
} from "./share.mjs";

const canvasHost = document.getElementById("canvas");
const viewerEl = document.getElementById("viewer");
const statusEl = document.getElementById("status");
const detailEl = document.getElementById("detail");
const detailKindEl = document.getElementById("detail-kind");
const detailProvenanceEl = document.getElementById("detail-provenance");
const detailTitleEl = document.getElementById("detail-title");
const detailSummaryEl = document.getElementById("detail-summary");
const detailMetaEl = document.getElementById("detail-meta");
const detailReceiptsEl = document.getElementById("detail-receipts");
const detailHistoryEl = document.getElementById("detail-history-list");
const evidenceBreadcrumbsEl = document.getElementById("evidence-breadcrumbs");
const detailClaimInputEl = document.getElementById("detail-claim-input");
const detailHintEl = document.getElementById("detail-hint");
const detailStatusEl = document.getElementById("detail-status");
const detailActionsEl = document.getElementById("detail-actions");
const detailRejectEl = document.getElementById("detail-reject");
const detailEvidenceFoldEl = document.getElementById("detail-evidence-fold");
const detailHistoryFoldEl = document.getElementById("detail-history-fold");
const emptyEl = document.getElementById("empty");
const errorEl = document.getElementById("error");
const editAlertEl = document.getElementById("edit-alert");
const modelIdentityEl = document.getElementById("model-identity");
const slider = document.getElementById("as-of");
const sliderLabel = document.getElementById("as-of-label");
const zoomOutButton = document.getElementById("zoom-out");
const zoomResetButton = document.getElementById("zoom-reset");
const zoomInButton = document.getElementById("zoom-in");
const rotateButton = document.getElementById("rotate");
const cardButton = document.getElementById("human-card");
const shareButton = document.getElementById("share-x");
const shareNoticeEl = document.getElementById("share-notice");
const lineExplorerEl = document.getElementById("line-explorer");
const lineSelectEl = document.getElementById("line-select");
const searchPanelEl = document.getElementById("model-search-panel");
const searchInputEl = document.getElementById("model-search");
const searchResultsEl = document.getElementById("search-results");
const searchSummaryEl = document.getElementById("search-summary");
const searchEmptyEl = document.getElementById("search-empty");
const openSearchButton = document.getElementById("open-search");
const closeSearchButton = document.getElementById("close-search");
const clearFocusButton = document.getElementById("clear-focus");
const mobileGuideEl = document.getElementById("mobile-guide");
const layerCountEls = Object.fromEntries(
  ["points", "lines", "faces", "volumes", "root"]
    .map((kind) => [kind, document.getElementById(`layer-count-${kind}`)]),
);

const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(COLORS.canvas, 0.026);

const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 100);
// No `preserveDrawingBuffer`: it makes the browser copy the whole framebuffer
// every frame (tens of megabytes at 2x device pixel ratio) to serve readers
// that may never come. Both readers here — samplePixels() and
// createConstellationBlob() — read inside the same synchronous task as the
// render that produced the pixels, which is exactly when the buffer is still
// valid without it.
const renderer = new THREE.WebGLRenderer({
  antialias: true,
  alpha: true,
});
renderer.setClearColor(0x000000, 0);
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.04;
renderer.domElement.setAttribute("aria-hidden", "true");
canvasHost.appendChild(renderer.domElement);

const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(window.innerWidth, window.innerHeight);
labelRenderer.domElement.style.position = "absolute";
labelRenderer.domElement.style.inset = "0";
labelRenderer.domElement.style.pointerEvents = "none";
canvasHost.appendChild(labelRenderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.07;
// OrbitControls keeps its dolly, which is what a touchscreen pinch and a
// middle-button drag ride on, but it never sees the wheel: the viewer takes
// that in the capture phase below. Its wheel path applies the whole delta in
// one update and resets the accumulator, so wheel zoom landed in hard steps
// while orbit and pan glided under damping, and it normalises by
// `devicePixelRatio | 0`, which halves the gain on a Retina display — a full
// trackpad pinch moved the camera 5% — and divides by zero once the ratio
// drops below 1, where one notch teleports to the zoom limit.
controls.zoomToCursor = true;
controls.target.set(0, 2.2, 0);

scene.add(new THREE.HemisphereLight(COLORS.text, COLORS.canvas, 2.05));
const keyLight = new THREE.DirectionalLight(COLORS.text, 2.35);
keyLight.position.set(5, 11, 8);
scene.add(keyLight);
const pointLight = new THREE.PointLight(COLORS.points, 7, 22, 2);
pointLight.position.set(-5, -1, 3);
scene.add(pointLight);
const violetLight = new THREE.PointLight(COLORS.volumes, 8, 24, 2);
violetLight.position.set(5, 4, -4);
scene.add(violetLight);

let graph = new THREE.Group();
scene.add(graph);
let model = { points: [], lines: [], faces: [], volumes: [], root: null, stats: {} };
let modelPointById = new Map();
let sceneModel = { points: [], lines: [], faces: [], volumes: [], root: null };
let modelFingerprint = "";
let modelGeneratedAt = "";
let modelHealthFingerprint = "";
let cutoff = new Date();
let minTime = new Date();
let maxTime = new Date();
let playTimer = null;
let pointerDown = null;
// Which pointers are down, tracked apart from the click candidate. A gesture
// stays live whatever it turns out to be — a second finger retires the click
// candidate because a pinch must never select a node, and a right-drag is a
// pan that carries no click candidate at all — and hover picking and the graph
// poll must stay out of the way of all of them. Identities rather than a count:
// a release can land on a label instead of the canvas, and a counter that
// missed one would wedge closed for the rest of the session.
const livePointers = new Set();
let labelGesture = null;
let hoverPointer = null;
let hoverDirty = false;
let selected = null;
let selectedItem = null;
let selectionReturnFocus = null;
let detailMode = "node";
let evidenceTrail = [];
let evidenceRequest = 0;
let portraitMode = null;
let labels = [];
let pickables = [];
let screenLinePickables = [];
let lineNavigatorItems = [];
let selectionTargets = new Map();
let positions = new Map();
let items = new Map();
let searchItems = new Map();
let sceneNodeLabels = new Map();
let linePresentations = new Map();
let layerObjects = freshLayerObjects();
let currentLayout = null;
let layoutRadius = 6;
let framedRadius = 0;
let pulseGlows = [];
let focusLabels = [];
let focusLabelsKey = "";
let fitDistance = 12;
let zoomGoalDistance = null;
let zoomAnchor = null;
let canvasBounds = null;
let lastZoomPercent = null;
let lastFrameTime = performance.now();
let shareReady = false;
let modelLoadPromise = null;
let searchEntries = [];
let searchMatches = [];
let searchActiveIndex = 0;
let cameraFlight = null;
let focusVisualsSuspended = false;
const layerVisible = { points: true, lines: true, faces: true, volumes: true, root: true };
const kindLayers = {
  point: "points",
  context: "points",
  line: "lines",
  face: "faces",
  volume: "volumes",
  root: "root",
};
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const zoomDirection = new THREE.Vector3();
const zoomRaycaster = new THREE.Raycaster();
const zoomPlane = new THREE.Plane();
const zoomViewDirection = new THREE.Vector3();
const zoomAnchorBefore = new THREE.Vector3();
const zoomAnchorAfter = new THREE.Vector3();
const zoomShift = new THREE.Vector3();
const zoomAnchorNdc = new THREE.Vector2();
const pickWorldPosition = new THREE.Vector3();
const pickProjected = new THREE.Vector3();
const MIN_NODE_HIT_RADIUS_PX = 12;
const MIN_LINE_HIT_RADIUS_PX = 8;
const TOUCH_NODE_HIT_RADIUS_PX = 22;
const TOUCH_LINE_HIT_RADIUS_PX = 14;
const ZOOM_MIN_PERCENT = 50;
const ZOOM_MAX_PERCENT = 400;
const ZOOM_STEP_PERCENT = 25;
const EVIDENCE_CARD_LIMIT = 12;
const MODEL_GRAPH_TIMEOUT_MS = 45_000;
const SHARE_CARD_TIMEOUT_MS = 15_000;
const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function freshLayerObjects() {
  return { points: [], lines: [], hierarchy: [], faces: [], volumes: [], root: [] };
}

function hash(text) {
  let value = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    value ^= text.charCodeAt(i);
    value = Math.imul(value, 16777619);
  }
  return (value >>> 0) / 4294967296;
}

function addAtmosphere() {
  const count = 420;
  const positions = new Float32Array(count * 3);
  for (let index = 0; index < count; index += 1) {
    const radius = 10 + hash(`star:${index}:radius`) * 22;
    const theta = hash(`star:${index}:theta`) * Math.PI * 2;
    const phi = Math.acos(2 * hash(`star:${index}:phi`) - 1);
    positions[index * 3] = radius * Math.sin(phi) * Math.cos(theta);
    positions[index * 3 + 1] = radius * Math.cos(phi);
    positions[index * 3 + 2] = radius * Math.sin(phi) * Math.sin(theta);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const stars = new THREE.Points(
    geometry,
    new THREE.PointsMaterial({
      color: COLORS.focus,
      size: 0.035,
      transparent: true,
      opacity: 0.38,
      depthWrite: false,
      sizeAttenuation: true,
    })
  );
  stars.rotation.z = 0.18;
  scene.add(stars);
}

const glowTextures = new Map();

function glowTexture(color) {
  if (glowTextures.has(color)) return glowTextures.get(color);
  const canvas = document.createElement("canvas");
  canvas.width = 128;
  canvas.height = 128;
  const context = canvas.getContext("2d");
  const cssColor = `#${color.toString(16).padStart(6, "0")}`;
  const gradient = context.createRadialGradient(64, 64, 0, 64, 64, 64);
  gradient.addColorStop(0, `${cssColor}e6`);
  gradient.addColorStop(0.16, `${cssColor}7a`);
  gradient.addColorStop(0.5, `${cssColor}20`);
  gradient.addColorStop(1, `${cssColor}00`);
  context.fillStyle = gradient;
  context.fillRect(0, 0, 128, 128);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  glowTextures.set(color, texture);
  return texture;
}

function addGlow(position, color, size, opacity, layer, pulse = 0.06, focusRefs = []) {
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({
      map: glowTexture(color),
      color,
      transparent: true,
      opacity,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    })
  );
  sprite.position.copy(position);
  sprite.scale.setScalar(size);
  sprite.renderOrder = -1;
  sprite.userData.glowBase = size;
  sprite.userData.glowPhase = hash(`${position.x}:${position.y}:${position.z}`) * Math.PI * 2;
  sprite.userData.glowPulse = pulse;
  register(sprite, layer, focusRefs);
  pulseGlows.push(sprite);
  return sprite;
}

addAtmosphere();

function shortLabel(text, max = 34) {
  const value = String(text || "Untitled").replace(/\s+/g, " ").trim();
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function strongestIds(values, limit) {
  return new Set([...values].sort((left, right) => {
    const leftStrength = Number(left.observations || 0) * 2 + Number(left.confidence || 0);
    const rightStrength = Number(right.observations || 0) * 2 + Number(right.confidence || 0);
    return rightStrength - leftStrength || String(left.id).localeCompare(String(right.id));
  }).slice(0, limit).map((item) => item.id));
}

function itemTime(item) {
  const raw = item.valid_from || item.created_at || item.occurred_at;
  const value = raw ? new Date(raw) : null;
  return value && !Number.isNaN(value.getTime()) ? value : null;
}

function visibleAt(item) {
  const time = itemTime(item);
  return !time || time <= cutoff;
}

function register(object, layer, focusRefs = []) {
  object.userData.layer = layer;
  object.userData.focusRefs = focusRefs;
  layerObjects[layer].push(object);
  graph.add(object);
  return object;
}

function selectionKey(kind, id) {
  return `${kind}:${id}`;
}

function registerSelectionTarget(kind, id, target) {
  const key = selectionKey(kind, id);
  target.userData.ref = { kind, id };
  const targets = selectionTargets.get(key) || [];
  targets.push(target);
  selectionTargets.set(key, targets);
}

function registerPickable(object, layer, kind, item) {
  registerSelectionTarget(kind, item.id, object);
  register(object, layer);
  pickables.push(object);
  items.set(selectionKey(kind, item.id), item);
  searchItems.set(selectionKey(kind, item.id), item);
  return object;
}

function addLabel(text, position, layer, priority, kind, item, context = false) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = `model-label${context ? " context" : ""}`;
  element.textContent = shortLabel(text);
  element.title = String(text || "");
  element.dataset.kind = kind;
  element.setAttribute("aria-label", `Open ${kind} details: ${shortLabel(text, 80)}`);
  element.setAttribute("aria-controls", "detail");
  element.setAttribute("aria-expanded", "false");
  const label = new CSS2DObject(element);
  label.position.copy(position);
  label.userData.priority = priority;
  registerSelectionTarget(kind, item.id, label);
  element.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    // A label used to swallow the gesture outright, so the twenty of them on
    // screen were twenty places where a drag did nothing at all. Hand the
    // pointer to the canvas — OrbitControls captures it there and orbits — and
    // record what was under it so a gesture that turns out to be a click still
    // opens this label's node.
    event.preventDefault();
    // preventDefault also suppresses the focus the button would have taken, so
    // give it back: a mouse user who selects a node should still be able to
    // Tab onward from it.
    element.focus({ preventScroll: true });
    labelGesture = { kind, item };
    renderer.domElement.dispatchEvent(new PointerEvent("pointerdown", {
      pointerId: event.pointerId,
      pointerType: event.pointerType,
      isPrimary: event.isPrimary,
      button: event.button,
      buttons: event.buttons,
      clientX: event.clientX,
      clientY: event.clientY,
      // Modifiers pick the gesture: shift or ctrl turns OrbitControls' left
      // drag from an orbit into a pan, and a drag begun on a label has to mean
      // the same thing as one begun on bare canvas.
      ctrlKey: event.ctrlKey,
      shiftKey: event.shiftKey,
      altKey: event.altKey,
      metaKey: event.metaKey,
      bubbles: false,
      cancelable: true,
    }));
  });
  element.addEventListener("click", (event) => {
    event.stopPropagation();
    // Pointer-driven activation is resolved on the canvas above. Only keyboard
    // activation, which reports no pointer detail, still arrives here.
    if (event.detail !== 0) return;
    showDetails(kind, item);
  });
  register(label, layer);
  labels.push(label);
  return label;
}

function clearFocusLabels() {
  focusLabels.forEach((label) => {
    graph.remove(label);
    label.element.remove();
    labels = labels.filter((candidate) => candidate !== label);
    const layer = label.userData.layer;
    if (layerObjects[layer]) {
      layerObjects[layer] = layerObjects[layer].filter((candidate) => candidate !== label);
    }
    const ref = label.userData.ref;
    if (ref) {
      const key = selectionKey(ref.kind, ref.id);
      const remaining = (selectionTargets.get(key) || [])
        .filter((candidate) => candidate !== label);
      if (remaining.length) selectionTargets.set(key, remaining);
      else selectionTargets.delete(key);
    }
  });
  focusLabels = [];
}

function updateFocusLabels(focusKeys, activeKey) {
  const nextKey = activeKey
    ? `${activeKey}|${[...focusKeys].sort().join("|")}`
    : "";
  if (nextKey === focusLabelsKey) return;
  clearFocusLabels();
  focusLabelsKey = nextKey;
  if (!activeKey) return;

  const kindPriority = { root: 0, volume: 1, face: 2, point: 3, context: 4 };
  const candidates = [...focusKeys]
    .map((key) => {
      const separator = key.indexOf(":");
      return {
        key,
        kind: separator > 0 ? key.slice(0, separator) : "context",
        id: separator > 0 ? key.slice(separator + 1) : key,
      };
    })
    .filter(({ kind }) => kind !== "line")
    .sort((left, right) => (
      Number(right.key === activeKey) - Number(left.key === activeKey)
      || (kindPriority[left.kind] ?? 9) - (kindPriority[right.kind] ?? 9)
      || left.key.localeCompare(right.key)
    ));

  let labeled = candidates.filter(({ key }) => (
    (selectionTargets.get(key) || []).some((target) => target.element)
  )).length;
  for (const candidate of candidates) {
    const alreadyLabeled = (selectionTargets.get(candidate.key) || [])
      .some((target) => target.element);
    if (alreadyLabeled) continue;
    // The active object is the one label that must never lose a budget race.
    // A high-degree node can already have nine labelled neighbors before this
    // loop begins; allow one generated active label in that case, then stop.
    if (labeled >= 9 && candidate.key !== activeKey) break;
    const item = items.get(candidate.key);
    const position = selectionPosition(candidate.kind, candidate.id);
    const layer = kindLayers[candidate.kind];
    if (!item || !position || !layer) continue;
    const verticalOffset = {
      root: 0.66,
      volume: 0.55,
      face: 0.4,
      point: 0.25,
      context: 0.24,
    }[candidate.kind] || 0.25;
    const label = addLabel(
      searchTitle(candidate.kind, item),
      position.add(new THREE.Vector3(0, verticalOffset, 0)),
      layer,
      850 - labeled,
      candidate.kind,
      item,
      candidate.kind === "context",
    );
    label.userData.focusGenerated = true;
    focusLabels.push(label);
    labeled += 1;
  }
}

function addLine(start, end, color, opacity, dashed, layer = "lines", focusRefs = []) {
  const geometry = new THREE.BufferGeometry().setFromPoints([start, end]);
  const material = dashed
    ? new THREE.LineDashedMaterial({ color, transparent: true, opacity, dashSize: 0.12, gapSize: 0.09 })
    : new THREE.LineBasicMaterial({ color, transparent: true, opacity });
  const line = new THREE.Line(geometry, material);
  if (dashed) line.computeLineDistances();
  register(line, layer, focusRefs);
  return line;
}

function registerScreenLinePickable(lineObject, item, start, end) {
  registerSelectionTarget("line", item.id, lineObject);
  lineObject.userData.pickSegment = { start: start.clone(), end: end.clone() };
  lineObject.userData.baseOpacity = Number(lineObject.material.opacity || 0);
  screenLinePickables.push(lineObject);
  const key = selectionKey("line", item.id);
  items.set(key, item);
  searchItems.set(key, item);
  return lineObject;
}

function renderLineExplorer(lines) {
  lineNavigatorItems = lines;
  lineSelectEl.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.disabled = lines.length > 0;
  placeholder.selected = true;
  placeholder.textContent = lines.length ? "Choose a relationship…" : "No model lines available";
  lineSelectEl.appendChild(placeholder);
  lines.forEach((line, index) => {
    const option = document.createElement("option");
    option.value = String(index + 1);
    option.textContent = linePresentations.get(line.id)?.option || "Relationship";
    lineSelectEl.appendChild(option);
  });
  lineExplorerEl.hidden = lines.length === 0;
  syncLineExplorer();
}

function syncLineExplorer() {
  lineSelectEl.disabled = !layerVisible.lines || lineNavigatorItems.length === 0;
  const selectedIndex = selected?.kind === "line"
    ? lineNavigatorItems.findIndex((line) => line.id === selected.id)
    : -1;
  lineSelectEl.value = selectedIndex >= 0 ? String(selectedIndex + 1) : "";
}

function disposeGraph() {
  scene.remove(graph);
  graph.traverse((object) => {
    if (object.element) object.element.remove();
    if (object.geometry) object.geometry.dispose();
    if (Array.isArray(object.material)) object.material.forEach((material) => material.dispose());
    else if (object.material) object.material.dispose();
  });
  graph = new THREE.Group();
  scene.add(graph);
  labels = [];
  pickables = [];
  screenLinePickables = [];
  renderLineExplorer([]);
  selectionTargets = new Map();
  positions = new Map();
  items = new Map();
  searchItems = new Map();
  sceneNodeLabels = new Map();
  linePresentations = new Map();
  layerObjects = freshLayerObjects();
  pulseGlows = [];
  focusLabels = [];
  focusLabelsKey = "";
}

function addPoint(point, position, baseRadius, showLabel, promoted, currentAtCutoff) {
  const active = currentAtCutoff;
  const clusterScale = promoted ? 1 : 0.72;
  const radius = (active ? baseRadius : baseRadius * 0.68) * clusterScale;
  const geometry = new THREE.SphereGeometry(radius, 14, 10);
  const material = new THREE.MeshStandardMaterial({
    color: COLORS.points,
    emissive: COLORS.points,
    emissiveIntensity: active ? (promoted ? 0.44 : 0.2) : 0.06,
    transparent: true,
    opacity: active ? (promoted ? 0.9 : 0.58) : (promoted ? 0.3 : 0.16),
    roughness: 0.26,
  });
  const mesh = registerPickable(new THREE.Mesh(geometry, material), "points", "point", point);
  mesh.position.copy(position);
  if (active && promoted && hash(`${point.id}:glow`) < 0.05) {
    addGlow(
      position,
      COLORS.points,
      radius * 5.4,
      0.1,
      "points",
      0.08,
      [selectionKey("point", point.id)],
    );
  }
  if (showLabel) {
    addLabel(
      point.content,
      position.clone().add(new THREE.Vector3(0, radius + 0.16, 0)),
      "points",
      active ? 55 : 25,
      "point",
      point
    );
  }
}

function addContextNode(id) {
  const position = positions.get(id);
  if (!position) return;
  const item = { id, content: id === "self" ? "USER" : id, kind: "context" };
  const mesh = registerPickable(
    new THREE.Mesh(
      new THREE.BoxGeometry(0.14, 0.14, 0.14),
      new THREE.MeshStandardMaterial({ color: COLORS.context, roughness: 0.5 })
    ),
    "points",
    "context",
    item
  );
  mesh.position.copy(position);
  if (id === "self" || hash(id) < 0.16) {
    addLabel(
      item.content,
      position.clone().add(new THREE.Vector3(0, 0.24, 0)),
      "points",
      30,
      "context",
      item,
      true
    );
  }
}

function addClusterHalo(memberPositions, center, focusRefs = []) {
  if (memberPositions.length < 2) return;
  const radius = Math.min(1.9, Math.max(0.55, ...memberPositions.map((member) => member.distanceTo(center))) + 0.18);
  const mesh = register(
    new THREE.Mesh(
      new THREE.SphereGeometry(1, 16, 10),
      new THREE.MeshBasicMaterial({
        color: COLORS.faces,
        transparent: true,
        opacity: 0.065,
        wireframe: true,
        depthWrite: false,
      })
    ),
    "faces",
    focusRefs,
  );
  mesh.position.copy(center);
  mesh.scale.setScalar(radius);
  mesh.renderOrder = 1;
}

function addFace(face, showLabel) {
  const position = positions.get(face.id);
  if (!position) return;
  const faceKey = selectionKey("face", face.id);
  const memberIds = currentLayout?.facePointIds.get(face.id) || [];
  const memberPositions = memberIds.map((id) => positions.get(id)).filter(Boolean);
  addClusterHalo(memberPositions, position, [faceKey]);
  addGlow(position, COLORS.faces, 1.55, 0.16, "faces", 0.07, [faceKey]);
  const node = registerPickable(
    new THREE.Mesh(
      new THREE.OctahedronGeometry(0.28, 0),
      new THREE.MeshStandardMaterial({
        color: COLORS.faces,
        emissive: COLORS.faces,
        emissiveIntensity: 0.42,
        roughness: 0.26,
      })
    ),
    "faces",
    "face",
    face
  );
  node.position.copy(position);
  if (showLabel) {
    addLabel(
      face.signature,
      position.clone().add(new THREE.Vector3(0, 0.4, 0)),
      "faces",
      75,
      "face",
      face
    );
  }
  memberIds.forEach((id) => {
    const member = positions.get(id);
    if (member) {
      addLine(
        member,
        position,
        COLORS.hierarchy,
        0.1,
        true,
        "hierarchy",
        [faceKey, selectionKey("point", id)],
      );
    }
  });
}

function addVolume(volume, showLabel) {
  const position = positions.get(volume.id);
  if (!position) return;
  const volumeKey = selectionKey("volume", volume.id);
  addGlow(position, COLORS.volumes, 2.35, 0.18, "volumes", 0.06, [volumeKey]);
  const mesh = registerPickable(
    new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.48, 1),
      new THREE.MeshStandardMaterial({
        color: COLORS.volumes,
        emissive: COLORS.volumes,
        emissiveIntensity: 0.32,
        transparent: true,
        opacity: 0.72,
        wireframe: true,
      })
    ),
    "volumes",
    "volume",
    volume
  );
  mesh.position.copy(position);
  const core = register(
    new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.17, 1),
      new THREE.MeshBasicMaterial({ color: COLORS.volumes, transparent: true, opacity: 0.68 })
    ),
    "volumes",
    [volumeKey],
  );
  core.position.copy(position);
  if (showLabel) {
    addLabel(
      volume.signature,
      position.clone().add(new THREE.Vector3(0, 0.55, 0)),
      "volumes",
      90,
      "volume",
      volume
    );
  }
  const memberIds = currentLayout?.volumeFaceIds.get(volume.id) || [];
  memberIds.forEach((id) => {
    const member = positions.get(id);
    if (member) {
      addLine(
        member,
        position,
        COLORS.volumes,
        0.22,
        false,
        "hierarchy",
        [volumeKey, selectionKey("face", id)],
      );
    }
  });
}

function addRoot(root) {
  const position = positions.get(root.id);
  if (!position) return;
  const rootKey = selectionKey("root", root.id);
  addGlow(position, COLORS.root, 4.2, 0.38, "root", 0.075, [rootKey]);
  const mesh = registerPickable(
    new THREE.Mesh(
      new THREE.DodecahedronGeometry(0.62, 0),
      new THREE.MeshPhysicalMaterial({
        color: COLORS.root,
        emissive: COLORS.root,
        emissiveIntensity: 0.68,
        roughness: 0.18,
        metalness: 0.08,
        clearcoat: 1,
        clearcoatRoughness: 0.22,
        transparent: true,
        opacity: 1,
        depthTest: false,
        depthWrite: false,
      })
    ),
    "root",
    "root",
    root
  );
  mesh.position.copy(position);
  mesh.renderOrder = 2;
  addLabel(
    root.signature,
    position.clone().add(new THREE.Vector3(0, 0.66, 0)),
    "root",
    120,
    "root",
    root
  );
  const parentIds = currentLayout?.rootVolumeIds || [];
  parentIds.forEach((id) => {
    const member = positions.get(id);
    if (member) {
      addLine(
        member,
        position,
        COLORS.root,
        0.32,
        false,
        "hierarchy",
        [rootKey, selectionKey("volume", id)],
      );
    }
  });
}

function addModelLine(line) {
  if (!visibleAt(line)) return;
  const start = positionForGraphId(line.source);
  const end = positionForGraphId(line.target);
  if (!start || !end) return;
  const evolution = line.kind === "evolution";
  const sourceCluster = currentLayout?.pointClusterById.get(line.source);
  const targetCluster = currentLayout?.pointClusterById.get(line.target);
  const sameCluster = sourceCluster && sourceCluster === targetCluster;
  // Transparent strokes keep a large model calm, but each semantic relation
  // still needs to survive compositing against the deep-space canvas.
  const opacity = evolution ? (sameCluster ? 0.5 : 0.2) : 0.36;
  const lineObject = addLine(
    start,
    end,
    evolution ? COLORS.lines : COLORS.relation,
    opacity,
    !evolution,
  );
  registerScreenLinePickable(lineObject, line, start, end);
}

function positionForGraphId(id) {
  const pointId = currentLayout?.endpointPointIds?.get?.(id);
  const contextId = currentLayout?.endpointContextIds?.get?.(id);
  return positions.get(pointId || contextId || id);
}

function addOrbitRing(radius, color, opacity, rotation, layer) {
  const points = Array.from({ length: 129 }, (_, index) => {
    const angle = (index / 128) * Math.PI * 2;
    return new THREE.Vector3(Math.cos(angle) * radius, 0, Math.sin(angle) * radius);
  });
  const line = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(points),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false })
  );
  line.rotation.set(...rotation);
  line.renderOrder = -2;
  register(line, layer);
}

function addGround() {
  const ringLayer = model.root ? "root" : (model.volumes.length ? "volumes" : "points");
  const radius = Math.max(5, layoutRadius);
  [0.34, 0.58, 0.84, 1.08].forEach((factor, index) => {
    addOrbitRing(
      radius * factor,
      COLORS.hierarchy,
      Math.max(0.014, 0.038 - index * 0.007),
      [0.12 + index * 0.035, index * 0.18, 0.05 - index * 0.02],
      ringLayer
    );
  });
  addOrbitRing(radius * 0.72, COLORS.focus, 0.03, [Math.PI / 2.8, 0.42, 0.18], ringLayer);
}

function buildScene({
  frame = true,
  preserveSelection = false,
  selectionReplacement = null,
} = {}) {
  disposeGraph();
  modelPointById = new Map(model.points.map((point) => [point.id, point]));
  const visiblePoints = model.points
    .filter((point) => pointVisibleAt(point, cutoff, modelPointById))
    .sort((a, b) => a.id.localeCompare(b.id));
  const visibleFaces = model.faces.filter(visibleAt);
  const visibleVolumes = model.volumes.filter(visibleAt);
  const visibleRoot = model.root && visibleAt(model.root) ? model.root : null;
  const visibleLines = model.lines.filter(visibleAt);
  currentLayout = computeClusterLayout({
    points: visiblePoints,
    lines: visibleLines,
    faces: visibleFaces,
    volumes: visibleVolumes,
    root: visibleRoot,
  });
  const projectedLines = coalesceProjectedLines(visibleLines, currentLayout);
  const renderedSceneLines = projectedLines.lines;
  currentLayout.diagnostics.projectedRelationLinesCollapsed = Math.max(
    0,
    visibleLines.length - renderedSceneLines.length,
  );
  sceneModel = {
    points: visiblePoints,
    lines: renderedSceneLines,
    faces: visibleFaces,
    volumes: visibleVolumes,
    root: visibleRoot,
  };
  positions = new Map(
    [...currentLayout.positions].map(([id, position]) => [id, new THREE.Vector3(...position)])
  );
  layoutRadius = currentLayout.diagnostics.bounds.radius;
  scene.fog.density = Math.max(0.006, Math.min(0.018, 0.09 / Math.max(layoutRadius, 5)));
  addGround();

  const labelEveryPoint = visiblePoints.length <= 60;
  const labeledFaces = strongestIds(visibleFaces, 10);
  const labeledVolumes = strongestIds(visibleVolumes, 6);
  visiblePoints.forEach((point) => {
    const active = pointVisibleAt(point, cutoff, modelPointById);
    const promoted = currentLayout.pointClusterById.get(point.id)?.startsWith("face:") || false;
    const showLabel = labelEveryPoint || (promoted && (
      currentLayout.directPointIds.has(point.id) || (active && hash(point.id) < 0.035)
    ));
    addPoint(point, positions.get(point.id), currentLayout.pointRadius, showLabel, promoted, active);
  });
  // Historical and inactive Points remain searchable and inspectable even
  // though the constellation only renders the chain head valid at this time.
  model.points.filter((point) => pointKnownAt(point, cutoff)).forEach((point) => {
    const key = selectionKey("point", point.id);
    if (!searchItems.has(key)) searchItems.set(key, point);
  });
  currentLayout.contextIds.forEach(addContextNode);
  renderedSceneLines.forEach(addModelLine);
  const renderedLineItems = renderedSceneLines.filter(
    (line) => positionForGraphId(line.source) && positionForGraphId(line.target),
  );
  sceneNodeLabels = modelNodeLabelIndex(
    sceneModel,
    currentLayout.endpointPointIds,
    currentLayout.endpointContextIds,
  );
  const searchLines = model.lines.filter(
    (line) => lineKnownAt(line, model, cutoff, modelPointById),
  );
  const searchModel = {
    ...sceneModel,
    points: model.points.filter((point) => pointKnownAt(point, cutoff)),
    lines: searchLines,
  };
  const searchNodeLabels = modelNodeLabelIndex(
    searchModel,
    currentLayout.endpointPointIds,
    currentLayout.endpointContextIds,
  );
  linePresentations = indexLinePresentations(searchLines, searchModel, searchNodeLabels);
  searchLines.forEach((line) => searchItems.set(selectionKey("line", line.id), line));
  renderLineExplorer(renderedLineItems);
  visibleFaces.forEach((face) => addFace(face, labeledFaces.has(face.id)));
  visibleVolumes.forEach((volume) => addVolume(volume, labeledVolumes.has(volume.id)));
  if (visibleRoot) addRoot(visibleRoot);

  const renderedLines = renderedLineItems.length;
  const currentCounts = {
    points: visiblePoints.length,
    lines: renderedLines,
    faces: visibleFaces.length,
    volumes: visibleVolumes.length,
    root: visibleRoot ? 1 : 0,
  };
  updateLayerCounts(currentCounts);
  rebuildSearchEntries();
  applyLayerVisibility({ preserveSelection, selectionReplacement });
  renderStatus(currentCounts);
  emptyEl.hidden = visiblePoints.length > 0;
  if (frame) frameLayout(false);
  // Fresh label elements and a possibly-resized status strip: both cached
  // measurements have to be taken again.
  invalidatePanelBoxes();
  // A viewer that started against an empty store rendered nothing for its
  // first frames and would otherwise have spent its whole probe budget on a
  // blank scene, leaving the local smoke signal reading "nothing rendered"
  // forever. A new scene deserves a fresh look.
  renderProbeSweeps = 0;
  window.__persomeViewerState = {
    schemaVersion: model.schema_version,
    generatedAt: modelGeneratedAt || null,
    commit: model.build?.core_commit || null,
    stats: model.stats,
    rendered: {
      points: visiblePoints.length,
      lines: renderedLines,
      faces: visibleFaces.length,
      volumes: visibleVolumes.length,
      root: Boolean(visibleRoot),
      context: currentLayout.contextIds.length,
    },
    layers: { ...layerVisible },
    layout: currentLayout.diagnostics,
  };
  window.__persomeLayoutState = currentLayout.diagnostics;
  viewerEl.dataset.schemaVersion = String(model.schema_version || "");
  viewerEl.dataset.renderedPoints = String(visiblePoints.length);
  viewerEl.dataset.renderedLines = String(renderedLines);
  viewerEl.dataset.renderedFaces = String(visibleFaces.length);
  viewerEl.dataset.renderedVolumes = String(visibleVolumes.length);
  viewerEl.dataset.renderedRoot = String(Boolean(visibleRoot));
  viewerEl.dataset.coreCommit = model.build?.core_commit || "";
  viewerEl.dataset.layoutVersion = currentLayout.diagnostics.version;
}

function renderStatus(counts) {
  statusEl.replaceChildren();
  const parts = [
    ["points", "Points", counts.points],
    ["lines", "Lines", counts.lines],
    ["faces", "Faces", counts.faces],
    ["volumes", "Volumes", counts.volumes],
    ["root", "Root", counts.root],
  ];
  parts.forEach(([kind, label, value]) => {
    const stat = document.createElement("span");
    stat.className = "stat";
    stat.dataset.kind = kind;
    const count = document.createElement("b");
    count.textContent = String(value);
    const name = document.createElement("small");
    name.textContent = label;
    stat.append(count, name);
    statusEl.append(stat);
  });
  const buildStatus = model.build?.status;
  if (buildStatus) {
    const build = document.createElement("span");
    const buildStateClass = {
      not_built: "not-built",
      building: "building",
      degraded: "degraded",
      complete: "complete",
    }[buildStatus] || "not-built";
    build.className = `build-state build-state--${buildStateClass}`;
    build.dataset.status = buildStatus;
    const signal = document.createElement("i");
    signal.setAttribute("aria-hidden", "true");
    const label = buildStatus === "not_built"
      ? "Not built"
      : buildStatus === "building"
        ? "Building…"
        : `Build ${buildStatus}`;
    build.append(signal, label);
    statusEl.append(build);
  }
  modelIdentityEl.textContent = model.root?.signature
    || "A living map of what you notice, repeat, and become.";
}

function updateLayerCounts(counts) {
  Object.entries(layerCountEls).forEach(([kind, countEl]) => {
    if (!countEl) return;
    const count = Number(counts[kind] || 0);
    countEl.textContent = String(count);
    const button = countEl.closest("button");
    if (button) {
      const label = kind === "root" ? "Root" : `${kind[0].toUpperCase()}${kind.slice(1)}`;
      button.setAttribute("aria-label", `${label}: ${count}. Toggle visibility`);
    }
  });
}

function searchTitle(kind, item, lineDetail = null) {
  if (kind === "line") return lineDetail?.title || item.label || item.predicate || item.kind || item.id;
  return item.content || item.signature || item.label || item.predicate || item.kind || item.id;
}

function searchSubtitle(kind, item, lineDetail = null, pointDetail = null) {
  if (kind === "point") return pointDetail?.subtitle || "Modeled observation · not active";
  if (kind === "line") {
    return lineDetail ? `${lineDetail.source} → ${lineDetail.target}` : "Relationship";
  }
  if (kind === "face") return "Stable pattern";
  if (kind === "volume") return "Cross-pattern structure";
  if (kind === "root") return "Current personal model";
  return "Context referenced by a relation";
}

function searchWeight(kind, item, pointDetail = null) {
  const observations = Math.min(8, Number(item.observations || 0));
  if (kind === "root") return 24;
  if (kind === "volume") return 18 + observations;
  if (kind === "face") return 12 + observations;
  if (kind === "point") {
    return 6
      + Math.min(4, Number(item.confidence || 0) * 4)
      + Number(pointDetail?.weightAdjustment || 0);
  }
  if (kind === "line") return 3;
  return 1;
}

function rebuildSearchEntries() {
  const entries = [...searchItems.entries()].map(([key, item]) => {
    const separator = key.indexOf(":");
    const kind = separator > 0 ? key.slice(0, separator) : "context";
    const lineDetail = kind === "line" ? linePresentations.get(item.id) : null;
    const pointDetail = kind === "point"
      ? pointSearchMetadata(item, cutoff, modelPointById)
      : null;
    return {
      key,
      kind,
      id: item.id,
      title: searchTitle(kind, item, lineDetail),
      subtitle: searchSubtitle(kind, item, lineDetail, pointDetail),
      aliases: lineDetail
        ? [lineDetail.predicate, lineDetail.label, lineDetail.source, lineDetail.target, item.kind]
        : [...new Set([item.kind, item.status, ...(pointDetail?.aliases || [])].filter(Boolean))],
      stateAliases: pointDetail?.aliases || [],
      weight: searchWeight(kind, item, pointDetail),
      searchState: pointDetail?.state || "",
    };
  });
  searchEntries = prepareSearchEntries(entries);
  if (!searchPanelEl.hidden) renderSearchResults();
}

function setSearchActiveIndex(nextIndex) {
  if (!searchMatches.length) {
    searchActiveIndex = 0;
    searchInputEl.removeAttribute("aria-activedescendant");
    return;
  }
  searchActiveIndex = (nextIndex + searchMatches.length) % searchMatches.length;
  const buttons = [...searchResultsEl.querySelectorAll(".search-result")];
  buttons.forEach((button, index) => {
    button.setAttribute("aria-selected", String(index === searchActiveIndex));
  });
  const active = buttons[searchActiveIndex];
  if (active) {
    searchInputEl.setAttribute("aria-activedescendant", active.id);
    active.scrollIntoView({ block: "nearest" });
  }
}

function renderSearchResults() {
  const query = searchInputEl.value.trim();
  searchMatches = rankSearchEntries(searchEntries, query, 9);
  searchActiveIndex = Math.min(searchActiveIndex, Math.max(0, searchMatches.length - 1));
  searchResultsEl.replaceChildren();
  searchMatches.forEach((entry, index) => {
    const button = document.createElement("button");
    button.id = `model-search-result-${index}`;
    button.type = "button";
    button.tabIndex = -1;
    button.className = "search-result";
    button.dataset.kind = entry.kind;
    if (entry.searchState) button.dataset.searchState = entry.searchState;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(index === searchActiveIndex));

    const marker = document.createElement("i");
    marker.setAttribute("aria-hidden", "true");
    const copy = document.createElement("span");
    const title = document.createElement("b");
    title.textContent = entry.title;
    const subtitle = document.createElement("small");
    subtitle.textContent = entry.subtitle;
    copy.append(title, subtitle);
    const kind = document.createElement("em");
    kind.textContent = entry.kind === "context" ? "entity" : entry.kind;
    button.append(marker, copy, kind);
    button.addEventListener("pointerenter", () => setSearchActiveIndex(index));
    button.addEventListener("click", () => selectSearchMatch(entry));
    searchResultsEl.appendChild(button);
  });
  searchEmptyEl.hidden = searchMatches.length > 0;
  searchSummaryEl.textContent = query
    ? (searchMatches.length
      ? `Top ${searchMatches.length} matches in this model and its history.`
      : "No matching object in this model or its history.")
    : `Showing ${searchMatches.length} anchors from this model and its history.`;
  setSearchActiveIndex(searchActiveIndex);
}

function openSearch() {
  pauseAutoRotate();
  searchPanelEl.hidden = false;
  searchInputEl.setAttribute("aria-expanded", "true");
  searchActiveIndex = 0;
  renderSearchResults();
  invalidatePanelBoxes();
  window.requestAnimationFrame(() => {
    searchInputEl.focus({ preventScroll: true });
    searchInputEl.select();
  });
}

function closeSearch(restoreFocus = true) {
  if (searchPanelEl.hidden) return;
  searchPanelEl.hidden = true;
  searchInputEl.setAttribute("aria-expanded", "false");
  searchInputEl.removeAttribute("aria-activedescendant");
  invalidatePanelBoxes();
  if (restoreFocus) openSearchButton.focus({ preventScroll: true });
}

function selectSearchMatch(entry) {
  const item = searchItems.get(entry.key);
  if (!item) {
    rebuildSearchEntries();
    renderSearchResults();
    return;
  }
  const rendered = items.has(entry.key);
  const layer = rendered ? kindLayers[entry.kind] : null;
  if (layer && !layerVisible[layer]) {
    layerVisible[layer] = true;
    applyLayerVisibility();
    if (window.__persomeViewerState) window.__persomeViewerState.layers = { ...layerVisible };
  }
  closeSearch(false);
  showDetails(entry.kind, item, openSearchButton);
  if (rendered) flyToSelection(entry.kind, entry.id);
  document.getElementById("close-detail").focus({ preventScroll: true });
}

function pauseAutoRotate() {
  if (!controls.autoRotate) return;
  controls.autoRotate = false;
  rotateButton.setAttribute("aria-pressed", "false");
}

function showShareNotice(title, message, failed = false) {
  const heading = shareNoticeEl.querySelector("strong");
  const detail = shareNoticeEl.querySelector("small");
  heading.textContent = title;
  detail.textContent = message;
  shareNoticeEl.classList.toggle("failed", failed);
  shareNoticeEl.hidden = false;
  window.clearTimeout(showShareNotice.timer);
  showShareNotice.timer = window.setTimeout(() => {
    shareNoticeEl.hidden = true;
  }, 9000);
}

function setShareBusy(busy) {
  [cardButton, shareButton].forEach((button) => {
    button.disabled = busy || !shareReady;
    button.setAttribute("aria-busy", String(busy));
  });
  cardButton.querySelector("b").textContent = busy ? "Preparing…" : "Card";
  shareButton.querySelector("b").textContent = busy ? "Preparing…" : "Share";
}

async function loadShareProjection() {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), SHARE_CARD_TIMEOUT_MS);
  try {
    const response = await fetch("./share-card", {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`Share endpoint returned HTTP ${response.status}`);
    const payload = await response.json();
    const generatedAt = String(payload.generated_at || "");
    if (
      !generatedAt
      || !payload.model
      || !Array.isArray(payload.model.faces)
      || !Array.isArray(payload.model.volumes)
      || !payload.model.stats
      || typeof payload.model.stats !== "object"
    ) {
      throw new Error("Share endpoint returned an invalid projection");
    }
    return { generatedAt, model: payload.model };
  } finally {
    window.clearTimeout(timeout);
  }
}

async function loadConstellationBundle() {
  // `/share-card` exposes only scrubbed summaries, while `/graph` supplies the
  // private geometry already available to this owner-local viewer. Match their
  // server generation before drawing so text/counts can never describe a newer
  // model than the constellation. A cache expiry can land between the two GETs,
  // so carry the newer graph into bounded retries rather than guessing.
  let graphPayload = modelGeneratedAt ? { generated_at: modelGeneratedAt, model } : null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const share = await loadShareProjection();
    if (graphPayload?.generated_at === share.generatedAt) {
      return { shareModel: share.model, graphModel: graphPayload.model };
    }
    graphPayload = await fetchModelGraph();
    if (graphPayload.generated_at === share.generatedAt) {
      return { shareModel: share.model, graphModel: graphPayload.model };
    }
  }
  throw new Error("The model changed while preparing the share image. Try again.");
}

function createHumanCardBlob(shareModel) {
  const canvas = document.createElement("canvas");
  canvas.width = HUMAN_CARD_WIDTH;
  canvas.height = HUMAN_CARD_HEIGHT;
  const context = canvas.getContext("2d");
  if (!context) return Promise.reject(new Error("Canvas export is unavailable"));
  drawHumanCard(context, shareModel);
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("The HUMAN.md Card could not be encoded"));
    }, "image/png");
  });
}

function createConstellationBlob(shareModel, shareGraphModel = model) {
  const canvas = document.createElement("canvas");
  canvas.width = CONSTELLATION_CARD_WIDTH;
  canvas.height = CONSTELLATION_CARD_HEIGHT;
  const context = canvas.getContext("2d");
  if (!context) return Promise.reject(new Error("Canvas export is unavailable"));

  // Sharing is a projection, not a screenshot of the owner's current camera.
  // Preserve every interactive state that the temporary 16:9 overview touches
  // so a focused/flying/zooming viewer resumes exactly where it was.
  const rendererSize = renderer.getSize(new THREE.Vector2());
  const returnFocusKey = [...selectionTargets.entries()].find(([, targets]) => (
    targets.some((target) => target.element === selectionReturnFocus)
  ))?.[0] || null;
  const viewState = {
    rendererSize,
    pixelRatio: renderer.getPixelRatio(),
    cameraAspect: camera.aspect,
    cameraPosition: camera.position.clone(),
    cameraQuaternion: camera.quaternion.clone(),
    controlsTarget: controls.target.clone(),
    cameraFlight,
    zoomGoalDistance,
    zoomAnchor,
    selected: selected ? { ...selected } : null,
    selectedItem,
    selectionReturnFocus,
    returnFocusKey,
    focusVisualsSuspended,
    layers: { ...layerVisible },
    sliderValue: slider.value,
    sliderLabel: sliderLabel.textContent,
    cutoff: new Date(cutoff),
    fitDistance,
    framedRadius,
    portraitMode,
    minDistance: controls.minDistance,
    maxDistance: controls.maxDistance,
    maxTargetRadius: controls.maxTargetRadius,
    autoRotate: controls.autoRotate,
    model,
    minTime: new Date(minTime),
    maxTime: new Date(maxTime),
  };

  try {
    pauseAutoRotate();
    cameraFlight = null;
    zoomGoalDistance = null;
    zoomAnchor = null;
    focusVisualsSuspended = true;

    const exportingDifferentModel = shareGraphModel !== model;
    if (exportingDifferentModel) {
      model = shareGraphModel;
      slider.value = "100";
      updateTimelineBounds();
      buildScene({ frame: false, preserveSelection: true });
    }

    // The share projection is current, so its picture must be current too.
    // Rebuild at Now before fitting the export; otherwise time travel would pair
    // a historical constellation with current narrative and aggregate counts.
    if (!exportingDifferentModel && slider.value !== "100") {
      slider.value = "100";
      updateCutoff();
      buildScene({ frame: false, preserveSelection: true });
    }

    // Layer toggles are inspection state. The public artifact always shows the
    // complete current time slice so its picture agrees with its full counts.
    Object.keys(layerVisible).forEach((layer) => { layerVisible[layer] = true; });
    applyLayerVisibility({ preserveSelection: true });

    const overview = fittedOverviewPose(
      layoutRadius,
      CONSTELLATION_CARD_WIDTH,
      CONSTELLATION_CARD_HEIGHT,
    );

    // Render at the artifact's own aspect ratio. Drawing a portrait viewport
    // into the landscape card with drawCover() crops most of the constellation,
    // even if that viewport's camera was otherwise fitted.
    renderer.setPixelRatio(1);
    renderer.setSize(CONSTELLATION_CARD_WIDTH, CONSTELLATION_CARD_HEIGHT, false);
    camera.aspect = overview.aspect;
    camera.position.set(...overview.position);
    controls.target.set(...overview.target);
    camera.lookAt(controls.target);
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld(true);
    renderer.render(scene, camera);
    const renderedLines = sceneModel.lines.filter(
      (line) => positionForGraphId(line.source) && positionForGraphId(line.target),
    );
    const renderedShareModel = {
      ...shareModel,
      stats: {
        ...(shareModel.stats || {}),
        points: sceneModel.points.length,
        evolution_lines: renderedLines.filter((line) => line.kind === "evolution").length,
        relation_lines: renderedLines.filter((line) => line.kind === "relation").length,
        faces: sceneModel.faces.length,
        volumes: sceneModel.volumes.length,
        roots: Number(Boolean(sceneModel.root)),
      },
    };
    drawConstellationCard(context, renderer.domElement, renderedShareModel);
  } finally {
    renderer.setPixelRatio(viewState.pixelRatio);
    renderer.setSize(viewState.rendererSize.x, viewState.rendererSize.y, false);

    const exportedDifferentModel = model !== viewState.model;
    model = viewState.model;
    minTime = viewState.minTime;
    maxTime = viewState.maxTime;
    slider.value = viewState.sliderValue;
    sliderLabel.textContent = viewState.sliderLabel;
    cutoff = viewState.cutoff;
    Object.entries(viewState.layers).forEach(([layer, visible]) => {
      layerVisible[layer] = visible;
    });
    // A historical export temporarily built the latest scene. Rebuild the
    // owner's exact slice before restoring the camera and focus over it.
    if (exportedDifferentModel || viewState.sliderValue !== "100") {
      buildScene({ frame: false, preserveSelection: true });
    }

    camera.aspect = viewState.cameraAspect;
    camera.position.copy(viewState.cameraPosition);
    camera.quaternion.copy(viewState.cameraQuaternion);
    controls.target.copy(viewState.controlsTarget);
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld(true);
    fitDistance = viewState.fitDistance;
    framedRadius = viewState.framedRadius;
    portraitMode = viewState.portraitMode;
    controls.minDistance = viewState.minDistance;
    controls.maxDistance = viewState.maxDistance;
    controls.maxTargetRadius = viewState.maxTargetRadius;
    cameraFlight = viewState.cameraFlight;
    zoomGoalDistance = viewState.zoomGoalDistance;
    zoomAnchor = viewState.zoomAnchor;
    controls.autoRotate = viewState.autoRotate;
    rotateButton.setAttribute("aria-pressed", String(viewState.autoRotate));
    selected = viewState.selected;
    selectedItem = selected
      ? items.get(selectionKey(selected.kind, selected.id)) || viewState.selectedItem
      : null;
    focusVisualsSuspended = viewState.focusVisualsSuspended;
    applyLayerVisibility();
    // Restoring focus can generate labels that did not exist while focus was
    // suspended, so resolve the return element only after selection sync.
    selectionReturnFocus = selected && viewState.returnFocusKey
      ? (selectionTargets.get(viewState.returnFocusKey) || [])
        .find((target) => target.element)?.element || viewState.selectionReturnFocus
      : viewState.selectionReturnFocus;
    syncZoomUI();
    renderer.render(scene, camera);
  }
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("The constellation image could not be encoded"));
    }, "image/png");
  });
}

function downloadShareImage(blob, fileName) {
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 1000);
}

function paintConstellationHandoff(popup) {
  if (!popup) return;
  popup.document.title = "Preparing your Persome constellation";
  popup.document.body.innerHTML = `
    <main style="min-height:100vh;display:grid;place-items:center;margin:0;background:${MODEL_PALETTE.canvas};color:${MODEL_PALETTE.text};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
      <section style="width:min(440px,calc(100vw - 48px));padding:38px;border:1px solid rgba(218,218,218,.14);border-radius:24px;background:${MODEL_PALETTE.surface};box-shadow:0 30px 100px rgba(0,0,0,.45)">
        <p style="margin:0 0 18px;color:${MODEL_PALETTE.focus};font-size:11px;font-weight:750;letter-spacing:.16em">PERSOME · SHARE TO X</p>
        <h1 style="margin:0;font-size:34px;line-height:1.05;letter-spacing:-.045em">Your constellation is downloading.</h1>
        <p style="margin:18px 0 0;color:${MODEL_PALETTE.muted};font-size:15px;line-height:1.65">In X, add <strong style="color:${MODEL_PALETTE.text}">${CONSTELLATION_FILE_NAME}</strong> with the image button. Review the image before posting.</p>
      </section>
    </main>`;
}

async function exportHumanCard() {
  setShareBusy(true);
  try {
    const share = await loadShareProjection();
    const blob = await createHumanCardBlob(share.model);
    downloadShareImage(blob, HUMAN_CARD_FILE_NAME);
    showShareNotice(
      "HUMAN.md Card downloaded",
      "Detected secrets, PII, paths, IDs, and evidence receipts were excluded. Review summaries before sharing.",
    );
    setShareBusy(false);
  } catch (error) {
    showShareNotice("Share image failed", error.message || String(error), true);
    setShareBusy(false);
  }
}

async function shareConstellationToX() {
  const popup = window.open("about:blank", "_blank");
  paintConstellationHandoff(popup);
  setShareBusy(true);
  try {
    const { shareModel, graphModel } = await loadConstellationBundle();
    const blob = await createConstellationBlob(shareModel, graphModel);
    downloadShareImage(blob, CONSTELLATION_FILE_NAME);
    showShareNotice(
      "Constellation downloaded",
      `Add ${CONSTELLATION_FILE_NAME} with the image button in X.`,
    );
    const intentUrl = buildXIntentUrl();
    if (popup && !popup.closed) {
      popup.opener = null;
      popup.location.replace(intentUrl);
      popup.focus();
    } else {
      window.location.assign(intentUrl);
    }
    setShareBusy(false);
  } catch (error) {
    if (popup && !popup.closed) popup.close();
    showShareNotice("Share image failed", error.message || String(error), true);
    setShareBusy(false);
  }
}

function objectFocusKeys(object) {
  const keys = new Set(object.userData.focusRefs || []);
  const ref = object.userData.ref;
  if (ref?.kind && ref?.id) keys.add(selectionKey(ref.kind, ref.id));
  return keys;
}

function applyMaterialFocus(object, focusing, relevant) {
  const materials = Array.isArray(object.material) ? object.material : [object.material];
  materials.filter(Boolean).forEach((material) => {
    material.userData.persomeFocusBase ||= {
      opacity: Number(material.opacity),
      transparent: Boolean(material.transparent),
      depthWrite: Boolean(material.depthWrite),
      emissiveIntensity: Number.isFinite(material.emissiveIntensity)
        ? Number(material.emissiveIntensity)
        : null,
    };
    const base = material.userData.persomeFocusBase;
    const muted = focusing && !relevant;
    const nextTransparent = muted ? true : base.transparent;
    if (material.transparent !== nextTransparent) material.needsUpdate = true;
    material.transparent = nextTransparent;
    const mutedFactor = object.isSprite ? 0.025 : (object.isLine ? 0.025 : 0.045);
    material.opacity = muted ? base.opacity * mutedFactor : base.opacity;
    material.depthWrite = muted ? false : base.depthWrite;
    if (base.emissiveIntensity !== null) {
      material.emissiveIntensity = muted ? base.emissiveIntensity * 0.045 : base.emissiveIntensity;
    }
  });
}

function syncSelectionState() {
  const selectedKey = selected ? selectionKey(selected.kind, selected.id) : null;
  const selectionVisible = selectedKey && items.has(selectedKey) && !focusVisualsSuspended;
  const activeKey = selectionVisible ? selectionKey(selected.kind, selected.id) : null;
  const focusKeys = selectionVisible
    ? focusKeysForSelection(sceneModel, currentLayout, selected)
    : new Set();
  const focusing = focusKeys.size > 0;
  if (!focusVisualsSuspended) updateFocusLabels(focusKeys, activeKey);

  Object.values(layerObjects).flat().forEach((object) => {
    const keys = objectFocusKeys(object);
    const relevant = [...keys].some((key) => focusKeys.has(key));
    if (object.element) {
      const active = keys.has(activeKey);
      object.element.classList.toggle("focus-muted", focusing && !relevant);
      object.element.classList.toggle("focus-neighbor", focusing && relevant && !active);
    } else if (object.material) {
      applyMaterialFocus(object, focusing, relevant);
    }
  });

  selectionTargets.forEach((targets, key) => {
    const active = key === activeKey;
    targets.forEach((target) => {
      if (target.element) {
        target.element.setAttribute("aria-expanded", String(active));
      } else if (target.isLine && target.material) {
        const baseOpacity = Number(target.userData.baseOpacity || 0);
        target.userData.baseColor ??= target.material.color.getHex();
        if (active) target.material.opacity = Math.min(1, baseOpacity * 2 + 0.24);
        target.material.color.setHex(active ? COLORS.focus : target.userData.baseColor);
        target.renderOrder = active ? 5 : 0;
      } else if (target.isMesh) {
        target.userData.selectionBaseScale ||= target.scale.clone();
        target.scale.copy(target.userData.selectionBaseScale).multiplyScalar(active ? 1.45 : 1);
      }
    });
  });
  syncLineExplorer();
  window.__persomeInteractionState = {
    linePickables: pickables.filter((object) => object.isLine).length,
    screenLinePickables: screenLinePickables.length,
    minimumNodeHitRadiusPx: MIN_NODE_HIT_RADIUS_PX,
    minimumLineHitRadiusPx: MIN_LINE_HIT_RADIUS_PX,
    touchNodeHitRadiusPx: TOUCH_NODE_HIT_RADIUS_PX,
    touchLineHitRadiusPx: TOUCH_LINE_HIT_RADIUS_PX,
    nodePickables: pickables.length,
    interactiveLabels: labels.filter((label) => Boolean(label.userData.ref)).length,
    selected: selected ? { ...selected } : null,
    focused: Boolean(selectionVisible),
    neighborhoodObjects: focusKeys.size,
  };
}

function clearSelection(restoreFocus = false) {
  const returnFocus = restoreFocus ? selectionReturnFocus : null;
  evidenceRequest += 1;
  selected = null;
  selectedItem = null;
  selectionReturnFocus = null;
  detailMode = "node";
  evidenceTrail = [];
  detailEl.hidden = true;
  delete detailEl.dataset.kind;
  invalidatePanelBoxes();
  syncSelectionState();
  if (returnFocus?.isConnected) {
    window.requestAnimationFrame(() => returnFocus.focus({ preventScroll: true }));
  }
}

function showAllModel() {
  clearSelection(true);
  resetCamera();
}

function applyLayerVisibility({ preserveSelection = false, selectionReplacement = null } = {}) {
  Object.entries(layerObjects).forEach(([layer, objects]) => {
    objects.forEach((object) => {
      const visible = layer === "hierarchy"
        ? [...objectFocusKeys(object)].every((key) => {
          const separator = key.indexOf(":");
          const kind = separator > 0 ? key.slice(0, separator) : "context";
          const endpointLayer = kindLayers[kind];
          return !endpointLayer || layerVisible[endpointLayer];
        })
        : layerVisible[layer];
      object.visible = visible;
      if (object.element) object.element.hidden = !visible;
    });
  });
  document.querySelectorAll("[data-layer]").forEach((button) => {
    button.setAttribute("aria-pressed", String(layerVisible[button.dataset.layer]));
  });
  if (!preserveSelection) {
    const reconciliation = reconcileSceneSelection(
      selected,
      items,
      layerVisible,
      kindLayers,
      selectionReplacement,
    );
    if (reconciliation.invalidated) {
      // The selected object may have been the camera's orbit target after search
      // focus. Once a cutoff or layer removes it, the drawer's Show all action is
      // gone too, so restore the fitted overview here rather than leaving an
      // apparently empty viewport aimed at a node that no longer exists.
      recoverInvalidSceneSelection(reconciliation, clearSelection, resetCamera);
      return;
    }
    if (reconciliation.replaced) {
      selected = reconciliation.selection;
      selectedItem = items.get(selectionKey(selected.kind, selected.id)) || null;
    }
  }
  syncSelectionState();
}

function humaneMeta(label, value) {
  // Only genuine timestamps. A Line's "From"/"To" carry entity labels, and
  // `new Date("March")` happily returns a date — replacing someone's name with
  // a fabricated one.
  if (label !== "Valid from") return value;
  const parsed = new Date(String(value));
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function appendMeta(label, value) {
  if (value === null || value === undefined || value === "") return;
  value = humaneMeta(label, value);
  const row = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = `${label}: `;
  row.append(name, String(value));
  detailMetaEl.appendChild(row);
}


function technicalDetails(link) {
  const details = document.createElement("details");
  details.className = "evidence-technical";
  const summary = document.createElement("summary");
  summary.textContent = "Technical details";
  const values = [
    link.id ? `ID: ${link.id}` : "",
    link.reference ? `Receipt: ${link.reference}` : "",
  ].filter(Boolean);
  const body = document.createElement("div");
  body.textContent = values.join("\n");
  details.append(summary, body);
  return details;
}

function evidenceCard(link, { drill = true } = {}) {
  const card = document.createElement("article");
  card.className = "evidence-card";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "evidence-link";
  const relationEl = document.createElement("span");
  relationEl.textContent = relationLabel(link.relation);
  const labelEl = document.createElement("b");
  labelEl.textContent = link.label || "Recorded evidence";
  button.append(relationEl, labelEl);
  if (link.timestamp || link.status) {
    const meta = document.createElement("small");
    meta.textContent = [link.status, link.timestamp].filter(Boolean).join(" · ");
    button.appendChild(meta);
  }
  if (drill && (link.reference || link.id)) {
    button.addEventListener("click", () => {
      detailEvidenceFoldEl.open = true;
      loadEvidence(link.reference || link.id);
    });
  } else {
    button.disabled = true;
  }
  card.append(button, technicalDetails(link));
  return card;
}

function appendEvidenceGroup(title, links, note = "") {
  if (!links?.length) return;
  const heading = document.createElement("strong");
  heading.textContent = title;
  detailReceiptsEl.appendChild(heading);
  if (note) {
    const copy = document.createElement("p");
    copy.className = "evidence-note";
    copy.textContent = note;
    detailReceiptsEl.appendChild(copy);
  }
  links.forEach((link) => {
    detailReceiptsEl.appendChild(evidenceCard(link));
  });
}

function renderBreadcrumbs() {
  evidenceBreadcrumbsEl.replaceChildren();
  evidenceTrail.forEach((crumb, index) => {
    if (index) {
      const separator = document.createElement("span");
      separator.textContent = "/";
      separator.setAttribute("aria-hidden", "true");
      evidenceBreadcrumbsEl.appendChild(separator);
    }
    if (index === evidenceTrail.length - 1) {
      const current = document.createElement("strong");
      current.textContent = crumb.label;
      current.setAttribute("aria-current", "page");
      evidenceBreadcrumbsEl.appendChild(current);
      return;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = crumb.label;
    button.addEventListener("click", () => {
      evidenceTrail = evidenceTrail.slice(0, index + 1);
      if (crumb.data) renderEvidence(crumb.data);
      else renderNodeEvidence(selected.kind, selectedItem);
    });
    evidenceBreadcrumbsEl.appendChild(button);
  });
}

function renderEvidence(data) {
  detailReceiptsEl.replaceChildren();
  renderBreadcrumbs();

  const heading = document.createElement("strong");
  heading.textContent = evidenceBreadcrumb(data);
  detailReceiptsEl.appendChild(heading);
  if (data.summary) {
    const summary = document.createElement("p");
    summary.className = "evidence-summary";
    summary.textContent = data.summary;
    detailReceiptsEl.appendChild(summary);
  }
  const facts = [
    data.kind ? `Kind: ${data.kind}` : "",
    data.status ? `Status: ${data.status}` : "",
    data.timestamp ? `Time: ${data.timestamp}` : "",
  ].filter(Boolean);
  facts.forEach((fact) => {
    const row = document.createElement("div");
    row.className = "evidence-fact";
    row.textContent = fact;
    detailReceiptsEl.appendChild(row);
  });
  appendEvidenceGroup("Direct sources", data.sources);
  appendEvidenceGroup("Version history", data.history);
  appendEvidenceGroup(
    "Nearby context",
    data.context,
    "These captures are close in time. They are investigation clues, not claimed direct proof.",
  );
  if (data.status === "missing") {
    const missing = document.createElement("p");
    missing.className = "evidence-note evidence-missing";
    missing.textContent = "The receipt is retained, but its local payload was not found or has expired.";
    detailReceiptsEl.appendChild(missing);
  }
  const technical = technicalDetails({
    id: data.id,
    reference: data.canonical_reference || data.reference,
  });
  if (data.path) {
    const body = technical.querySelector("div");
    body.textContent += `${body.textContent ? "\n" : ""}Path: ${data.path}`;
  }
  detailReceiptsEl.appendChild(technical);
}

async function loadEvidence(reference) {
  if (!selected || !reference) return;
  detailMode = "evidence";
  const request = ++evidenceRequest;
  detailReceiptsEl.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "evidence-loading";
  loading.textContent = "Resolving evidence…";
  detailReceiptsEl.appendChild(loading);
  try {
    // Now keeps the original endpoint shape. Time travel adds an explicit
    // server boundary so a nested drill-down cannot reveal a successor or
    // nearby capture that had not happened at the selected cutoff.
    const evidenceCutoff = Number(slider.value) < 100 ? cutoff : null;
    const response = await fetch(evidenceRequestPath(reference, evidenceCutoff), {
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!selected || request !== evidenceRequest || detailMode !== "evidence") return;
    evidenceTrail.push({ label: evidenceBreadcrumb(data), data });
    renderEvidence(data);
  } catch (error) {
    if (!selected || request !== evidenceRequest || detailMode !== "evidence") return;
    detailReceiptsEl.replaceChildren();
    renderBreadcrumbs();
    const message = document.createElement("p");
    message.className = "evidence-note evidence-missing";
    message.textContent = `Unable to resolve evidence. ${error.message || error}`;
    detailReceiptsEl.appendChild(message);
  }
}

function renderNodeEvidence(kind, item) {
  detailMode = "node";
  const request = ++evidenceRequest;
  detailReceiptsEl.replaceChildren();
  renderBreadcrumbs();

  const cards = nodeEvidenceCards(item, model, cutoff);
  const shown = Math.min(cards.length, EVIDENCE_CARD_LIMIT);
  const countEl = document.getElementById("detail-evidence-count");
  // Say what is actually on screen. Claiming 34 and rendering 12 is the kind of
  // small dishonesty this viewer exists not to commit.
  if (countEl) {
    countEl.textContent = cards.length > shown ? `${shown} of ${cards.length}` : (cards.length || "");
  }
  if (cards.length) {
    const heading = document.createElement("strong");
    heading.textContent = "Direct evidence";
    detailReceiptsEl.appendChild(heading);
    cards.slice(0, EVIDENCE_CARD_LIMIT).forEach((card) => {
      detailReceiptsEl.appendChild(evidenceCard(card));
    });
  } else {
    const empty = document.createElement("p");
    empty.className = "evidence-note";
    empty.textContent = "No direct evidence receipts are attached to this object.";
    detailReceiptsEl.appendChild(empty);
  }

  if (kind === "point") {
    fetch(`./node?id=${encodeURIComponent(item.id)}`)
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!data || !selected || request !== evidenceRequest || detailMode !== "node"
          || selected.kind !== kind || selected.id !== item.id) return;
        (data.raw || []).slice(0, 3).forEach((raw) => {
          const row = document.createElement("div");
          row.className = "evidence-preview";
          row.textContent = `${raw.ts ? `${String(raw.ts).slice(0, 16)}  ` : ""}${raw.text || ""}`;
          detailReceiptsEl.appendChild(row);
        });
      })
      .catch(() => {});
  }
}

function renderOverview(kind, item) {
  detailSummaryEl.replaceChildren();
  const overview = evidenceOverview(kind, item, model, cutoff);
  const copy = document.createElement("p");
  copy.textContent = overview.copy;
  detailSummaryEl.append(copy);
  // `overview.highlights` is the first three of exactly the cards the Evidence
  // fold renders, so showing them here too buried the rest of the drawer under
  // a duplicate. `renderNodeEvidence` owns the fold's count.
}

function renderNodeHistory(item) {
  detailHistoryEl.replaceChildren();
  const history = nodeHistoryCards(item, model, cutoff);
  const count = document.getElementById("detail-history-count");
  if (count) count.textContent = history.length ? String(history.length) : "";
  if (!history.length) {
    const empty = document.createElement("p");
    empty.className = "evidence-note";
    empty.textContent = "No previous or next version is recorded for this object.";
    detailHistoryEl.appendChild(empty);
    return;
  }
  const heading = document.createElement("strong");
  heading.textContent = "Version trail";
  detailHistoryEl.appendChild(heading);
  history.forEach((link) => detailHistoryEl.appendChild(evidenceCard(link)));
}

function appendLineTechnicalDetails(item) {
  const details = technicalDetails({ id: item.id });
  const body = details.querySelector("div");
  body.textContent = [
    item.id ? `Line ID: ${item.id}` : "",
    item.source ? `Source ID: ${item.source}` : "",
    item.target ? `Target ID: ${item.target}` : "",
  ].filter(Boolean).join("\n");
  detailSummaryEl.appendChild(details);
}

// Kinds that map to a durable row the owner can correct. Evolution lines are
// synthesized from supersede chains and relation lines are derived edges —
// neither is an object with an editable proposition. Context nodes are
// fabricated in this file from relation endpoints and have no server row at all.
const EDITABLE_KINDS = new Set(["point", "face", "volume", "root"]);

function isEditable(kind, item) {
  // The server decides. It knows which layer backs a Point, whether a newer
  // version exists, and whether a pattern has been promoted; the client would
  // be guessing. Offering an edit the writer refuses is worse than not
  // offering one — on a real model that was 46% of the Points on screen.
  return Boolean(item) && EDITABLE_KINDS.has(kind) && Boolean(item.id) && !item.edit_refusal;
}

function editableText(kind, item) {
  return kind === "point" ? item.content || "" : item.signature || "";
}

// The server answers with a stable slug. Echoing it at the owner explains
// nothing: they did not choose the reason, and most of these are about the
// state of their store rather than about what they typed.
const EDIT_REFUSALS = {
  point_has_no_file:
    "This fact is not filed under any memory, so there is nothing to correct"
    + " it in.",
  point_file_missing:
    "The memory file behind this fact is missing from disk, so it cannot be"
    + " corrected. Run `persome doctor` — the index and your Markdown have"
    + " drifted apart.",
  point_not_editable_in_this_file:
    "This fact lives in an append-only log, which corrections cannot rewrite.",
  point_superseded:
    "A newer version of this fact exists. Correct that one instead.",
  point_already_retired: "This fact has already been withdrawn.",
  point_archived: "This fact has already been withdrawn.",
  unknown_point: "This fact is no longer in your model. Reload the view.",
  unknown_object: "This object is no longer in your model. Reload the view.",
  object_archived: "This has already been removed from your model.",
  object_not_active:
    "This pattern has not been promoted yet, so its wording cannot be pinned."
    + " You can still reject it.",
  kind_level_mismatch: "That object is a different layer of the model.",
  replacement_forges_an_entry:
    "That text contains a memory entry heading, which would corrupt the file it"
    + " is written into. Remove the line starting with \u0060## [\u0060.",
  replacement_too_long: "That correction is too long.",
  empty_replacement: "Write the correction first.",
};

function editRefusalMessage(detail, status) {
  const known = EDIT_REFUSALS[detail];
  if (known) return known;
  if (typeof detail === "string" && detail.startsWith("edit_failed")) {
    return "The Runtime could not apply this correction. Check `persome status`.";
  }
  return `Could not save: ${detail || `HTTP ${status}`}`;
}

function setEditStatus(message, tone) {
  // The drawer may already be closed: committing by clicking away — the gesture
  // the editor itself advertises — both saves and closes. Writing a failure
  // into a `display:none` panel reports it to nobody, and the next selection
  // clears it before it could ever be read. Anything the owner must act on goes
  // to the viewport-level alert instead.
  if (detailEl.hidden && (tone === "error" || tone === "warn")) {
    showEditAlert(message);
    return;
  }
  detailStatusEl.textContent = message || "";
  if (tone) detailStatusEl.dataset.tone = tone;
  else delete detailStatusEl.dataset.tone;
}

function showEditAlert(message) {
  if (!message) return;
  const text = document.createElement("p");
  text.textContent = message;
  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.textContent = "Dismiss";
  dismiss.addEventListener("click", () => { editAlertEl.hidden = true; });
  editAlertEl.replaceChildren(text, dismiss);
  editAlertEl.hidden = false;
}

// ── Editing in place ──────────────────────────────────────────────────────
// The claim is the content, so correcting it happens on the claim itself:
// click the text, it becomes editable, blur or Cmd+Enter commits, Escape
// reverts. No mode to enter, no form to find, no second copy of the text to
// keep in sync with the first.

let editingItem = null;
let editInFlight = false;

async function submitEdit(kind, item, op, replacement, reason) {
  if (editInFlight) return;
  editInFlight = true;
  detailRejectEl.disabled = true;
  setEditStatus(op === "retire" ? "Removing…" : "Saving…", null);
  try {
    const response = await fetch("./edit", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        schema_version: 1,
        kind,
        id: item.id,
        op,
        replacement: op === "retire" ? "" : replacement,
        reason,
      }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      setEditStatus(editRefusalMessage(payload?.detail, response.status), "error");
      return;
    }
    const data = payload?.data || {};
    const nextId = data.new_id || item.id;
    // A correction is a statement about the present, and the successor Point is
    // stamped now. If the owner is time-travelling, that Point sits past the
    // cutoff and would never appear — the save would look like it failed.
    if (Number(slider.value) < 100) {
      slider.value = "100";
      updateCutoff();
    }
    const refreshed = await loadModel(true, {
      kind,
      fromId: item.id,
      toId: nextId,
    });
    if (refreshed === false) {
      // The awaits above can span seconds. Do not write a warning into a detail
      // drawer the owner opened for something else while this request ran.
      if (selected?.kind === kind && selected.id === item.id) {
        setEditStatus("Saved, but the view could not refresh. Reload to see it.", "warn");
      }
      return;
    }
    // A Point rewrite moves selection to its successor during scene rebuild.
    // If the owner selected something else meanwhile, that selection is theirs.
    if (!selected || selected.kind !== kind || selected.id !== nextId) return;
    if (op === "retire") {
      clearSelection();
      return;
    }
    const nextItem = items.get(selectionKey(kind, nextId));
    if (!nextItem) {
      setEditStatus("Saved. Reopen the node to see the update.", "ok");
      return;
    }
    showDetails(kind, nextItem);
    if (data.shadow_misses) {
      // The markdown layer changed but the Point may not have moved. Saying
      // "saved" here would be a lie the owner cannot see through.
      setEditStatus(
        "Saved to memory, but the model layer did not pick it up."
        + " Run `persome doctor` — the model may need a rebuild.",
        "warn",
      );
    } else {
      setEditStatus("Saved.", "ok");
    }
  } catch (error) {
    setEditStatus(`Could not save: ${error?.message || error}`, "error");
  } finally {
    editInFlight = false;
    detailRejectEl.disabled = false;
  }
}


function autoGrow() {
  detailClaimInputEl.style.height = "auto";
  detailClaimInputEl.style.height = `${detailClaimInputEl.scrollHeight}px`;
}

function beginEditing(kind, item) {
  if (!isEditable(kind, item) || editingItem) return;
  editingItem = { kind, id: item.id, original: editableText(kind, item) };
  detailClaimInputEl.value = editingItem.original;
  detailClaimInputEl.hidden = false;
  detailTitleEl.hidden = true;
  detailHintEl.innerHTML = "<kbd>\u2318</kbd><kbd>\u21a9</kbd> or click away to save · <kbd>Esc</kbd> to discard";
  detailHintEl.hidden = false;
  autoGrow();
  detailClaimInputEl.focus();
  detailClaimInputEl.setSelectionRange(
    detailClaimInputEl.value.length,
    detailClaimInputEl.value.length,
  );
}

function endEditing() {
  editingItem = null;
  detailClaimInputEl.hidden = true;
  detailTitleEl.hidden = false;
  const editable = detailTitleEl.dataset.editable === "true";
  detailHintEl.innerHTML = editable ? "Click the text to rewrite it in your own words." : "";
  detailHintEl.hidden = !editable;
}

function commitEditing() {
  if (!editingItem) return;
  const next = detailClaimInputEl.value.trim();
  const { kind, id, original } = editingItem;
  if (editInFlight && next && next !== original.trim()) {
    // Keep the editor open with the owner's text rather than discarding it.
    // Closing it here would drop the correction on the floor and then report
    // "Saved." for the previous one.
    setEditStatus("Still saving the last change — try again in a moment.", "warn");
    return;
  }
  endEditing();
  if (!next || next === original.trim()) return;
  const item = items.get(selectionKey(kind, id));
  if (item) submitEdit(kind, item, "rewrite", next, "");
}

detailClaimInputEl.addEventListener("input", autoGrow);
detailClaimInputEl.addEventListener("blur", () => {
  // "Click away to save" means clicking somewhere else on this page. Cmd-Tab to
  // check a wording in another app also fires blur, and committing there would
  // save a half-typed sentence as the owner's own words. Focus leaving the
  // window is not an intent signal, so the editor simply stays open.
  if (!document.hasFocus()) return;
  commitEditing();
});
detailClaimInputEl.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.stopPropagation();
    endEditing();
    detailTitleEl.focus();
  } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    commitEditing();
    // Symmetric with the Escape path: a keyboard commit must not strand focus
    // on the document body.
    if (!detailEl.hidden && !detailTitleEl.hidden) detailTitleEl.focus();
  }
});

detailTitleEl.addEventListener("click", () => {
  if (selectedItem) beginEditing(selected?.kind, selectedItem);
});
detailTitleEl.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  if (selectedItem) beginEditing(selected?.kind, selectedItem);
});

detailRejectEl.addEventListener("click", () => {
  if (!selected || !selectedItem) return;
  if (detailRejectEl.dataset.armed !== "true") {
    // One deliberate confirmation: withdrawing a claim is not undoable here.
    detailRejectEl.dataset.armed = "true";
    detailRejectEl.textContent = "Yes, remove it from my model";
    setEditStatus("This leaves your history, but not your model.", null);
    return;
  }
  submitEdit(selected.kind, selectedItem, "retire", "", "");
});

function uneditableNote(kind, item) {
  // A refusal the server already computed explains itself better than a
  // generic "not editable here".
  const refusal = item?.edit_refusal;
  if (refusal && EDIT_REFUSALS[refusal]) return EDIT_REFUSALS[refusal];
  if (kind === "context") {
    return "An entity your model refers to — a person, project, tool, or file."
      + " It is the far end of a relation, not a claim about you, so there is"
      + " nothing here to correct.";
  }
  if (kind === "line") {
    return "A relation between two things your model knows."
      + " Lines are derived from what they connect, so correct those instead.";
  }
  return "";
}

function renderEditor(kind, item) {
  endEditing();
  setEditStatus("", null);
  delete detailRejectEl.dataset.armed;
  detailRejectEl.textContent = "This is wrong about me";
  detailRejectEl.disabled = editInFlight;

  const editable = isEditable(kind, item);
  // Rejecting is a separate capability: a pattern that cannot be reworded can
  // still be withdrawn.
  const rejectable = Boolean(item?.id)
    && EDITABLE_KINDS.has(kind)
    && (editable || item.edit_refusal === "object_not_active");
  detailTitleEl.dataset.editable = String(editable);
  detailActionsEl.hidden = !rejectable;
  // Say what a node IS when it cannot be corrected. "Nothing to edit here" only
  // tells the owner what they cannot do; an entity or a Line is not a claim
  // about them at all, and that is the useful thing to know.
  detailHintEl.innerHTML = editable
    ? "Click the text to rewrite it in your own words."
    : uneditableNote(kind, item);
  detailHintEl.hidden = !detailHintEl.innerHTML;
  // Focusable, so a keyboard user can reach the claim and press Enter to edit
  // it — but never `role="button"`. That would override the heading role and
  // leave the drawer with no heading at all, which is the one landmark a screen
  // reader user navigates it by. `aria-describedby` carries the affordance.
  if (editable) {
    detailTitleEl.setAttribute("title", "Click to correct this in your own words");
    detailTitleEl.tabIndex = 0;
  } else {
    detailTitleEl.removeAttribute("title");
    detailTitleEl.removeAttribute("tabindex");
  }
}

function showDetails(kind, item, returnFocus = null) {
  if (detailEl.hidden) {
    const candidate = returnFocus || document.activeElement;
    selectionReturnFocus = candidate instanceof HTMLElement
      && candidate !== document.body
      && !detailEl.contains(candidate)
      ? candidate
      : openSearchButton;
  }
  const lineDetail = kind === "line" ? linePresentations.get(item.id) : null;
  selected = { kind, id: item.id };
  selectedItem = item;
  pauseAutoRotate();
  // The drawer is one of the boxes labels are culled against, and it has just
  // changed shape.
  invalidatePanelBoxes();
  syncSelectionState();
  detailEl.dataset.kind = kind;
  detailKindEl.textContent = kind === "context" ? "entity" : kind;
  detailTitleEl.textContent = (
    lineDetail?.title
    || item.content || item.signature || item.label || item.predicate || item.kind || item.id
  );
  evidenceTrail = [{ label: detailTitleEl.textContent, data: null }];
  // "Evidence-backed" is a claim about where the text came from, so it must not
  // sit above text the owner wrote themselves.
  const authored = isAuthored(kind, item);
  detailProvenanceEl.textContent = kind === "context"
    ? "Referenced by your model"
    : (authored ? "Your words" : "Evidence-backed");
  detailProvenanceEl.classList.toggle("detail-authored", authored);
  detailMetaEl.replaceChildren();
  appendMeta("Layer", item.layer || item.level);
  appendMeta("Status", item.status);
  appendMeta("Type", item.kind);
  appendMeta("Predicate", lineDetail?.predicate);
  appendMeta("Label", lineDetail?.label);
  appendMeta("From", lineDetail?.source);
  appendMeta("To", lineDetail?.target);
  appendMeta("Confidence", item.confidence);
  appendMeta("Observations", item.observations);
  appendMeta("Valid from", item.valid_from);
  appendMeta("Members", item.members?.length);
  renderOverview(kind, item);
  if (lineDetail) appendLineTechnicalDetails(item);
  renderNodeEvidence(kind, item);
  renderNodeHistory(item);
  renderEditor(kind, item);
  editAlertEl.hidden = true;
  // Folds start closed on every selection: the claim is what the owner came to
  // read, and provenance is one click away rather than one of four places the
  // content might be hiding.
  detailEvidenceFoldEl.open = false;
  detailHistoryFoldEl.open = false;
  detailEl.scrollTop = 0;
  detailEl.hidden = false;
}

// An owner-authored object must not read as a machine-derived claim. Faces,
// Volumes and the Root carry `provenance="authored"`; Points carry the
// `source:owner-edit` tag.
function isAuthored(kind, item) {
  if (kind === "point") return String(item.tags || "").split(/\s+/).includes("source:owner-edit");
  return item.provenance === "authored";
}

function updateTimelineBounds() {
  const dated = [
    ...model.points,
    ...model.lines,
    ...model.faces,
    ...model.volumes,
    ...(model.root ? [model.root] : []),
  ].map(itemTime).filter(Boolean).sort((a, b) => a - b);
  minTime = dated[0] || new Date();
  maxTime = new Date();
  updateCutoff();
}

function updateCutoff() {
  const fraction = Number(slider.value) / 100;
  cutoff = new Date(minTime.getTime() + (maxTime.getTime() - minTime.getTime()) * fraction);
  sliderLabel.textContent = fraction >= 1 ? "Now" : cutoff.toISOString().slice(0, 10);
}

function showModelLoadError(error) {
  const message = document.createElement("p");
  message.textContent = error?.name === "AbortError"
    ? "The model is taking too long to load. The Runtime may still be building it."
    : `Unable to load the personal model. ${error?.message || error}`;
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "Retry";
  retry.addEventListener("click", () => loadModel(true));
  errorEl.replaceChildren(message, retry);
  errorEl.hidden = false;
}

function updateHealthBanner(health) {
  let banner = document.getElementById("index-health-banner");
  if (!health) {
    if (banner) banner.hidden = true;
    return;
  }
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "index-health-banner";
    banner.setAttribute("role", "status");
    document.body.appendChild(banner);
  }
  const label = health.status === "unknown" ? "Runtime health unknown" : "Evidence layer degraded";
  banner.textContent = `${label}: ${health.note || ""} — see \`persome status\``;
  banner.hidden = false;
}

async function fetchModelGraph() {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), MODEL_GRAPH_TIMEOUT_MS);
  try {
    const response = await fetch("./graph", {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`Model endpoint returned HTTP ${response.status}`);
    const payload = await response.json();
    if (
      !String(payload.generated_at || "")
      || !payload.model
      || !Array.isArray(payload.model.points)
    ) {
      throw new Error("Model endpoint returned an invalid snapshot");
    }
    return payload;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function loadModelOnce(force, selectionReplacement = null) {
  try {
    const payload = await fetchModelGraph();
    errorEl.hidden = true;
    const nextHealthFingerprint = JSON.stringify(payload.index_health || null);
    if (
      !force
      && payload.generated_at === modelGeneratedAt
      && nextHealthFingerprint === modelHealthFingerprint
    ) return true;
    const nextFingerprint = modelPollingFingerprint(payload.model, payload.index_health || null);
    if (!force && nextFingerprint === modelFingerprint) {
      modelGeneratedAt = payload.generated_at || "";
      modelHealthFingerprint = nextHealthFingerprint;
      return true;
    }
    model = payload.model;
    modelGeneratedAt = payload.generated_at || "";
    modelHealthFingerprint = nextHealthFingerprint;
    modelFingerprint = nextFingerprint;
    updateHealthBanner(payload.index_health || null);
    shareReady = Boolean(
      model.points.length || model.faces.length || model.volumes.length || model.root,
    );
    setShareBusy(false);
    updateTimelineBounds();
    buildScene({ selectionReplacement });
    // A poll can update a Line or structural object in place while its drawer
    // is open. Rebind the selection to the fresh snapshot and redraw the
    // drawer, unless the owner is in the middle of editing a Point.
    if (selected && !editingItem) {
      const freshItem = items.get(selectionKey(selected.kind, selected.id))
        || searchItems.get(selectionKey(selected.kind, selected.id));
      if (freshItem) showDetails(selected.kind, freshItem, selectionReturnFocus);
    }
    return true;
  } catch (error) {
    shareReady = false;
    setShareBusy(false);
    showModelLoadError(error);
    // Reported rather than swallowed: a caller that just wrote to the model
    // needs to know it is still looking at pre-write state.
    return false;
  }
}

function loadModel(force = false, selectionReplacement = null) {
  // A forced load must actually observe the server again. Returning an
  // in-flight promise silently dropped `force`, so a poll issued microseconds
  // before a correction was saved would satisfy the reload that follows the
  // save — and the drawer would re-render from the pre-edit snapshot while
  // reporting success. A forced load now queues behind whatever is running.
  if (modelLoadPromise && !force) return modelLoadPromise;
  const run = () => loadModelOnce(force, selectionReplacement);
  const started = modelLoadPromise ? modelLoadPromise.then(run, run) : run();
  const chained = started.finally(() => {
    // Only the newest load clears the slot; an older one finishing later must
    // not blank out a load that is still running.
    if (modelLoadPromise === chained) modelLoadPromise = null;
  });
  modelLoadPromise = chained;
  return chained;
}

function frameLayout(force) {
  const overview = fittedOverviewPose(layoutRadius, window.innerWidth, window.innerHeight);
  portraitMode = overview.portrait;
  if (!force && framedRadius > 0 && overview.radius <= framedRadius * 1.16) return;
  fitDistance = overview.distance;
  cameraFlight = null;
  zoomGoalDistance = null;
  camera.position.set(...overview.position);
  controls.target.set(...overview.target);
  controls.minDistance = overview.distance * 100 / ZOOM_MAX_PERCENT;
  controls.maxDistance = overview.distance * 100 / ZOOM_MIN_PERCENT;
  // Zooming at the cursor walks the orbit centre towards whatever is under it.
  // Bound how far it may wander so a long pinch cannot strand the constellation
  // off-screen with nothing left to orbit around.
  controls.maxTargetRadius = overview.radius * 1.5;
  zoomAnchor = null;
  framedRadius = overview.radius;
  controls.update();
  syncZoomUI();
}

function resetCamera() {
  frameLayout(true);
}

function selectionPosition(kind, id) {
  if (kind === "line") {
    const line = sceneModel.lines.find((item) => item.id === id);
    const start = line ? positionForGraphId(line.source) : null;
    const end = line ? positionForGraphId(line.target) : null;
    if (start && end) return start.clone().lerp(end, 0.5);
  }
  return positions.get(id)?.clone() || null;
}

function cameraFocusDistance(kind) {
  const factor = {
    root: 0.7,
    volume: 0.56,
    face: 0.42,
    line: 0.36,
    point: 0.3,
    context: 0.3,
  }[kind] || 0.42;
  const cap = kind === "root" ? 18 : (kind === "volume" ? 14 : 10);
  return THREE.MathUtils.clamp(
    Math.min(cap, fitDistance * factor),
    controls.minDistance,
    controls.maxDistance,
  );
}

function flyToSelection(kind, id) {
  const target = selectionPosition(kind, id);
  if (!target) return;
  pauseAutoRotate();
  zoomGoalDistance = null;
  zoomAnchor = null;

  const direction = camera.position.clone().sub(controls.target);
  if (direction.lengthSq() < 1e-8) direction.set(0.72, 0.9, 1);
  direction.normalize().applyAxisAngle(new THREE.Vector3(0, 1, 0), 0.16);
  direction.y += 0.06;
  direction.normalize().multiplyScalar(cameraFocusDistance(kind));
  const destination = target.clone().add(direction);
  const travel = controls.target.distanceTo(target) + camera.position.distanceTo(destination) * 0.24;
  const duration = REDUCED_MOTION ? 0 : THREE.MathUtils.clamp(0.56 + travel * 0.025, 0.56, 1.15);
  cameraFlight = {
    elapsed: 0,
    duration,
    fromPosition: camera.position.clone(),
    toPosition: destination,
    fromTarget: controls.target.clone(),
    toTarget: target,
  };
  if (!duration) applyCameraFlight(0);
}

function applyCameraFlight(deltaSeconds) {
  if (!cameraFlight) return;
  const flight = cameraFlight;
  flight.elapsed += deltaSeconds;
  const progress = flight.duration ? Math.min(1, flight.elapsed / flight.duration) : 1;
  const eased = progress < 0.5
    ? 4 * progress * progress * progress
    : 1 - Math.pow(-2 * progress + 2, 3) / 2;
  camera.position.lerpVectors(flight.fromPosition, flight.toPosition, eased);
  controls.target.lerpVectors(flight.fromTarget, flight.toTarget, eased);
  controls.update();
  if (progress >= 1) cameraFlight = null;
}

function clampZoomPercent(value) {
  return zoomMath.clampPercent(value, ZOOM_MIN_PERCENT, ZOOM_MAX_PERCENT);
}

function currentZoomPercent() {
  const distance = camera.position.distanceTo(controls.target);
  return zoomMath.percentForDistance(
    fitDistance,
    distance,
    ZOOM_MIN_PERCENT,
    ZOOM_MAX_PERCENT,
  );
}

// Rebuilt in place rather than reallocated: syncZoomUI runs once per frame and
// a fresh object literal per frame is pure garbage for the collector to chase
// during exactly the gesture we are trying to keep smooth.
window.__persomeZoomState = {
  percent: 100,
  distance: 0,
  fitDistance: 0,
  minPercent: ZOOM_MIN_PERCENT,
  maxPercent: ZOOM_MAX_PERCENT,
  animating: false,
};

function syncZoomUI() {
  const distance = camera.position.distanceTo(controls.target);
  const percent = zoomMath.percentForDistance(
    fitDistance,
    distance,
    ZOOM_MIN_PERCENT,
    ZOOM_MAX_PERCENT,
  );
  // The DOM writes are the expensive half. They only mean anything when the
  // rounded percent actually changes, which during a glide is a few frames out
  // of every hundred.
  if (percent !== lastZoomPercent) {
    zoomResetButton.textContent = `${percent}%`;
    zoomResetButton.setAttribute(
      "aria-label",
      `Reset zoom to 100 percent (currently ${percent} percent)`,
    );
    zoomOutButton.disabled = percent <= ZOOM_MIN_PERCENT;
    zoomInButton.disabled = percent >= ZOOM_MAX_PERCENT;
    lastZoomPercent = percent;
  }
  const state = window.__persomeZoomState;
  state.percent = percent;
  state.distance = Number(distance.toFixed(3));
  state.fitDistance = Number(fitDistance.toFixed(3));
  state.animating = zoomGoalDistance !== null || cameraFlight !== null;
}

function requestZoom(percent) {
  cameraFlight = null;
  const clamped = clampZoomPercent(percent);
  zoomGoalDistance = THREE.MathUtils.clamp(
    fitDistance * 100 / clamped,
    controls.minDistance,
    controls.maxDistance,
  );
  zoomAnchor = null;
}

// Wheel and pinch drive the goal multiplicatively from wherever the glide is
// already headed, so a stream of small events composes into one continuous
// gesture instead of repeatedly restarting from the camera's current position.
function requestZoomBy(factor, anchor) {
  cameraFlight = null;
  const base = zoomGoalDistance === null
    ? camera.position.distanceTo(controls.target)
    : zoomGoalDistance;
  zoomGoalDistance = THREE.MathUtils.clamp(
    base / factor,
    controls.minDistance,
    controls.maxDistance,
  );
  zoomAnchor = anchor || null;
}

function stepZoom(direction) {
  const current = zoomGoalDistance === null
    ? currentZoomPercent()
    : zoomMath.percentForDistance(
      fitDistance,
      zoomGoalDistance,
      ZOOM_MIN_PERCENT,
      ZOOM_MAX_PERCENT,
    );
  requestZoom(zoomMath.nextPercent(
    current,
    direction,
    ZOOM_STEP_PERCENT,
    ZOOM_MIN_PERCENT,
    ZOOM_MAX_PERCENT,
  ));
}

// Where the ray through `ndc` meets the plane that carries the orbit target and
// faces the camera. Zooming keeps this point pinned under the pointer.
function anchorWorldPoint(ndc, out) {
  camera.updateMatrixWorld();
  zoomRaycaster.setFromCamera(ndc, camera);
  camera.getWorldDirection(zoomViewDirection);
  zoomPlane.setFromNormalAndCoplanarPoint(zoomViewDirection, controls.target);
  return zoomRaycaster.ray.intersectPlane(zoomPlane, out);
}

function applyZoomAnimation(deltaSeconds) {
  if (zoomGoalDistance === null) return;
  const currentDistance = camera.position.distanceTo(controls.target);
  const nextDistance = REDUCED_MOTION
    ? zoomGoalDistance
    : THREE.MathUtils.damp(currentDistance, zoomGoalDistance, 12, deltaSeconds);
  const settled = Math.abs(nextDistance - zoomGoalDistance) < 0.01;
  const landedDistance = settled ? zoomGoalDistance : nextDistance;
  const anchored = zoomAnchor !== null
    && anchorWorldPoint(zoomAnchor, zoomAnchorBefore) !== null;

  zoomDirection.copy(camera.position).sub(controls.target);
  if (zoomDirection.lengthSq() < 1e-8) zoomDirection.set(0, 0, 1);
  zoomDirection.setLength(landedDistance);
  camera.position.copy(controls.target).add(zoomDirection);

  if (anchored && anchorWorldPoint(zoomAnchor, zoomAnchorAfter) !== null) {
    // Translating the camera and the orbit target by the same vector leaves the
    // spherical radius, theta and phi untouched, so OrbitControls' damping
    // carries on undisturbed while the anchored point stays under the pointer.
    zoomShift.copy(zoomAnchorBefore).sub(zoomAnchorAfter);
    camera.position.add(zoomShift);
    controls.target.add(zoomShift);
  }

  if (settled) {
    zoomGoalDistance = null;
    zoomAnchor = null;
  }
}

// Reading an element's box forces the browser to flush pending layout. The
// render loop writes a fresh inline transform onto every label each frame, so
// layout is always dirty by the time culling runs — measuring here meant one
// full-document reflow per frame, across hundreds of labels. Nothing being
// measured actually changes that often: a label pill is sized by its own text
// and a fixed max-width, and the overlay panels move only on resize or when the
// drawer opens. Measure each once and cache.
let occupiedPanels = null;

function invalidatePanelBoxes() {
  occupiedPanels = null;
  canvasBounds = null;
}

const panelResizeObserver = "ResizeObserver" in window
  ? new ResizeObserver(invalidatePanelBoxes)
  : null;
if (panelResizeObserver) {
  document.querySelectorAll(
    ".story, .legend, .mobile-guide, .status, .timeline, .detail, .search-dialog",
  ).forEach((element) => panelResizeObserver.observe(element));
}
document.querySelector(".legend")?.addEventListener("toggle", invalidatePanelBoxes);
mobileGuideEl?.addEventListener("toggle", invalidatePanelBoxes);
detailEvidenceFoldEl.addEventListener("toggle", invalidatePanelBoxes);
detailHistoryFoldEl.addEventListener("toggle", invalidatePanelBoxes);

function panelBoxes() {
  if (occupiedPanels) return occupiedPanels;
  occupiedPanels = [...document.querySelectorAll(
    ".story, .legend, .mobile-guide, .status, .timeline, .detail:not([hidden]), .search-dialog",
  )]
    .map((element) => element.getBoundingClientRect())
    .filter((box) => box.width > 0 && box.height > 0)
    .map((box) => ({ x0: box.left - 6, x1: box.right + 6, y0: box.top - 6, y1: box.bottom + 6 }));
  return occupiedPanels;
}

const cullProjected = new THREE.Vector3();
const cullCandidates = [];

function cullLabels() {
  const occupied = panelBoxes().slice();
  const mobile = window.innerWidth < 760;
  const maxLabels = mobile ? (selected ? 9 : 8) : 20;
  const maxWidth = mobile ? 130 : 220;
  const safeArea = {
    left: mobile ? 8 : 6,
    right: window.innerWidth - (mobile ? 8 : 6),
    top: mobile ? 144 : 104,
    bottom: window.innerHeight - (mobile ? 70 : 64),
  };
  let shown = 0;
  const projected = cullProjected;
  cullCandidates.length = 0;
  labels.forEach((label) => {
    if (!label.visible) return;
    if (label.element.classList.contains("focus-muted")) {
      label.element.classList.add("hidden");
      label.element.setAttribute("aria-hidden", "true");
      label.element.tabIndex = -1;
      return;
    }
    label.getWorldPosition(projected);
    projected.project(camera);
    const x = (projected.x * 0.5 + 0.5) * window.innerWidth;
    const y = (-projected.y * 0.5 + 0.5) * window.innerHeight;
    // Measure only labels that are on screen and not yet known. Culling runs
    // before labelRenderer.render(), so a freshly built label is not in the
    // document on its first pass and measures zero — cache that and the size is
    // wrong for as long as the label lives. A culled label is display:none and
    // measures zero too, so asking it costs a reflow and answers nothing.
    // Everything else settles after one read and is never measured again.
    if (!label.userData.measuredWidth && !label.element.classList.contains("hidden")) {
      const measured = label.element.offsetWidth;
      if (measured) {
        label.userData.measuredWidth = Math.max(32, measured);
        label.userData.measuredHeight = Math.max(21, label.element.offsetHeight || 21);
      }
    }
    const estimatedWidth = (label.element.textContent.length * 6.2) + 16;
    cullCandidates.push({
      label,
      x,
      y,
      width: Math.min(maxWidth, label.userData.measuredWidth || Math.max(32, estimatedWidth)),
      height: label.userData.measuredHeight || 21,
      priority: (label.userData.priority || 0)
        + (label.element.getAttribute("aria-expanded") === "true" ? 1000 : 0)
        + (label.element.classList.contains("focus-neighbor") ? 500 : 0),
      depth: projected.z,
    });
  });
  const candidates = cullCandidates.sort((a, b) => b.priority - a.priority);

  candidates.forEach((candidate) => {
    const { label, x, y, width, height, depth } = candidate;
    const box = { x0: x - width / 2, x1: x + width / 2, y0: y - height / 2, y1: y + height / 2 };
    const overlaps = occupied.some((other) => box.x0 < other.x1 && box.x1 > other.x0 && box.y0 < other.y1 && box.y1 > other.y0);
    const outsideSafeArea = box.x0 < safeArea.left || box.x1 > safeArea.right
      || box.y0 < safeArea.top || box.y1 > safeArea.bottom;
    const hidden = depth < -1 || depth > 1 || outsideSafeArea || overlaps || shown >= maxLabels;
    if (label.element.classList.contains("hidden") !== hidden) {
      label.element.classList.toggle("hidden", hidden);
      label.element.setAttribute("aria-hidden", String(hidden));
      label.element.tabIndex = hidden ? -1 : 0;
    }
    if (!hidden) {
      occupied.push(box);
      shown += 1;
    }
  });
  window.__persomeLabelHealth = { total: labels.length, shown, max: maxLabels };
}

// Each gl.readPixels blocks until the GPU catches up, so this 77-point sweep is
// 77 pipeline stalls. It only latched once it saw something lit, and a viewer
// whose model is empty, still loading, or has every layer toggled off never
// does — so the render loop paid the whole sweep on every frame, forever, in
// exactly the states where the owner is most likely to be poking at the
// controls. The sweep is unchanged; it is now bounded, and it reuses one
// buffer instead of allocating 77 per frame. It must stay inside the render
// task: without `preserveDrawingBuffer` the pixels are only valid there.
const RENDER_PROBE_MAX_SWEEPS = 30;
const renderProbePixel = new Uint8Array(4);
let renderProbeSweeps = 0;

function samplePixels() {
  if (window.__persomeModelRender?.lit > 0) return;
  if (renderProbeSweeps >= RENDER_PROBE_MAX_SWEEPS) return;
  renderProbeSweeps += 1;
  const gl = renderer.getContext();
  const width = gl.drawingBufferWidth;
  const height = gl.drawingBufferHeight;
  let lit = 0;
  let checked = 0;
  for (let y = 1; y < 8; y += 1) {
    for (let x = 1; x < 12; x += 1) {
      gl.readPixels(Math.floor(width * x / 12), Math.floor(height * y / 8), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, renderProbePixel);
      checked += 1;
      if (renderProbePixel[0] + renderProbePixel[1] + renderProbePixel[2] > 96) lit += 1;
    }
  }
  window.__persomeModelRender = { width, height, checked, lit, sweeps: renderProbeSweeps };
  canvasHost.dataset.litPixels = String(lit);
}

document.querySelectorAll("[data-layer]").forEach((button) => {
  button.addEventListener("click", () => {
    const layer = button.dataset.layer;
    layerVisible[layer] = !layerVisible[layer];
    applyLayerVisibility();
    if (window.__persomeViewerState) window.__persomeViewerState.layers = { ...layerVisible };
  });
});

openSearchButton.addEventListener("click", openSearch);
closeSearchButton.addEventListener("click", () => closeSearch());
searchInputEl.addEventListener("input", () => {
  searchActiveIndex = 0;
  renderSearchResults();
});
searchPanelEl.addEventListener("click", (event) => {
  if (event.target === searchPanelEl) closeSearch();
});
searchPanelEl.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closeSearch();
    return;
  }
  if (event.key === "ArrowDown") {
    event.preventDefault();
    setSearchActiveIndex(searchActiveIndex + 1);
    return;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    setSearchActiveIndex(searchActiveIndex - 1);
    return;
  }
  if (event.key === "Enter" && event.target === searchInputEl && searchMatches[searchActiveIndex]) {
    event.preventDefault();
    selectSearchMatch(searchMatches[searchActiveIndex]);
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...searchPanelEl.querySelectorAll("button, input")]
    .filter((element) => (
      !element.disabled && element.tabIndex >= 0 && element.getClientRects().length > 0
    ));
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

lineSelectEl.addEventListener("change", () => {
  const index = Number(lineSelectEl.value) - 1;
  const item = lineNavigatorItems[index];
  if (item) showDetails("line", item);
});


rotateButton.addEventListener("click", (event) => {
  controls.autoRotate = !controls.autoRotate;
  controls.autoRotateSpeed = 0.7;
  event.currentTarget.setAttribute("aria-pressed", String(controls.autoRotate));
});
zoomOutButton.addEventListener("click", () => stepZoom(-1));
zoomResetButton.addEventListener("click", () => requestZoom(100));
zoomInButton.addEventListener("click", () => stepZoom(1));
controls.addEventListener("start", () => {
  cameraFlight = null;
  zoomGoalDistance = null;
  zoomAnchor = null;
});

// Zoom input lives on the whole viewer, not just the canvas: the topbar, the
// legend and the status strip cover a good part of the screen, and a wheel that
// lands on one of them used to fall through to the browser instead of the
// model. The drawer is a real scroll container, so it keeps its own wheel.
function anchorFromClient(clientX, clientY) {
  // Cached: a trackpad delivers wheel events faster than the display refreshes,
  // and this rect only moves when the window does.
  if (!canvasBounds) canvasBounds = renderer.domElement.getBoundingClientRect();
  const bounds = canvasBounds;
  if (!bounds.width || !bounds.height) return null;
  zoomAnchorNdc.set(
    ((clientX - bounds.left) / bounds.width) * 2 - 1,
    -((clientY - bounds.top) / bounds.height) * 2 + 1,
  );
  return zoomAnchorNdc;
}

// Safari reports a trackpad pinch as its own gesture events and never as a
// ctrlKey wheel, so a viewer that only listens for the wheel has no pinch at
// all there. These listeners are registered unconditionally: engines that do
// not implement the events simply never fire them, which is cheaper and more
// honest than sniffing for a vendor hook whose name has moved around.
let gestureScale = 0;
let gestureSeenAt = 0;
const gestureAnchor = new THREE.Vector2();
const GESTURE_IDLE_MS = 400;

// A gesture whose `gestureend` never arrives must not wedge the wheel off for
// the rest of the session, so an idle gesture is treated as finished.
function gestureInFlight() {
  if (!gestureScale) return false;
  if (performance.now() - gestureSeenAt < GESTURE_IDLE_MS) return true;
  gestureScale = 0;
  return false;
}

// Capture phase on the whole viewer, so this runs before OrbitControls' own
// wheel listener on the canvas and stops the event reaching it. That leaves
// OrbitControls' touch and middle-button dolly intact while the wheel — the
// path with the bad normalisation and no damping — belongs to the viewer. It
// also means a wheel over the topbar, legend or status strip zooms the model
// instead of falling through to the browser.
viewerEl.addEventListener("wheel", (event) => {
  // The drawer, line picker, and search panel own ordinary scrolling. A
  // ctrlKey wheel is a Chrome/Firefox trackpad pinch, though, and must remain
  // model navigation even when the gesture starts over one of those panels.
  if (!shouldHandleModelGesture(event)) return;
  event.stopPropagation();
  // Prevent browser page zoom even when Safari also reports this pinch through
  // GestureEvents and the duplicate wheel is discarded below.
  event.preventDefault();
  // If a gesture is in flight this wheel is the same fingers counted twice.
  if (gestureInFlight()) return;
  // Chrome and Firefox report a trackpad pinch as a wheel with ctrlKey set,
  // which is also the browser's own page-zoom chord: without preventDefault the
  // page would zoom instead of the model.
  pauseAutoRotate();
  requestZoomBy(
    zoomMath.wheelFactor(event, window.innerHeight),
    anchorFromClient(event.clientX, event.clientY),
  );
}, { passive: false, capture: true });

viewerEl.addEventListener("gesturestart", (event) => {
  // Safari GestureEvents always describe a pinch, never ordinary scrolling,
  // so panels do not opt out as they do for a plain wheel.
  if (!shouldHandleModelGesture(event)) return;
  event.preventDefault();
  gestureScale = 1;
  gestureSeenAt = performance.now();
  if (anchorFromClient(event.clientX, event.clientY)) gestureAnchor.copy(zoomAnchorNdc);
  else gestureAnchor.set(0, 0);
  pauseAutoRotate();
}, { passive: false, capture: true });
viewerEl.addEventListener("gesturechange", (event) => {
  if (!gestureScale) return;
  event.preventDefault();
  gestureSeenAt = performance.now();
  const scale = Number(event.scale);
  if (!Number.isFinite(scale) || scale <= 0) return;
  // `scale` is cumulative since gesturestart, so the step is the ratio since
  // the last event — which composes into the same goal a wheel stream would.
  zoomAnchorNdc.copy(gestureAnchor);
  requestZoomBy(scale / gestureScale, zoomAnchorNdc);
  gestureScale = scale;
}, { passive: false, capture: true });
viewerEl.addEventListener("gestureend", (event) => {
  if (!gestureScale) return;
  event.preventDefault();
  gestureScale = 0;
}, { passive: false, capture: true });
document.getElementById("reset").addEventListener("click", resetCamera);
document.getElementById("close-detail").addEventListener("click", () => clearSelection(true));
clearFocusButton.addEventListener("click", showAllModel);
cardButton.addEventListener("click", exportHumanCard);
shareButton.addEventListener("click", shareConstellationToX);

function rebuildAtSelectedCutoff() {
  // Detail data is cutoff-bound. Closing the selection both removes already
  // rendered future evidence and increments evidenceRequest, so an unbounded
  // Now request that resolves after time travel cannot repopulate the drawer.
  clearSelection(false);
  updateCutoff();
  buildScene();
}

slider.addEventListener("input", () => {
  rebuildAtSelectedCutoff();
});

document.getElementById("play").addEventListener("click", (event) => {
  const button = event.currentTarget;
  if (playTimer) {
    window.clearInterval(playTimer);
    playTimer = null;
    button.textContent = "▶";
    button.setAttribute("aria-pressed", "false");
    return;
  }
  if (Number(slider.value) >= 100) {
    slider.value = "0";
    rebuildAtSelectedCutoff();
  }
  button.textContent = "Ⅱ";
  button.setAttribute("aria-pressed", "true");
  playTimer = window.setInterval(() => {
    const next = Number(slider.value) + 2;
    slider.value = String(Math.min(100, next));
    rebuildAtSelectedCutoff();
    if (next >= 100) button.click();
  }, 240);
});

function pickAt(event) {
  const bounds = renderer.domElement.getBoundingClientRect();
  if (!bounds.width || !bounds.height) return null;
  const localPointer = {
    x: event.clientX - bounds.left,
    y: event.clientY - bounds.top,
  };
  const coarsePointer = event.pointerType === "touch" || window.innerWidth < 760;
  const nodeHitRadius = coarsePointer ? TOUCH_NODE_HIT_RADIUS_PX : MIN_NODE_HIT_RADIUS_PX;
  const lineHitRadius = coarsePointer ? TOUCH_LINE_HIT_RADIUS_PX : MIN_LINE_HIT_RADIUS_PX;
  // Projecting into a shared scratch vector rather than cloning per candidate:
  // hover picking runs on the frame after every pointer move, over every
  // visible node, so a clone per node is a steady drip of garbage.
  const projectWorldPoint = (worldPosition) => {
    const projected = pickProjected.copy(worldPosition).project(camera);
    if (projected.z < -1 || projected.z > 1) return null;
    return {
      x: (projected.x + 1) * 0.5 * bounds.width,
      y: (1 - projected.y) * 0.5 * bounds.height,
      depth: projected.z,
    };
  };
  const visiblePickables = pickables.filter((object) => object.visible);
  const nodeCandidates = visiblePickables.map((object) => {
    object.getWorldPosition(pickWorldPosition);
    const projected = projectWorldPoint(pickWorldPosition);
    return projected ? { ...projected, target: object } : null;
  }).filter(Boolean);
  const screenNode = pickScreenTarget(
    localPointer,
    nodeCandidates,
    [],
    { nodeRadius: nodeHitRadius },
  );
  if (screenNode) return { object: screenNode };

  pointer.x = (localPointer.x / bounds.width) * 2 - 1;
  pointer.y = -(localPointer.y / bounds.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const meshHit = raycaster.intersectObjects(visiblePickables, false)[0];
  if (meshHit) return meshHit;

  const lineCandidates = screenLinePickables.filter((object) => object.visible).map((object) => {
    const segment = object.userData.pickSegment;
    const start = segment ? projectWorldPoint(segment.start) : null;
    const end = segment ? projectWorldPoint(segment.end) : null;
    if (!start || !end) return null;
    return {
      start,
      end,
      depth: Math.min(start.depth, end.depth),
      target: object,
    };
  }).filter(Boolean);
  const screenLine = pickScreenTarget(
    localPointer,
    [],
    lineCandidates,
    { lineRadius: lineHitRadius },
  );
  return screenLine ? { object: screenLine } : null;
}

// How far a pointer may travel and still count as a click rather than a drag.
// Measured as a real distance, not the sum of the axes, so a diagonal nudge is
// not penalised twice; a finger wobbles far more than a mouse does.
function clickSlopFor(pointerType) {
  if (pointerType === "touch") return 12;
  if (pointerType === "pen") return 8;
  return 5;
}

renderer.domElement.addEventListener("pointerdown", (event) => {
  // Every button counts as a live gesture: the right button pans and the middle
  // button dollies, and neither wants hover picking running underneath it. Only
  // the primary button can become a click.
  livePointers.add(event.pointerId);
  const primary = event.pointerType !== "mouse" || event.button === 0;
  // A second finger means a pinch. Whatever the first finger was doing, it was
  // not choosing a node, so retire the click candidate rather than letting the
  // gesture end in an accidental selection.
  pointerDown = primary && livePointers.size === 1
    ? { x: event.clientX, y: event.clientY, type: event.pointerType }
    : null;
  if (livePointers.size > 1 || !primary) labelGesture = null;
  hoverDirty = false;
  hoverPointer = null;
  renderer.domElement.style.cursor = "grabbing";
});
renderer.domElement.addEventListener("pointermove", (event) => {
  if (livePointers.size) return;
  hoverPointer = { clientX: event.clientX, clientY: event.clientY };
  hoverDirty = true;
});
renderer.domElement.addEventListener("pointerleave", () => {
  hoverPointer = null;
  hoverDirty = false;
  if (!livePointers.size) renderer.domElement.style.cursor = "grab";
});

// Releases are pruned on the window, not the canvas. A gesture that began on a
// label is captured by the canvas, but a second, uncaptured pointer releases
// wherever it happens to be — on the label, on an overlay — and a pointer left
// behind in the set would stop hover picking and the graph poll for good.
window.addEventListener("pointerup", (event) => livePointers.delete(event.pointerId), true);
window.addEventListener("pointercancel", (event) => livePointers.delete(event.pointerId), true);

renderer.domElement.addEventListener("pointercancel", () => {
  pointerDown = null;
  labelGesture = null;
  hoverPointer = null;
  hoverDirty = false;
  renderer.domElement.style.cursor = "grab";
});
renderer.domElement.addEventListener("pointerup", (event) => {
  const release = pointerUpOutcome(event);
  if (!release.selectionEligible) {
    renderer.domElement.style.cursor = release.cursor;
    return;
  }
  const started = pointerDown;
  const label = labelGesture;
  pointerDown = null;
  labelGesture = null;
  if (!started) {
    renderer.domElement.style.cursor = release.cursor;
    return;
  }
  const dx = event.clientX - started.x;
  const dy = event.clientY - started.y;
  const slop = clickSlopFor(started.type);
  if (dx * dx + dy * dy > slop * slop) {
    renderer.domElement.style.cursor = "grab";
    return;
  }
  // A gesture that began on a label and stayed put is that label's click; the
  // canvas captured the pointer, so the label never sees a click of its own.
  if (label) {
    renderer.domElement.style.cursor = pointerUpOutcome(event, true).cursor;
    showDetails(label.kind, label.item);
    return;
  }
  const hit = pickAt(event);
  renderer.domElement.style.cursor = pointerUpOutcome(event, Boolean(hit)).cursor;
  if (!hit?.object.userData.ref) {
    clearSelection();
    return;
  }
  const ref = hit.object.userData.ref;
  const item = items.get(selectionKey(ref.kind, ref.id));
  if (item) showDetails(ref.kind, item);
});

window.addEventListener("keydown", (event) => {
  const target = event.target;
  const typing = (
    target instanceof HTMLInputElement
    || target instanceof HTMLTextAreaElement
    || target instanceof HTMLSelectElement
    || Boolean(target?.isContentEditable)
  );
  if (handleSearchShortcut(event, Boolean(editingItem), openSearch)) return;
  if (event.key === "Escape" && !searchPanelEl.hidden) {
    event.preventDefault();
    closeSearch();
    return;
  }
  if (event.key === "Escape" && selected) {
    // The claim editor handles its own Escape (discard, keep the drawer open)
    // and stops propagation, so anything reaching here is either not editing or
    // is in some other field. Either way Escape closes the drawer, which is
    // what a single Escape should do.
    if (typing) {
      target.blur();
      return;
    }
    clearSelection(true);
    return;
  }
  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "/") {
    event.preventDefault();
    openSearch();
  } else if (event.key.toLowerCase() === "f" && selected) {
    event.preventDefault();
    flyToSelection(selected.kind, selected.id);
  } else if (event.key === "+" || event.key === "=") {
    event.preventDefault();
    stepZoom(1);
  } else if (event.key === "-" || event.key === "_") {
    event.preventDefault();
    stepZoom(-1);
  } else if (event.key === "0") {
    event.preventDefault();
    requestZoom(100);
  }
});

window.addEventListener("resize", () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
  labelRenderer.setSize(window.innerWidth, window.innerHeight);
  invalidatePanelBoxes();
  const portrait = window.innerWidth / window.innerHeight < 0.72;
  if (portrait !== portraitMode) resetCamera();
});

window.addEventListener("error", (event) => {
  errorEl.textContent = `Viewer error: ${event.message}`;
  errorEl.hidden = false;
});
window.addEventListener("unhandledrejection", (event) => {
  errorEl.textContent = `Viewer error: ${event.reason?.message || event.reason}`;
  errorEl.hidden = false;
});

// A rolling window of how long each frame's own work took, so a smoothness
// regression is a number somebody can read rather than a feeling. Written into
// a pre-allocated ring: a probe that allocates every frame would measure itself.
const FRAME_SAMPLE_COUNT = 240;
const frameSamples = new Float32Array(FRAME_SAMPLE_COUNT);
const frameSamplesSorted = new Float32Array(FRAME_SAMPLE_COUNT);
let frameSampleIndex = 0;
let frameSampleFilled = 0;
window.__persomeFrameStats = { p50: 0, p95: 0, worst: 0, samples: 0 };

function recordFrameCost(milliseconds) {
  frameSamples[frameSampleIndex] = milliseconds;
  frameSampleIndex = (frameSampleIndex + 1) % FRAME_SAMPLE_COUNT;
  frameSampleFilled = Math.min(FRAME_SAMPLE_COUNT, frameSampleFilled + 1);
  if (frameSampleIndex % 30 !== 0) return;
  frameSamplesSorted.set(frameSamples);
  const recent = frameSamplesSorted.subarray(0, frameSampleFilled);
  recent.sort();
  const stats = window.__persomeFrameStats;
  stats.p50 = Number(recent[Math.floor(frameSampleFilled * 0.5)].toFixed(2));
  stats.p95 = Number(recent[Math.floor(frameSampleFilled * 0.95)].toFixed(2));
  stats.worst = Number(recent[frameSampleFilled - 1].toFixed(2));
  stats.samples = frameSampleFilled;
}

function animate(frameTime = performance.now()) {
  const frameStart = performance.now();
  const deltaSeconds = Math.min(Math.max((frameTime - lastFrameTime) / 1000, 0), 0.5);
  lastFrameTime = frameTime;
  applyCameraFlight(deltaSeconds);
  applyZoomAnimation(deltaSeconds);
  // Auto-rotate advances by wall time rather than by frame, so the model turns
  // at one speed whether the display runs at 60Hz or 120Hz.
  controls.update(deltaSeconds);
  syncZoomUI();
  const time = performance.now() * 0.001;
  if (!REDUCED_MOTION) {
    pulseGlows.forEach((glow) => {
      const pulse = 1 + Math.sin(time * 1.2 + glow.userData.glowPhase) * glow.userData.glowPulse;
      glow.scale.setScalar(glow.userData.glowBase * pulse);
    });
  }
  if (hoverDirty && hoverPointer && !livePointers.size) {
    renderer.domElement.style.cursor = pickAt(hoverPointer) ? "pointer" : "grab";
    hoverDirty = false;
  }
  cullLabels();
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
  samplePixels();
  recordFrameCost(performance.now() - frameStart);
  window.requestAnimationFrame(animate);
}

resetCamera();
animate();
await loadModel(true);
window.setInterval(() => {
  // Parsing a multi-megabyte snapshot and fingerprinting every Point takes long
  // enough to drop frames. Never do it under the owner's finger.
  // An inline owner draft is equally important: a background refresh must not
  // retire the selected Point and silently discard or wedge that editor.
  if (livePointers.size || editingItem || editInFlight) return;
  loadModel(false);
}, 5000);
