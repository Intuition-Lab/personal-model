/**
 * Shared visual palette for the localhost model viewer and constellation card.
 *
 * The dense evidence field stays neutral. Chroma is reserved for promoted
 * structure and focus so the graph remains readable at real-model scale.
 */
export const MODEL_PALETTE = Object.freeze({
  canvas: "#1c1c1c",
  surface: "#232323",
  surfaceRaised: "#282828",
  text: "#dadada",
  muted: "#a6a6ae",
  dim: "#8a8a94",
  point: "#b3b3b3",
  line: "#666666",
  lineUi: "#92929b",
  face: "#53dfdd",
  volume: "#a882ff",
  root: "#fa99cd",
  focus: "#a68af9",
  entity: "#76767f",
  guide: "#3f3f3f",
  guideUi: "#8a8a94",
  relation: "#44cf6e",
  historical: "#666666",
  success: "#44cf6e",
  warning: "#e0de71",
  error: "#fb7378",
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
