import assert from "node:assert/strict";
import test from "node:test";

import {
  CONSTELLATION_CARD_HEIGHT,
  CONSTELLATION_CARD_WIDTH,
  CONSTELLATION_FILE_NAME,
  HUMAN_CARD_HEIGHT,
  HUMAN_CARD_WIDTH,
  HUMAN_CARD_FILE_NAME,
  SHARE_TEXTS,
  SHARE_URL,
  buildXIntentUrl,
  drawConstellationCard,
  drawHumanCard,
  humanCard,
  pickShareText,
  shareNarrative,
  shareStats,
} from "../../resources/model_assets/share.mjs";

test("keeps the HUMAN.md Card portrait-sized and portable", () => {
  assert.equal(HUMAN_CARD_WIDTH, 1080);
  assert.equal(HUMAN_CARD_HEIGHT, 1350);
  assert.equal(HUMAN_CARD_FILE_NAME, "my-human-card.png");
});

test("keeps the X share artifact constellation-sized and distinct", () => {
  assert.equal(CONSTELLATION_CARD_WIDTH, 1200);
  assert.equal(CONSTELLATION_CARD_HEIGHT, 675);
  assert.equal(CONSTELLATION_FILE_NAME, "my-persome-constellation.png");
  assert.notEqual(CONSTELLATION_FILE_NAME, HUMAN_CARD_FILE_NAME);
});

test("uses only signatures from the server share projection", () => {
  const card = humanCard({
    root: {
      signature: "turning personal context into agency",
      human_card: {
        current_root: "raw owner-only copy must be ignored",
      },
    },
  });

  assert.deepEqual(card, {
    optimizesFor: "depth over speed",
    currentRoot: "turning personal context into agency",
    decisionStyle: "evidence first, intuition at the edge",
    aiShould: "challenge premature expansion",
    neverExpose: "private source content",
  });
});

const EXPECTED_SHARE_TEXTS = [
  [
    "I let @PersonalModel_ observe how I use my Mac, and apparently this is what I look like 😳",
    "",
    "#PersonalModel @PersonalModel_",
  ].join("\n"),
  [
    "I let my @PersonalModel_ learn from how I use my Mac. I didn’t expect this is how it sees me 😳",
    "",
    "#PersonalModel @PersonalModel_",
  ].join("\n"),
  [
    "I let @PersonalModel_ observe how I use my Mac, and this is the model it built 🤔",
    "",
    "#PersonalModel @PersonalModel_",
  ].join("\n"),
];

test("keeps the three approved X share variants verbatim", () => {
  assert.deepEqual(SHARE_TEXTS, EXPECTED_SHARE_TEXTS);
});

test("selects each X share variant from an equal third of the random range", () => {
  assert.equal(pickShareText(() => 0), EXPECTED_SHARE_TEXTS[0]);
  assert.equal(pickShareText(() => (1 / 3) - Number.EPSILON), EXPECTED_SHARE_TEXTS[0]);
  assert.equal(pickShareText(() => 1 / 3), EXPECTED_SHARE_TEXTS[1]);
  assert.equal(pickShareText(() => (2 / 3) - Number.EPSILON), EXPECTED_SHARE_TEXTS[1]);
  assert.equal(pickShareText(() => 2 / 3), EXPECTED_SHARE_TEXTS[2]);
  assert.equal(pickShareText(() => 0.999999), EXPECTED_SHARE_TEXTS[2]);
});

test("retains the approved X composer handoff for the constellation", () => {
  const intent = new URL(buildXIntentUrl({ random: () => 0 }));

  assert.equal(SHARE_TEXTS.length, 3);
  assert.equal(intent.origin, "https://x.com");
  assert.equal(intent.pathname, "/intent/tweet");
  assert.equal(intent.searchParams.get("text"), SHARE_TEXTS[0]);
  assert.equal(intent.searchParams.get("url"), SHARE_URL);
  assert.ok(!intent.href.includes("localhost"));
});

