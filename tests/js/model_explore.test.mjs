import assert from "node:assert/strict";
import test from "node:test";

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
} from "../../resources/model_assets/explore.mjs";

test("poll fingerprint changes for in-place Line presentation edits", () => {
  const line = {
    id: "relation-stable-id",
    kind: "relation",
    source: "self",
    target: "acme",
    predicate: "engaged_with",
    label: "works with",
    quote: "Reviewed the proposal",
    provenance: "observed",
    confidence: 0.8,
    observations: 2,
    polarity: "0",
    valid_from: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
  };
  const model = { points: [], lines: [line], faces: [], volumes: [], root: null };
  const original = modelPollingFingerprint(model);

  for (const [field, value] of [
    ["status", "shadow"],
    ["source", "point-owner"],
    ["target", "other-company"],
    ["label", "advises"],
    ["predicate", "advises"],
    ["polarity", "-"],
    ["confidence", 0.95],
    ["observations", 3],
  ]) {
    assert.notEqual(
      modelPollingFingerprint({ ...model, lines: [{ ...line, [field]: value }] }),
      original,
      field,
    );
  }
});

test("poll fingerprint changes when index health degrades or recovers", () => {
  const model = { points: [], lines: [], faces: [], volumes: [], root: null };
  const healthy = modelPollingFingerprint(model, null);
  const degraded = modelPollingFingerprint(model, {
    status: "degraded",
    note: "capture indexing is failing",
  });

  assert.notEqual(degraded, healthy);
  assert.notEqual(
    modelPollingFingerprint(model, { status: "unknown", note: "report is stale" }),
    degraded,
  );
  assert.equal(modelPollingFingerprint(model, null), healthy);
});

test("poll fingerprint covers every snapshot field and ignores the generation clock", () => {
  const point = {
    id: "point-stable",
    content: "A current claim",
    file_name: "person-alex.md",
    tags: "entity",
    receipt: "⟨point-stable:person-alex.md⟩",
  };
  const root = {
    id: "root-stable",
    signature: "Current synthesis",
    observations: 2,
    confidence: 0.8,
    provenance: "observed",
  };
  const model = { points: [point], lines: [], faces: [], volumes: [], root };
  const original = modelPollingFingerprint(model);

  for (const [collection, field, value] of [
    ["points", "file_name", "person-alex-retyped.md"],
    ["points", "receipt", "⟨point-stable:person-alex-retyped.md⟩"],
    ["points", "edit_refusal", "object_not_active"],
    ["root", "observations", 3],
    ["root", "confidence", 0.95],
    ["root", "provenance", "authored"],
  ]) {
    const changed = collection === "root"
      ? { ...model, root: { ...root, [field]: value } }
      : { ...model, points: [{ ...point, [field]: value }] };
    assert.notEqual(modelPollingFingerprint(changed), original, `${collection}.${field}`);
  }

  assert.equal(
    modelPollingFingerprint({ ...model, generated_at: "2026-08-11T09:00:00Z" }),
    modelPollingFingerprint({ ...model, generated_at: "2026-08-11T09:00:15Z" }),
  );
});

test("keeps an in-progress claim focused when the search chord is pressed", () => {
  let prevented = false;
  let opened = false;
  const event = {
    key: "k",
    metaKey: true,
    ctrlKey: false,
    preventDefault() { prevented = true; },
  };

  assert.equal(handleSearchShortcut(event, true, () => { opened = true; }), true);
  assert.equal(prevented, true);
  assert.equal(opened, false);

  prevented = false;
  event.metaKey = false;
  event.ctrlKey = true;
  assert.equal(handleSearchShortcut(event, true, () => { opened = true; }), true);
  assert.equal(prevented, true);
  assert.equal(opened, false);

  prevented = false;
  assert.equal(handleSearchShortcut(event, false, () => { opened = true; }), true);
  assert.equal(prevented, true);
  assert.equal(opened, true);
});

