import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  MODEL_COLORS,
  MODEL_PALETTE,
  colorWithAlpha,
} from "../../resources/model_assets/palette.mjs";

const css = await readFile(
  new URL("../../resources/model_assets/viewer.css", import.meta.url),
  "utf8",
);

function cssToken(name) {
  return css.match(new RegExp(`--${name}:\\s*([^;]+);`))?.[1]?.trim();
}

function channels(value) {
  const number = Number.parseInt(value.slice(1), 16);
  return [(number >> 16) & 255, (number >> 8) & 255, number & 255];
}

function luminance(value) {
  const linear = channels(value).map((channel) => {
    const normalized = channel / 255;
    return normalized <= 0.04045
      ? normalized / 12.92
      : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrast(left, right) {
  const bright = Math.max(luminance(left), luminance(right));
  const dark = Math.min(luminance(left), luminance(right));
  return (bright + 0.05) / (dark + 0.05);
}

function composite(foreground, background, alpha) {
  const front = channels(foreground);
  const back = channels(background);
  const mixed = front.map((channel, index) =>
    Math.round(channel * alpha + back[index] * (1 - alpha)),
  );
  return `#${mixed.map((channel) => channel.toString(16).padStart(2, "0")).join("")}`;
}

function chroma(value) {
  const values = channels(value);
  return Math.max(...values) - Math.min(...values);
}

test("keeps CSS, WebGL, and export palette roles aligned", () => {
  assert.equal(cssToken("bg"), MODEL_PALETTE.canvas);
  assert.equal(cssToken("surface"), colorWithAlpha(MODEL_PALETTE.surface, 0.82));
  assert.equal(cssToken("surface-soft"), colorWithAlpha(MODEL_PALETTE.surfaceRaised, 0.64));
  assert.equal(cssToken("text"), MODEL_PALETTE.text);
  assert.equal(cssToken("muted"), MODEL_PALETTE.muted);
  assert.equal(cssToken("dim"), MODEL_PALETTE.dim);
  assert.equal(cssToken("point"), MODEL_PALETTE.point);
  assert.equal(cssToken("line"), MODEL_PALETTE.lineUi);
  assert.equal(cssToken("line-stroke"), MODEL_PALETTE.line);
  assert.equal(cssToken("face"), MODEL_PALETTE.face);
  assert.equal(cssToken("volume"), MODEL_PALETTE.volume);
  assert.equal(cssToken("root"), MODEL_PALETTE.root);
  assert.equal(cssToken("focus"), MODEL_PALETTE.focus);
  assert.equal(cssToken("entity"), MODEL_PALETTE.entity);
  assert.equal(cssToken("guide"), MODEL_PALETTE.guide);
  assert.equal(cssToken("guide-ui"), MODEL_PALETTE.guideUi);
  assert.equal(cssToken("relation"), MODEL_PALETTE.relation);
  assert.equal(cssToken("historical"), MODEL_PALETTE.historical);
  assert.equal(cssToken("success"), MODEL_PALETTE.success);
  assert.equal(cssToken("warning"), MODEL_PALETTE.warning);
  assert.equal(cssToken("danger"), MODEL_PALETTE.error);

  assert.equal(MODEL_COLORS.points, Number.parseInt(MODEL_PALETTE.point.slice(1), 16));
  assert.equal(MODEL_COLORS.lines, Number.parseInt(MODEL_PALETTE.line.slice(1), 16));
  assert.equal(MODEL_COLORS.faces, Number.parseInt(MODEL_PALETTE.face.slice(1), 16));
  assert.equal(MODEL_COLORS.volumes, Number.parseInt(MODEL_PALETTE.volume.slice(1), 16));
  assert.equal(MODEL_COLORS.root, Number.parseInt(MODEL_PALETTE.root.slice(1), 16));
});

test("keeps the original semantic hues restrained on a deep-space canvas", () => {
  assert.equal(MODEL_PALETTE.canvas, "#090b16");
  assert.equal(MODEL_PALETTE.point, "#72d8c0");
  assert.equal(MODEL_PALETTE.line, "#9f7a52");
  assert.equal(MODEL_PALETTE.face, "#e47bc9");
  assert.equal(MODEL_PALETTE.volume, "#8298ee");
  assert.equal(MODEL_PALETTE.root, "#ff718f");
  assert.ok(chroma(MODEL_PALETTE.canvas) >= 10, "canvas should remain blue-black, not charcoal");
  assert.ok(chroma(MODEL_PALETTE.point) >= 60, "dense points should retain their mint hue");
  assert.ok(chroma(MODEL_PALETTE.line) >= 50, "dense lines should retain their warm amber hue");
  assert.ok(chroma(MODEL_PALETTE.root) >= 120, "the Root should stay clearly coral, not dusty pink");
  assert.notEqual(MODEL_PALETTE.face, MODEL_PALETTE.volume);
  assert.notEqual(MODEL_PALETTE.volume, MODEL_PALETTE.root);
  assert.notEqual(MODEL_PALETTE.root, MODEL_PALETTE.face);
  assert.ok(contrast(MODEL_PALETTE.focus, MODEL_PALETTE.canvas) >= 3);

  assert.ok(
    contrast(composite(MODEL_PALETTE.line, MODEL_PALETTE.canvas, 0.5), MODEL_PALETTE.canvas) >= 1.6,
    "same-cluster evolution lines must remain visible after alpha compositing",
  );
  assert.ok(
    contrast(composite(MODEL_PALETTE.line, MODEL_PALETTE.canvas, 0.5), MODEL_PALETTE.canvas) < 3,
    "same-cluster evolution lines should not overpower dense points",
  );
  assert.ok(
    contrast(composite(MODEL_PALETTE.line, MODEL_PALETTE.canvas, 0.2), MODEL_PALETTE.canvas) >= 1.15,
    "cross-cluster evolution lines must remain subtly visible after alpha compositing",
  );
  assert.ok(
    contrast(composite(MODEL_PALETTE.line, MODEL_PALETTE.canvas, 0.2), MODEL_PALETTE.canvas) < 1.5,
    "cross-cluster evolution lines should remain atmospheric",
  );
  assert.ok(
    contrast(composite(MODEL_PALETTE.relation, MODEL_PALETTE.canvas, 0.36), MODEL_PALETTE.canvas) >= 2.1,
    "semantic relation lines must remain readable after alpha compositing",
  );
  assert.ok(
    contrast(composite(MODEL_PALETTE.relation, MODEL_PALETTE.canvas, 0.36), MODEL_PALETTE.canvas) < 3.2,
    "semantic relation lines should not dominate the graph",
  );
});

test("keeps essential small text and controls legible on blue-black surfaces", () => {
  for (const role of ["text", "muted", "dim"]) {
    assert.ok(
      contrast(MODEL_PALETTE[role], MODEL_PALETTE.surface) >= 4.5,
      `${role} must remain readable on the common panel surface`,
    );
  }
  for (const role of ["point", "lineUi", "face", "volume", "root", "focus"]) {
    assert.ok(
      contrast(MODEL_PALETTE[role], MODEL_PALETTE.canvas) >= 3,
      `${role} must remain visible on the graph canvas`,
    );
  }
  assert.ok(contrast(MODEL_PALETTE.focus, MODEL_PALETTE.surface) >= 3);
  for (const role of ["entity", "guideUi"]) {
    assert.ok(
      contrast(MODEL_PALETTE[role], MODEL_PALETTE.surface) >= 3,
      `${role} legend cues must remain visible on the common panel surface`,
    );
  }
});

test("builds bounded rgba strings for canvas exports", () => {
  assert.equal(colorWithAlpha("#123456", 0.14), "rgba(18, 52, 86, 0.14)");
  assert.equal(colorWithAlpha("#123456", 9), "rgba(18, 52, 86, 1)");
  assert.equal(colorWithAlpha("#123456", -1), "rgba(18, 52, 86, 0)");
});
