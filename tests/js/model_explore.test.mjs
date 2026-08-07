import assert from "node:assert/strict";
import test from "node:test";

import {
  focusKeysForSelection,
  handleSearchShortcut,
  pointerUpOutcome,
  rankSearchEntries,
  reconcileSceneSelection,
  recoverInvalidSceneSelection,
  shouldHandleModelGesture,
} from "../../resources/model_assets/explore.mjs";

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

test("zooms through the legend while preserving real scrolling surfaces", () => {
  const targetIn = (className) => ({
    closest(selector) {
      return selector.split(", ").includes(className) ? { className } : null;
    },
  });

  assert.equal(shouldHandleModelGesture(targetIn(".legend")), true);
  assert.equal(shouldHandleModelGesture(targetIn(".detail")), false);
  assert.equal(shouldHandleModelGesture(targetIn(".line-explorer")), false);
  assert.equal(shouldHandleModelGesture(targetIn(".search-panel")), false);
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