test("renders only the Point chain head valid at the selected time", () => {
  const predecessor = {
    id: "old",
    is_latest: false,
    status: "shadow",
    valid_from: "2026-01-01T00:00:00Z",
    valid_until: "2026-03-01T00:00:00Z",
  };
  const successor = {
    id: "new",
    is_latest: true,
    status: "active",
    valid_from: "2026-03-01T00:00:00Z",
    valid_until: null,
  };

  assert.equal(pointVisibleAt(predecessor, new Date("2026-02-01T00:00:00Z")), true);
  assert.equal(pointVisibleAt(successor, new Date("2026-02-01T00:00:00Z")), false);
  assert.equal(pointVisibleAt(predecessor, new Date("2026-04-01T00:00:00Z")), false);
  assert.equal(pointVisibleAt(successor, new Date("2026-04-01T00:00:00Z")), true);
  assert.equal(pointVisibleAt({ ...successor, status: "shadow" }, new Date("2026-04-01T00:00:00Z")), false);
  assert.equal(pointKnownAt(successor, new Date("2026-02-01T00:00:00Z")), false);
  assert.equal(pointKnownAt(successor, new Date("2026-03-01T00:00:00Z")), true);
  assert.deepEqual(
    pointSearchMetadata(successor, new Date("2026-02-01T00:00:00Z")),
    {
      state: "future",
      subtitle: "Modeled observation · not yet valid at this date",
      aliases: ["future", "not yet valid"],
      weightAdjustment: -56,
    },
  );
  const model = {
    points: [predecessor, successor],
  };
  const evolution = { kind: "evolution", source: "old", target: "new" };
  assert.equal(lineKnownAt(evolution, model, new Date("2026-02-01T00:00:00Z")), false);
  assert.equal(lineKnownAt(evolution, model, new Date("2026-03-01T00:00:00Z")), true);
});

test("uses creation time when older Points do not carry valid_from", () => {
  const point = {
    id: "created-later",
    is_latest: true,
    status: "active",
    created_at: "2026-03-01T00:00:00Z",
  };

  assert.equal(pointVisibleAt(point, new Date("2026-02-01T00:00:00Z")), false);
  assert.equal(pointKnownAt(point, new Date("2026-02-01T00:00:00Z")), false);
  assert.equal(
    pointSearchMetadata(point, new Date("2026-02-01T00:00:00Z")).state,
    "future",
  );
  assert.equal(pointVisibleAt(point, new Date("2026-03-01T00:00:00Z")), true);

  const occurredOnly = { ...point, created_at: "", occurred_at: "2026-04-01T00:00:00Z" };
  assert.equal(pointKnownAt(occurredOnly, new Date("2026-03-01T00:00:00Z")), false);
  assert.equal(pointKnownAt(occurredOnly, new Date("2026-04-01T00:00:00Z")), true);
});

test("infers a predecessor end from its successor when valid_until is absent", () => {
  const predecessor = {
    id: "entity-old",
    is_latest: false,
    status: "shadow",
    created_at: "2026-01-01T00:00:00Z",
    superseded_by: ["entity-new"],
  };
  const successor = {
    id: "entity-new",
    is_latest: true,
    status: "active",
    created_at: "2026-03-01T00:00:00Z",
    supersedes: ["entity-old"],
  };
  const pointById = new Map([
    [predecessor.id, predecessor],
    [successor.id, successor],
  ]);

  assert.equal(
    pointVisibleAt(predecessor, new Date("2026-02-01T00:00:00Z"), pointById),
    true,
  );
  assert.equal(
    pointVisibleAt(successor, new Date("2026-02-01T00:00:00Z"), pointById),
    false,
  );
  assert.equal(
    pointVisibleAt(predecessor, new Date("2026-03-01T00:00:00Z"), pointById),
    false,
  );
  assert.equal(
    pointVisibleAt(successor, new Date("2026-03-01T00:00:00Z"), pointById),
    true,
  );
});

test("preserves ordinary panel scrolling but captures pinch everywhere", () => {
  const targetIn = (className) => ({
    closest(selector) {
      return selector.split(", ").includes(className) ? { className } : null;
    },
  });
  const eventIn = (className, properties = {}) => ({
    type: "wheel",
    target: targetIn(className),
    ...properties,
  });

  assert.equal(shouldHandleModelGesture(eventIn(".legend")), true);
  assert.equal(shouldHandleModelGesture(eventIn(".detail")), false);
  assert.equal(shouldHandleModelGesture(eventIn(".line-explorer")), false);
  assert.equal(shouldHandleModelGesture(eventIn(".search-panel")), false);

  assert.equal(shouldHandleModelGesture(eventIn(".detail", { ctrlKey: true })), true);
  assert.equal(shouldHandleModelGesture(eventIn(".line-explorer", { ctrlKey: true })), true);
  assert.equal(shouldHandleModelGesture(eventIn(".search-panel", { ctrlKey: true })), true);
  assert.equal(shouldHandleModelGesture(eventIn(".detail", { type: "gesturestart" })), true);
});

