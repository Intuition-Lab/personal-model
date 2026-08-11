import assert from "node:assert/strict";
import test from "node:test";

import {
  evidenceBreadcrumb,
  evidenceOverview,
  evidenceRequestPath,
  indexLinePresentations,
  linePresentation,
  modelNodeLabelIndex,
  nodeEvidenceCards,
  nodeHistoryCards,
  relationLabel,
} from "../../resources/model_assets/evidence.mjs";

const oldPoint = {
  id: "point-old",
  content: "The user checked the original evidence.",
  file_name: "project-persome.md",
  receipt: "⟨point-old:project-persome.md⟩",
  status: "superseded",
};
const currentPoint = {
  id: "point-current",
  content: "The user prefers auditable answers.",
  file_name: "user-preferences.md",
  receipt: "⟨point-current:user-preferences.md⟩",
  supersedes: ["point-old"],
  status: "active",
};
const model = { points: [oldPoint, currentPoint] };

test("adds a historical cutoff to evidence drill-down but keeps Now compatible", () => {
  const reference = "⟨point-old:user preferences.md⟩";
  const cutoff = new Date("2026-02-01T10:00:00Z");
  const historical = new URL(evidenceRequestPath(reference, cutoff), "http://localhost/model/");
  const now = new URL(evidenceRequestPath(reference), "http://localhost/model/");

  assert.equal(historical.searchParams.get("ref"), reference);
  assert.equal(historical.searchParams.get("as_of"), "2026-02-01T10:00:00.000Z");
  assert.equal(now.searchParams.get("ref"), reference);
  assert.equal(now.searchParams.has("as_of"), false);
});

test("turns aggregate receipts into human-readable evidence cards", () => {
  const face = {
    id: "face-internal-7",
    source_receipts: [currentPoint.receipt, oldPoint.receipt],
  };
  const cards = nodeEvidenceCards(face, model);

  assert.deepEqual(cards.map((card) => card.label), [
    "The user prefers auditable answers.",
    "The user checked the original evidence.",
  ]);
  assert.ok(cards.every((card) => !card.label.includes("point-")));
  assert.equal(evidenceOverview("face", face, model).title, "2 source observations");
});

test("keeps raw receipts as technical references instead of display labels", () => {
  const [card] = nodeEvidenceCards(
    { source_receipts: ["⟨private-id:project-secret-work.md⟩"] },
    { points: [] },
  );

  assert.equal(card.label, "Project Secret Work");
  assert.equal(card.reference, "⟨private-id:project-secret-work.md⟩");
});

test("labels version history and drill-down breadcrumbs with content", () => {
  const [history] = nodeHistoryCards(currentPoint, model);

  assert.equal(relationLabel(history.relation), "Previous version");
  assert.equal(history.label, "The user checked the original evidence.");
  assert.equal(evidenceBreadcrumb({ label: history.label }), history.label);
});

test("does not reveal a future successor in a historical cutoff", () => {
  const predecessor = {
    ...oldPoint,
    superseded_by: ["point-future"],
  };
  const future = {
    id: "point-future",
    content: "A correction that starts later.",
    valid_from: "2026-03-01T00:00:00Z",
  };

  assert.deepEqual(
    nodeHistoryCards(
      predecessor,
      { points: [predecessor, future] },
      new Date("2026-02-01T00:00:00Z"),
    ),
    [],
  );
  assert.equal(
    nodeHistoryCards(
      predecessor,
      { points: [predecessor, future] },
      new Date("2026-03-01T00:00:00Z"),
    )[0].id,
    "point-future",
  );
});

test("uses successor creation time when valid_from is absent", () => {
  const predecessor = {
    ...oldPoint,
    superseded_by: ["point-created-later"],
  };
  const future = {
    id: "point-created-later",
    content: "A later-created correction.",
    created_at: "2026-03-01T00:00:00Z",
  };

  assert.deepEqual(
    nodeHistoryCards(
      predecessor,
      { points: [predecessor, future] },
      new Date("2026-02-01T00:00:00Z"),
    ),
    [],
  );
  assert.equal(
    nodeHistoryCards(
      predecessor,
      { points: [predecessor, future] },
      new Date("2026-03-01T00:00:00Z"),
    )[0].id,
    "point-created-later",
  );
});

