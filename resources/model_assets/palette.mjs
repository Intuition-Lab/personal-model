/**
 * Shared visual palette for the localhost model viewer and constellation card.
 *
 * The original semantic hues stay recognizable while their saturation and
 * luminance are restrained for a dense, deep-space canvas.
 */
export const MODEL_PALETTE = Object.freeze({
  canvas: "#090b16",
  surface: "#111425",
  surfaceRaised: "#181c33",
  text: "#eceaf2",
  muted: "#aaa6b8",
  dim: "#878299",
  point: "#72d8c0",
  line: "#9f7a52",
  lineUi: "#c5a16f",
  face: "#e47bc9",
  volume: "#8298ee",
  root: "#ee809b",
  focus: "#aa96ef",
  entity: "#7f8196",
  guide: "#34374d",
  guideUi: "#898da4",
  relation: "#68c69c",
  historical: "#625f73",
  success: "#59c995",
  warning: "#d5b76a",
  error: "#eb767f",
});

function colorNumber(value) {
  return Number.parseInt(value.slice(1), 16);
}

export const MODEL_COLORS = Object.freeze({
  points: colorNumber(MODEL_PALETTE.point),
  lines: colorNumber(MODEL_PALETTE.line),
  faces: colorNumber(MODEL_PALETTE.face),
  volumes: colorNumber(MODEL_PALETTE.volume),
  root: colorNumber(MODEL_PALETTE.root),
  focus: colorNumber(MODEL_PALETTE.focus),
  context: colorNumber(MODEL_PALETTE.entity),
  hierarchy: colorNumber(MODEL_PALETTE.guide),
  relation: colorNumber(MODEL_PALETTE.relation),
  historical: colorNumber(MODEL_PALETTE.historical),
  canvas: colorNumber(MODEL_PALETTE.canvas),
  text: colorNumber(MODEL_PALETTE.text),
});

export function colorWithAlpha(value, alpha) {
  const number = colorNumber(value);
  const red = (number >> 16) & 255;
  const green = (number >> 8) & 255;
  const blue = number & 255;
  const safeAlpha = Math.max(0, Math.min(1, Number(alpha) || 0));
  return `rgba(${red}, ${green}, ${blue}, ${safeAlpha})`;
}
