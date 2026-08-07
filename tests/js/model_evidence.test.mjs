import assert from "node:assert/strict";
import test from "node:test";

import {
  evidenceBreadcrumb,
  evidenceOverview,
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
