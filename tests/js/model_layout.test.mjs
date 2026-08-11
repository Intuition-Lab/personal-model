import assert from "node:assert/strict";
import test from "node:test";

import {
  coalesceProjectedLines,
  computeClusterLayout,
  layoutMath,
  pickScreenTarget,
  zoomMath,
} from "../../resources/model_assets/layout.mjs";

function point(index, file = "project-runtime.md") {
  const id = `point-${String(index).padStart(3, "0")}`;
  return {
    id,
    receipt: `receipt:${id}`,
    file_name: file,
    created_at: `2026-07-01T10:${String(index).padStart(2, "0")}:00Z`,
    is_latest: true,
    status: "active",
  };
}

function face(id, members, createdAt) {
  const receipts = members.map((index) => `receipt:point-${String(index).padStart(3, "0")}`);
  return {
    id,
    member_receipts: receipts,
    source_receipts: receipts,
    observations: 4,
    confidence: 0.9,
    created_at: createdAt,
  };
}

function fixture(extraPoints = []) {
  const points = [
    ...Array.from({ length: 30 }, (_, index) => point(index)),
    ...Array.from({ length: 6 }, (_, index) => point(index + 30, "event-unmodeled.md")),
    ...extraPoints,
  ];
  const faces = [
    face("face-focus", [0, 1, 2, 3, 4, 5], "2026-07-01T11:00:00Z"),
    face("face-review", [10, 11, 12, 13, 14, 15], "2026-07-01T11:01:00Z"),
    face("face-collaboration", [20, 21, 22, 23, 24, 25], "2026-07-01T11:02:00Z"),
  ];
  const volumes = [
    {
      id: "volume-work",
      members: ["internal-focus", "internal-review"],
      source_receipts: [...faces[0].source_receipts, ...faces[1].source_receipts],
      observations: 5,
      confidence: 0.92,
      created_at: "2026-07-01T12:00:00Z",
    },
    {
      id: "volume-people",
      members: ["internal-collaboration"],
      source_receipts: [...faces[2].source_receipts],
      observations: 3,
      confidence: 0.88,
      created_at: "2026-07-01T12:01:00Z",
    },
  ];
  return {
    points,
    lines: [
      { id: "evolution-1", kind: "evolution", source: "point-006", target: "point-000" },
      { id: "evolution-2", kind: "evolution", source: "point-016", target: "point-010" },
      { id: "relation-1", kind: "relation", source: "self", target: "collaborator" },
    ],
    faces,
    volumes,
    root: { id: "root", members: volumes.map((volume) => volume.id) },
  };
}

test("lays the hierarchy out as centered, three-dimensional clusters", () => {
  const model = fixture();
  const layout = computeClusterLayout(model);
  const root = layout.positions.get("root");

  assert.deepEqual(root, [0, 0, 0]);
  assert.equal(layout.diagnostics.rootAtCenter, true);
  assert.ok(layout.diagnostics.averageRadius.volumes > 2.35);
  assert.ok(layout.diagnostics.averageRadius.faces > layout.diagnostics.averageRadius.volumes);
  assert.ok(layout.diagnostics.pointYSpread > 0.75);
  assert.equal(layout.diagnostics.directPoints, 18);
  assert.equal(layout.diagnostics.volumeMembershipEdges, 3);
  assert.ok(layout.diagnostics.sourceClusterPoints >= 6);
  assert.ok(layout.contextIds.includes("self"));
  model.volumes.forEach((volume) => {
    assert.ok(layoutMath.distance(layout.positions.get(volume.id), root) >= 2.4);
  });
  layout.contextIds.forEach((id) => {
    assert.ok(layoutMath.distance(layout.positions.get(id), root) > 1.09);
  });
  assert.ok(layoutMath.distance(layout.positions.get("face-focus"), root) > 3);
});

test("keeps existing coordinates stable as later evidence is appended", () => {
  const initialModel = fixture();
  const initial = computeClusterLayout(initialModel);
  const additions = Array.from({ length: 8 }, (_, index) => ({
    ...point(index + 40),
    created_at: `2026-07-02T10:${String(index).padStart(2, "0")}:00Z`,
  }));
  const grown = computeClusterLayout(fixture(additions));

  initialModel.points.forEach((item) => {
    assert.deepEqual(grown.positions.get(item.id), initial.positions.get(item.id));
  });
  initialModel.faces.forEach((item) => {
    assert.deepEqual(grown.positions.get(item.id), initial.positions.get(item.id));
  });
  initialModel.volumes.forEach((item) => {
    assert.deepEqual(grown.positions.get(item.id), initial.positions.get(item.id));
  });
});

