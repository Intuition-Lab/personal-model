import assert from "node:assert/strict";
import test from "node:test";

import {
  SHARE_CARD_HEIGHT,
  SHARE_CARD_WIDTH,
  SHARE_FILE_NAME,
  SHARE_HASHTAGS,
  SHARE_TEXT,
  SHARE_URL,
  buildXIntentUrl,
  shareStats,
} from "../../resources/model_assets/share.mjs";

test("builds an X composer URL with the standard copy, destination, and tags", () => {
  const intent = new URL(buildXIntentUrl());

  assert.equal(intent.origin, "https://x.com");
  assert.equal(intent.pathname, "/intent/tweet");
  assert.equal(intent.searchParams.get("text"), SHARE_TEXT);
  assert.equal(intent.searchParams.get("url"), SHARE_URL);
  assert.equal(intent.searchParams.get("hashtags"), SHARE_HASHTAGS.join(","));
  assert.ok(!intent.href.includes("localhost"));
  assert.ok(!intent.href.includes("/model/"));
});

test("keeps the share artifact fixed, portable, and aggregate-only", () => {
  assert.equal(SHARE_CARD_WIDTH, 1200);
  assert.equal(SHARE_CARD_HEIGHT, 675);
  assert.equal(SHARE_FILE_NAME, "my-persome-constellation.png");

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
