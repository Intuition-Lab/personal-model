export const SHARE_CARD_WIDTH = 1080;
export const SHARE_CARD_HEIGHT = 1350;
export const SHARE_FILE_NAME = "my-human-card.png";

const CARD_FIELDS = Object.freeze([
  ["optimizesFor", "Optimizes for"],
  ["currentRoot", "Current root"],
  ["decisionStyle", "Decision style"],
  ["aiShould", "AI should"],
  ["neverExpose", "Never expose"],
]);

function clean(value, limit = 132) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
}

function rankedPatterns(model = {}) {
  return [...(model.faces || [])]
    .filter((item) => item?.signature)
    .sort((left, right) => {
      const leftStrength = Number(left.observations || 0) * 2 + Number(left.confidence || 0);
      const rightStrength = Number(right.observations || 0) * 2 + Number(right.confidence || 0);
      return rightStrength - leftStrength || String(left.id || "").localeCompare(String(right.id || ""));
    });
}

/**
 * Build the public projection used by the share card.
 *
 * Only Root/Face signatures are eligible. Receipts, quotes, paths, IDs, source
 * content, and other model metadata never cross this boundary. A future model
 * may provide explicit `root.human_card` copy; current models fall back to the
 * strongest public summaries.
 */
export function humanCard(model = {}) {
  const explicit = model.root?.human_card || {};
  const patterns = rankedPatterns(model);
  return {
    optimizesFor: clean(explicit.optimizes_for || patterns[0]?.signature)
      || "depth over speed",
    currentRoot: clean(explicit.current_root || model.root?.signature)
      || "still forming from local context",
    decisionStyle: clean(explicit.decision_style || patterns[1]?.signature)
      || "evidence first, intuition at the edge",
    aiShould: clean(explicit.ai_should || patterns[2]?.signature)
      || "challenge premature expansion",
    neverExpose: "private source content",
  };
}

function wrappedLines(context, text, maxWidth, maxLines) {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  const lines = [];
  let current = "";
  words.forEach((word) => {
    const next = current ? `${current} ${word}` : word;
    if (context.measureText(next).width <= maxWidth || !current) {
      current = next;
    } else {
      lines.push(current);
      current = word;
    }
  });
  if (current) lines.push(current);
  if (lines.length <= maxLines) return lines;
  const visible = lines.slice(0, maxLines);
  let last = visible[maxLines - 1];
  while (last && context.measureText(`${last}…`).width > maxWidth) {
    last = last.slice(0, -1).trimEnd();
  }
  visible[maxLines - 1] = `${last}…`;
  return visible;
}

function drawWrappedText(context, text, x, y, maxWidth, lineHeight, maxLines) {
  const lines = wrappedLines(context, text, maxWidth, maxLines);
  lines.forEach((line, index) => context.fillText(line, x, y + index * lineHeight));
  return lines.length;
}

export function drawShareCard(context, model = {}) {
  const width = SHARE_CARD_WIDTH;
  const height = SHARE_CARD_HEIGHT;
  const card = humanCard(model);

  context.save();
  context.fillStyle = "#f3f0e9";
  context.fillRect(0, 0, width, height);

  const glow = context.createRadialGradient(900, 120, 10, 900, 120, 660);
  glow.addColorStop(0, "rgba(255, 116, 82, 0.16)");
  glow.addColorStop(0.54, "rgba(255, 116, 82, 0.04)");
  glow.addColorStop(1, "rgba(255, 116, 82, 0)");
  context.fillStyle = glow;
  context.fillRect(0, 0, width, height);

  context.strokeStyle = "rgba(31, 29, 27, 0.16)";
  context.lineWidth = 2;
  context.strokeRect(48, 48, width - 96, height - 96);

  context.fillStyle = "#1f1d1b";
  context.font = "700 86px ui-monospace, 'SFMono-Regular', Menlo, monospace";
  context.fillText("MY HUMAN.md", 96, 166);
  context.fillStyle = "#ef6a4a";
  context.fillRect(98, 202, 84, 8);

  let y = 316;
  CARD_FIELDS.forEach(([key, label], index) => {
    context.fillStyle = index === CARD_FIELDS.length - 1 ? "#cf4f36" : "#6d6861";
    context.font = "650 26px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    context.fillText(`${label}:`, 98, y);

    context.fillStyle = "#1f1d1b";
    context.font = "560 32px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    const count = drawWrappedText(context, card[key], 98, y + 50, width - 196, 40, 2);
    y += 104 + Math.max(0, count - 1) * 40;
  });

  context.strokeStyle = "rgba(31, 29, 27, 0.14)";
  context.beginPath();
  context.moveTo(98, height - 154);
  context.lineTo(width - 98, height - 154);
  context.stroke();
  context.fillStyle = "#6d6861";
  context.font = "560 24px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  context.fillText("Built locally with Persome · Build yours", 98, height - 102);
  context.restore();
}