test("keeps aggregate constellation counts separate from private source data", () => {
  assert.deepEqual(
    shareStats({
      stats: {
        points: 424,
        evolution_lines: 120,
        relation_lines: 26,
        faces: 12,
        volumes: 4,
        roots: 1,
      },
    }),
    [
      ["POINTS", 424],
      ["LINES", 146],
      ["FACES", 12],
      ["VOLUMES", 4],
      ["ROOT", 1],
    ],
  );
});

test("selects a bounded constellation narrative and highest-level patterns", () => {
  const narrative = shareNarrative({
    root: {
      signature: "A focused systems builder who turns context into auditable work.",
      receipts: ["private-root-receipt"],
    },
    volumes: [
      { id: "volume-low", signature: "Lower-confidence structure.", confidence: 0.7 },
      { id: "volume-high", signature: "Trusted personal AI connects context and correction.", confidence: 0.96 },
    ],
    faces: [
      { id: "face-high", signature: "Focused work starts with a small plan.", confidence: 0.99 },
      { id: "face-second", signature: "Research claims are checked against evidence.", confidence: 0.91 },
    ],
  });

  assert.equal(
    narrative.root,
    "A focused systems builder who turns context into auditable work.",
  );
  assert.deepEqual(narrative.highlights, [
    { kind: "VOLUME", text: "Trusted personal AI connects context and correction." },
    { kind: "VOLUME", text: "Lower-confidence structure." },
    { kind: "FACE", text: "Focused work starts with a small plan." },
  ]);
  assert.ok(!JSON.stringify(narrative).includes("private-root-receipt"));
});

function recordingContext() {
  const drawImages = [];
  const gradient = { addColorStop() {} };
  const context = {
    drawImages,
    measureText(value) {
      return { width: String(value).length * 7 };
    },
    createLinearGradient() {
      return gradient;
    },
    createRadialGradient() {
      return gradient;
    },
    drawImage(...args) {
      drawImages.push(args);
    },
  };
  return new Proxy(context, {
    get(target, property) {
      if (property in target) return target[property];
      return () => {};
    },
  });
}

test("draws the live WebGL canvas into the X constellation artifact only", () => {
  const source = { width: 1600, height: 900 };
  const constellationContext = recordingContext();
  drawConstellationCard(constellationContext, source, {
    root: { signature: "A test constellation." },
  });

  assert.equal(constellationContext.drawImages.length, 1);
  assert.equal(constellationContext.drawImages[0][0], source);

  const humanContext = recordingContext();
  drawHumanCard(humanContext, { root: { signature: "A test HUMAN.md Card." } });
  assert.equal(humanContext.drawImages.length, 0);
});

test("falls back to bounded high-level summaries without leaking sources", () => {
  const card = humanCard({
    root: {
      id: "private-root-id",
      signature: "Turning personal context into agency.",
      source_receipts: ["receipt-secret"],
      path: "/Users/alice/private/root.json",
    },
    faces: [
      { id: "face-b", signature: "Evidence first.", observations: 4, quote: "private quote" },
      { id: "face-a", signature: "Depth over speed.", observations: 9, source_receipts: ["secret"] },
      { id: "face-c", signature: "Challenge premature expansion.", observations: 2 },
    ],
    points: [{ content: "PRIVATE SOURCE CONTENT" }],
    receipts: [{ quote: "PRIVATE RECEIPT" }],
  });

  assert.deepEqual(card, {
    optimizesFor: "Depth over speed.",
    currentRoot: "Turning personal context into agency.",
    decisionStyle: "Evidence first.",
    aiShould: "Challenge premature expansion.",
    neverExpose: "private source content",
  });
  const serialized = JSON.stringify(card);
  assert.ok(!serialized.includes("private-root-id"));
  assert.ok(!serialized.includes("receipt-secret"));
  assert.ok(!serialized.includes("/Users/alice"));
  assert.ok(!serialized.includes("PRIVATE"));
});

test("uses safe defaults while the model is still sparse", () => {
  assert.deepEqual(humanCard({}), {
    optimizesFor: "depth over speed",
    currentRoot: "still forming from local context",
    decisionStyle: "evidence first, intuition at the edge",
    aiShould: "challenge premature expansion",
    neverExpose: "private source content",
  });
});