test("restores the grab cursor after non-primary mouse gestures", () => {
  assert.deepEqual(
    pointerUpOutcome({ pointerType: "mouse", button: 1 }, true),
    { selectionEligible: false, cursor: "grab" },
  );
  assert.deepEqual(
    pointerUpOutcome({ pointerType: "mouse", button: 2 }, true),
    { selectionEligible: false, cursor: "grab" },
  );
  assert.deepEqual(
    pointerUpOutcome({ pointerType: "mouse", button: 0 }, true),
    { selectionEligible: true, cursor: "pointer" },
  );
});

test("invalidates hidden selections but carries a correction to its successor", () => {
  const kindLayers = { point: "points", face: "faces" };
  const visible = { points: true, faces: true };
  const items = new Map([
    ["point:point-new", { id: "point-new" }],
    ["face:face-a", { id: "face-a" }],
  ]);

  assert.deepEqual(
    reconcileSceneSelection(
      { kind: "point", id: "point-old" },
      items,
      visible,
      kindLayers,
      { kind: "point", fromId: "point-old", toId: "point-new" },
    ),
    {
      selection: { kind: "point", id: "point-new" },
      invalidated: false,
      replaced: true,
    },
  );
  assert.deepEqual(
    reconcileSceneSelection(
      { kind: "point", id: "point-old" },
      items,
      visible,
      kindLayers,
    ),
    { selection: null, invalidated: true, replaced: false },
  );
  assert.deepEqual(
    reconcileSceneSelection(
      { kind: "face", id: "face-a" },
      items,
      { ...visible, faces: false },
      kindLayers,
    ),
    { selection: null, invalidated: true, replaced: false },
  );

  const recovery = [];
  assert.equal(
    recoverInvalidSceneSelection(
      { invalidated: true },
      () => recovery.push("clear"),
      () => recovery.push("frame"),
    ),
    true,
  );
  assert.deepEqual(recovery, ["clear", "frame"]);
});

test("ranks exact and title-prefix matches ahead of metadata and fuzzy matches", () => {
  const entries = [
    { key: "point:1", kind: "point", title: "Build an evidence trail", subtitle: "Point" },
    { key: "face:1", kind: "face", title: "Evidence-led product work", subtitle: "Stable pattern" },
    { key: "volume:1", kind: "volume", title: "Product craft", subtitle: "Evidence systems" },
    { key: "root:1", kind: "root", title: "Evidence", subtitle: "Current personal model" },
  ];

  assert.deepEqual(
    rankSearchEntries(entries, "evidence").map((entry) => entry.key),
    ["root:1", "face:1", "point:1", "volume:1"],
  );
  assert.equal(rankSearchEntries(entries, "evdnc")[0].key, "root:1");
});

test("uses explicit entry weights with deterministic kind tie-breaking for empty search", () => {
  const entries = [
    { key: "point:1", kind: "point", title: "Point", weight: 999 },
    { key: "face:1", kind: "face", title: "Face", weight: 10 },
    { key: "root:1", kind: "root", title: "Root", weight: 10 },
  ];

  assert.deepEqual(
    rankSearchEntries(entries, "").map((entry) => entry.key),
    ["point:1", "root:1", "face:1"],
  );
});