test("omits future Point receipts instead of exposing a generic drill-down", () => {
  const future = {
    id: "point-future-evidence",
    content: "A future correction that must stay hidden.",
    receipt: "⟨point-future-evidence:user-preferences.md⟩",
    created_at: "2026-03-01T00:00:00Z",
  };
  const face = { member_receipts: [future.receipt] };

  assert.deepEqual(
    nodeEvidenceCards(
      face,
      { points: [future] },
      new Date("2026-02-01T00:00:00Z"),
    ),
    [],
  );
  assert.equal(
    nodeEvidenceCards(
      face,
      { points: [future] },
      new Date("2026-03-01T00:00:00Z"),
    )[0].id,
    future.id,
  );
});

test("presents line endpoints without exposing raw node IDs or replacing the predicate", () => {
  const relation = linePresentation({
    id: "relation-private-7",
    kind: "relation",
    label: "maintains",
    predicate: "participates_in",
    source: "point-current",
    target: "private-context-id",
  }, model);

  assert.equal(relation.title, "maintains");
  assert.equal(relation.label, "maintains");
  assert.equal(relation.predicate, "participates_in");
  assert.equal(relation.source, "The user prefers auditable answers.");
  assert.equal(relation.target, "Context node");
  assert.ok(!JSON.stringify(relation).includes("point-current"));
  assert.ok(!JSON.stringify(relation).includes("private-context-id"));
});

test("labels canonical Line endpoints through their rendered entity Point", () => {
  const entityModel = {
    points: [{ id: "entity-acme", content: "Acme Labs" }],
  };
  const labels = modelNodeLabelIndex(
    entityModel,
    new Map([["acme labs", "entity-acme"]]),
  );
  const relation = linePresentation({
    id: "relation-acme",
    kind: "relation",
    predicate: "engaged_with",
    source: "self",
    target: "acme labs",
  }, entityModel, labels);

  assert.equal(relation.source, "You");
  assert.equal(relation.target, "Acme Labs");
});

test("labels folded context variants with their shared display identity", () => {
  const labels = modelNodeLabelIndex(
    { points: [] },
    new Map(),
    new Map([
      ["self", "self"],
      ["ACME LABS", "Acme Labs"],
    ]),
  );
  const relation = linePresentation({
    id: "relation-context",
    kind: "relation",
    predicate: "engaged_with",
    source: "self",
    target: "ACME LABS",
  }, { points: [] }, labels);

  assert.equal(relation.source, "You");
  assert.equal(relation.target, "Acme Labs");
});

test("indexes node labels once and presents each Line once without rescanning model nodes", () => {
  const points = Array.from({ length: 5_000 }, (_, index) => ({
    id: `point-${index}`,
    content: `Point ${index}`,
  }));
  const largeModel = { points };
  const nodeLabels = modelNodeLabelIndex(largeModel);
  points.find = () => {
    throw new Error("pre-indexed Line presentation must not scan the Point array");
  };

  const reads = new Map();
  const countedLine = (values) => new Proxy(values, {
    get(target, property, receiver) {
      if (["id", "kind", "label", "predicate", "source", "target"].includes(property)) {
        reads.set(property, (reads.get(property) || 0) + 1);
      }
      return Reflect.get(target, property, receiver);
    },
  });
  const lines = [
    countedLine({
      id: "line-a",
      kind: "relation",
      label: "supports",
      predicate: "supports",
      source: "point-1",
      target: "point-4999",
    }),
    countedLine({
      id: "line-b",
      kind: "evolution",
      label: "",
      predicate: "supersedes",
      source: "point-2",
      target: "point-3",
    }),
  ];

  const presentations = indexLinePresentations(lines, largeModel, nodeLabels);

  assert.equal(presentations.get("line-a").option, "supports: Point 1 → Point 4999");
  assert.equal(presentations.get("line-b").source, "Point 2");
  assert.deepEqual(Object.fromEntries(reads), {
    id: 2,
    kind: 2,
    predicate: 2,
    label: 2,
    source: 2,
    target: 2,
  });
});