test("keeps a point-only degraded model close to the center", () => {
  const layout = computeClusterLayout({
    points: [point(0, "event-first.md")],
    lines: [],
    faces: [],
    volumes: [],
    root: null,
  });

  assert.equal(layout.diagnostics.sourceClusterPoints, 1);
  assert.ok(layout.diagnostics.averageRadius.points < 2);
  assert.ok(layout.diagnostics.bounds.radius < 3);
});

test("does not widen a rootless degraded hierarchy", () => {
  const model = { ...fixture(), root: null };
  const layout = computeClusterLayout(model);
  const center = [0, 0, 0];

  model.volumes.forEach((volume) => {
    assert.ok(layoutMath.distance(layout.positions.get(volume.id), center) < 2.3);
  });
  layout.contextIds.forEach((id) => {
    assert.ok(layoutMath.distance(layout.positions.get(id), center) < 1.41);
  });
});

test("reuses a canonical entity Point instead of drawing a duplicate context node", () => {
  const entity = {
    ...point(0, "org-acme.md"),
    id: "entity-acme",
    content: " Acme\u00a0Labs ",
    tags: "entity",
  };
  const personEntity = {
    ...point(1, "person-alex.md"),
    id: "entity-alex",
    content: "Alex",
    tags: "person-entity",
  };
  const layout = computeClusterLayout({
    points: [entity, personEntity],
    lines: [
      { id: "relation-acme", kind: "relation", source: "self", target: "acme labs" },
      { id: "relation-alex", kind: "relation", source: "self", target: "Alex" },
    ],
    faces: [],
    volumes: [],
    root: null,
  });

  assert.equal(layout.endpointPointIds.get("acme labs"), entity.id);
  assert.equal(layout.positions.has("acme labs"), false);
  assert.ok(layout.positions.has(entity.id));
  assert.equal(layout.contextIds.includes("acme labs"), false);
  assert.equal(layout.contextIds.includes("self"), true);
  assert.equal(layout.endpointPointIds.get("Alex"), personEntity.id);
  assert.equal(layout.contextIds.includes("Alex"), false);
  assert.equal(layout.diagnostics.resolvedEntityEndpoints, 2);
});

test("does not guess when multiple live entity Points claim the same identity", () => {
  const duplicate = (id) => ({
    ...point(0, `person-${id}.md`),
    id,
    content: "Alex",
    tags: "entity",
  });
  const layout = computeClusterLayout({
    points: [duplicate("entity-a"), duplicate("entity-b")],
    lines: [{ id: "relation-alex", kind: "relation", source: "self", target: "Alex" }],
    faces: [],
    volumes: [],
    root: null,
  });

  assert.equal(layout.endpointPointIds.has("Alex"), false);
  assert.equal(layout.contextIds.includes("Alex"), true);
});

test("folds case and width variants into one context node without merging Lines", () => {
  const layout = computeClusterLayout({
    points: [],
    lines: [
      { id: "relation-upper", kind: "relation", source: "self", target: "Acme Labs" },
      { id: "relation-lower", kind: "relation", source: "SELF", target: "acme labs" },
    ],
    faces: [],
    volumes: [],
    root: null,
  });

  assert.deepEqual(layout.contextIds, ["Acme Labs", "self"]);
  assert.equal(layout.endpointContextIds.get("Acme Labs"), "Acme Labs");
  assert.equal(layout.endpointContextIds.get("acme labs"), "Acme Labs");
  assert.equal(layout.endpointContextIds.get("self"), "self");
  assert.equal(layout.endpointContextIds.get("SELF"), "self");
  assert.equal(layout.positions.has("acme labs"), false);
  assert.equal(layout.diagnostics.contextNodes, 2);
});

test("renders one projected Line while retaining every legacy variant for audit", () => {
  const lines = [
    {
      id: "relation-upper",
      kind: "relation",
      predicate: "engaged_with",
      source: "self",
      target: "Acme Labs",
    },
    {
      id: "relation-lower",
      kind: "relation",
      predicate: "engaged_with",
      source: "SELF",
      target: "acme labs",
    },
    {
      id: "relation-opposite",
      kind: "relation",
      predicate: "engaged_with",
      polarity: "-",
      source: "self",
      target: "acme labs",
    },
    { id: "evolution", kind: "evolution", source: "old", target: "new" },
  ];
  const layout = computeClusterLayout({
    points: [],
    lines,
    faces: [],
    volumes: [],
    root: null,
  });

  const projection = coalesceProjectedLines(lines, layout);

  assert.deepEqual(
    projection.lines.map((line) => line.id),
    ["evolution", "relation-lower", "relation-opposite"],
  );
  assert.deepEqual(
    projection.membersByRepresentative.get("relation-lower"),
    ["relation-lower", "relation-upper"],
  );
});