test("labels inactive Points explicitly and keeps history discoverable below current claims", () => {
  const points = [
    { id: "current", content: "Protect focused attention", is_latest: true, status: "active" },
    { id: "shadow", content: "Protect focused attention", is_latest: true, status: "shadow" },
    { id: "history", content: "Protect focused attention", is_latest: false, status: "superseded" },
  ];
  const entries = prepareSearchEntries(points.map((point) => {
    const metadata = pointSearchMetadata(point);
    return {
      key: `point:${point.id}`,
      kind: "point",
      title: point.content,
      subtitle: metadata.subtitle,
      aliases: metadata.aliases,
      stateAliases: metadata.aliases,
      weight: 6 + metadata.weightAdjustment,
      searchState: metadata.state,
    };
  }));

  assert.deepEqual(
    entries.map(({ searchState, subtitle }) => ({ searchState, subtitle })),
    [
      { searchState: "current", subtitle: "Modeled observation · current" },
      { searchState: "shadow", subtitle: "Modeled observation · shadow · not active" },
      { searchState: "history", subtitle: "Modeled observation · historical · superseded" },
    ],
  );
  assert.deepEqual(
    rankSearchEntries(entries, "protect focused").map((entry) => entry.key),
    ["point:current", "point:shadow", "point:history"],
  );
  const noisyFaces = prepareSearchEntries(Array.from({ length: 12 }, (_, index) => ({
    key: `face:${index}`,
    kind: "face",
    title: `Sensible habits allow deliberate outcomes weekly ${index}`,
    subtitle: "Stable pattern",
    weight: 20,
  })));
  assert.equal(rankSearchEntries([...entries, ...noisyFaces], "historical")[0].key, "point:history");
  assert.equal(rankSearchEntries([...entries, ...noisyFaces], "shadow")[0].key, "point:shadow");

  const endedPoint = {
    id: "ended",
    is_latest: true,
    status: "active",
    valid_until: "2026-03-01T00:00:00Z",
  };
  assert.equal(
    pointSearchMetadata(endedPoint, new Date("2026-02-28T23:59:59Z")).state,
    "current",
  );
  assert.deepEqual(
    pointSearchMetadata(endedPoint, new Date("2026-03-01T00:00:00Z")),
    {
      state: "history",
      subtitle: "Modeled observation · historical · ended",
      aliases: ["history", "historical", "not current", "ended"],
      weightAdjustment: -72,
    },
  );

  const predecessor = {
    id: "predecessor",
    is_latest: false,
    status: "shadow",
    valid_from: "2026-01-01T00:00:00Z",
    valid_until: "2026-03-01T00:00:00Z",
  };
  assert.deepEqual(
    pointSearchMetadata(predecessor, new Date("2026-02-01T00:00:00Z")),
    {
      state: "current",
      subtitle: "Modeled observation · current",
      aliases: ["current", "active"],
      weightAdjustment: 0,
    },
  );
  assert.deepEqual(
    pointSearchMetadata(predecessor, new Date("2026-03-01T00:00:00Z")),
    {
      state: "history",
      subtitle: "Modeled observation · historical · ended · shadow",
      aliases: ["history", "historical", "not current", "ended", "shadow"],
      weightAdjustment: -72,
    },
  );
});

test("bounded top-k ranking matches a full-sort reference across ties and input order", () => {
  const kindPriority = { root: 0, volume: 1, face: 2, point: 3, line: 4, context: 5 };
  const entries = prepareSearchEntries(Array.from({ length: 240 }, (_, index) => ({
    key: `${["line", "point", "face", "volume"][index % 4]}:${String(index).padStart(3, "0")}`,
    kind: ["line", "point", "face", "volume"][index % 4],
    title: `Shared anchor ${String(239 - index).padStart(3, "0")}`,
    subtitle: "Common match",
    weight: index % 11,
  })).reverse());
  const expected = [...entries].sort((left, right) => (
    right.weight - left.weight
    || kindPriority[left.kind] - kindPriority[right.kind]
    || left.title.localeCompare(right.title)
    || left.key.localeCompare(right.key)
  )).slice(0, 9).map((entry) => entry.key);

  assert.deepEqual(
    rankSearchEntries(entries, "shared", 9).map((entry) => entry.key),
    expected,
  );
});