test("renders a symmetric knows relationship once in either stored direction", () => {
  const lines = [
    {
      id: "knows-forward",
      kind: "relation",
      predicate: "knows",
      source: "Alice",
      target: "Bob",
    },
    {
      id: "knows-reverse",
      kind: "relation",
      predicate: "knows",
      source: "bob",
      target: "alice",
    },
  ];
  const layout = computeClusterLayout({
    points: [],
    lines,
    faces: [],
    volumes: [],
    root: null,
  });

  const projection = coalesceProjectedLines(lines, layout);

  assert.equal(projection.lines.length, 1);
  assert.deepEqual(
    projection.membersByRepresentative.get(projection.lines[0].id),
    ["knows-forward", "knows-reverse"],
  );
});

test("uses the strongest projected Line as the visible representative", () => {
  const lines = [
    {
      id: "relation-early-weak",
      kind: "relation",
      predicate: "engaged_with",
      source: "self",
      target: "Acme",
      observations: 1,
      confidence: 0.6,
      created_at: "2026-01-01T00:00:00Z",
    },
    {
      id: "relation-later-strong",
      kind: "relation",
      predicate: "engaged_with",
      source: "SELF",
      target: "acme",
      observations: 4,
      confidence: 0.9,
      quote: "A grounded observation",
      created_at: "2026-02-01T00:00:00Z",
    },
  ];
  const layout = computeClusterLayout({
    points: [], lines, faces: [], volumes: [], root: null,
  });

  const projection = coalesceProjectedLines(lines, layout);

  assert.deepEqual(projection.lines.map((line) => line.id), ["relation-later-strong"]);
  assert.deepEqual(
    projection.membersByRepresentative.get("relation-later-strong"),
    ["relation-early-weak", "relation-later-strong"],
  );
});

test("keeps self and non-entity labels as context endpoints", () => {
  const layout = computeClusterLayout({
    points: [
      { ...point(0), id: "entity-self", content: "self", tags: "entity" },
      { ...point(1), id: "fact-acme", content: "Acme", tags: "fact" },
    ],
    lines: [{ id: "relation", kind: "relation", source: "self", target: "Acme" }],
    faces: [],
    volumes: [],
    root: null,
  });

  assert.deepEqual(layout.contextIds, ["Acme", "self"]);
  assert.equal(layout.endpointPointIds.size, 0);
});

test("steps fitted zoom predictably through rapid actions and clamps its range", () => {
  assert.equal(zoomMath.percentForDistance(12, 12), 100);
  assert.equal(zoomMath.percentForDistance(12, 24), 50);
  assert.equal(zoomMath.percentForDistance(12, 3), 400);
  assert.equal(zoomMath.percentForDistance(12, 120), 50);
  assert.equal(zoomMath.percentForDistance(12, 0.3), 400);

  const firstTap = zoomMath.nextPercent(100, 1);
  const rapidSecondTap = zoomMath.nextPercent(firstTap, 1);
  assert.equal(firstTap, 125);
  assert.equal(rapidSecondTap, 150);
  assert.equal(zoomMath.nextPercent(113, -1), 100);
  assert.equal(zoomMath.nextPercent(50, -1), 50);
  assert.equal(zoomMath.nextPercent(400, 1), 400);
});

test("gives small nodes a stable screen-space hit target ahead of crossing lines", () => {
  const pointTarget = { id: "point" };
  const lineTarget = { id: "line" };
  const hit = pickScreenTarget(
    { x: 111, y: 100 },
    [{ x: 100, y: 100, depth: 0.4, target: pointTarget }],
    [{
      start: { x: 80, y: 100 },
      end: { x: 140, y: 100 },
      depth: 0.2,
      target: lineTarget,
    }],
  );

  assert.equal(hit, pointTarget);
});

test("selects relationship lines near their rendered segment without widening raycasts", () => {
  const lineTarget = { id: "relation" };
  const hit = pickScreenTarget(
    { x: 70, y: 56 },
    [],
    [{
      start: { x: 20, y: 50 },
      end: { x: 120, y: 50 },
      depth: 0.3,
      target: lineTarget,
    }],
  );
  const miss = pickScreenTarget(
    { x: 70, y: 62 },
    [],
    [{
      start: { x: 20, y: 50 },
      end: { x: 120, y: 50 },
      depth: 0.3,
      target: lineTarget,
    }],
  );

  assert.equal(hit, lineTarget);
  assert.equal(miss, null);
});

test("chooses the nearest projected node when hit targets overlap", () => {
  const near = { id: "near" };
  const far = { id: "far" };
  const hit = pickScreenTarget(
    { x: 100, y: 100 },
    [
      { x: 100, y: 100, depth: 0.7, target: far },
      { x: 100, y: 100, depth: 0.1, target: near },
    ],
  );

  assert.equal(hit, near);
});