test("large prepared indexes normalize only the query and avoid a full-result sort", () => {
  const entries = prepareSearchEntries(Array.from({ length: 20_000 }, (_, index) => ({
    key: `point:${index}`,
    kind: "point",
    title: index % 997 === 0 ? `Needle anchor ${index}` : `Memory object ${index}`,
    subtitle: "Modeled observation · current",
    aliases: ["active"],
    weight: index % 17,
  })));
  const originalNormalize = String.prototype.normalize;
  const originalSort = Array.prototype.sort;
  let normalizeCalls = 0;
  let largestSortedArray = 0;
  let matches;

  try {
    String.prototype.normalize = function countedNormalize(...args) {
      normalizeCalls += 1;
      return originalNormalize.apply(this, args);
    };
    Array.prototype.sort = function countedSort(...args) {
      largestSortedArray = Math.max(largestSortedArray, this.length);
      return originalSort.apply(this, args);
    };
    matches = rankSearchEntries(entries, "needle", 9);
  } finally {
    String.prototype.normalize = originalNormalize;
    Array.prototype.sort = originalSort;
  }

  assert.equal(normalizeCalls, 1);
  assert.equal(largestSortedArray, 0);
  assert.equal(matches.length, 9);
  assert.ok(matches.every((entry) => entry.normalizedTitle.includes("needle")));
});

function modelFixture() {
  return {
    points: [{ id: "point-a" }, { id: "point-b" }, { id: "point-c" }],
    lines: [
      { id: "line-ab", source: "point-a", target: "point-b" },
      { id: "line-context", source: "point-a", target: "project-x" },
    ],
    faces: [{ id: "face-work" }, { id: "face-life" }],
    volumes: [{ id: "volume-craft" }],
    root: { id: "root-self" },
  };
}

function layoutFixture() {
  return {
    pointClusterById: new Map([
      ["point-a", "face:face-work"],
      ["point-b", "face:face-work"],
      ["point-c", "face:face-life"],
    ]),
    facePointIds: new Map([
      ["face-work", ["point-a", "point-b"]],
      ["face-life", ["point-c"]],
    ]),
    volumeFaceIds: new Map([["volume-craft", ["face-work"]]]),
    rootVolumeIds: ["volume-craft"],
  };
}

test("focuses a Point's semantic cluster and directly connected relations", () => {
  const focus = focusKeysForSelection(
    modelFixture(),
    layoutFixture(),
    { kind: "point", id: "point-a" },
  );

  assert.deepEqual(focus, new Set([
    "point:point-a",
    "line:line-ab",
    "point:point-b",
    "line:line-context",
    "context:project-x",
    "face:face-work",
  ]));
  assert.equal(focus.has("point:point-c"), false);
});

test("focuses canonical Line endpoints through their rendered entity Points", () => {
  const model = {
    ...modelFixture(),
    lines: [{ id: "line-entity", source: "self", target: "Acme" }],
  };
  const layout = {
    ...layoutFixture(),
    endpointPointIds: new Map([["Acme", "point-a"]]),
  };

  assert.deepEqual(
    focusKeysForSelection(model, layout, { kind: "line", id: "line-entity" }),
    new Set(["line:line-entity", "context:self", "point:point-a"]),
  );
  assert.ok(
    focusKeysForSelection(model, layout, { kind: "point", id: "point-a" })
      .has("line:line-entity"),
  );
});

test("focuses case variants through their shared context node", () => {
  const model = {
    points: [],
    faces: [],
    volumes: [],
    root: null,
    lines: [
      { id: "line-upper", source: "self", target: "Acme Labs" },
      { id: "line-lower", source: "self", target: "acme labs" },
    ],
  };
  const layout = {
    endpointPointIds: new Map(),
    endpointContextIds: new Map([
      ["self", "self"],
      ["Acme Labs", "Acme Labs"],
      ["acme labs", "Acme Labs"],
    ]),
  };

  assert.deepEqual(
    focusKeysForSelection(model, layout, { kind: "context", id: "Acme Labs" }),
    new Set([
      "context:Acme Labs",
      "line:line-upper",
      "line:line-lower",
      "context:self",
    ]),
  );
});

test("focuses only the adjacent hierarchy shell for high-level selections", () => {
  const model = modelFixture();
  const layout = layoutFixture();

  assert.deepEqual(
    focusKeysForSelection(model, layout, { kind: "face", id: "face-work" }),
    new Set(["face:face-work", "point:point-a", "point:point-b", "volume:volume-craft"]),
  );
  assert.deepEqual(
    focusKeysForSelection(model, layout, { kind: "root", id: "root-self" }),
    new Set(["root:root-self", "volume:volume-craft"]),
  );
});
