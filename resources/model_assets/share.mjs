export const HUMAN_CARD_WIDTH = 1080;
export const HUMAN_CARD_HEIGHT = 1350;
export const HUMAN_CARD_FILE_NAME = "my-human-card.png";
export const CONSTELLATION_CARD_WIDTH = 1200;
export const CONSTELLATION_CARD_HEIGHT = 675;
export const CONSTELLATION_FILE_NAME = "my-persome-constellation.png";
export const SHARE_URL = "https://github.com/Intuition-Lab/personal-model";
export const SHARE_TEXTS = Object.freeze([
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
]);

export function pickShareText(random = Math.random) {
  const index = Math.floor(random() * SHARE_TEXTS.length);
  return SHARE_TEXTS[Math.max(0, Math.min(index, SHARE_TEXTS.length - 1))];
}

export function buildXIntentUrl({
  text,
  url = SHARE_URL,
  random = Math.random,
} = {}) {
  const params = new URLSearchParams();
  params.set("text", text ?? pickShareText(random));
  params.set("url", url);
  return `https://x.com/intent/tweet?${params.toString()}`;
}

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
 * Only summaries from the server's canonically scrubbed `/model/share-card`
 * projection are eligible. The raw owner graph must never be passed here.
 */
export function humanCard(model = {}) {
  const patterns = rankedPatterns(model);
  return {
    optimizesFor: clean(patterns[0]?.signature) || "depth over speed",
    currentRoot: clean(model.root?.signature) || "still forming from local context",
    decisionStyle: clean(patterns[1]?.signature) || "evidence first, intuition at the edge",
    aiShould: clean(patterns[2]?.signature) || "challenge premature expansion",
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

export function drawHumanCard(context, model = {}) {
  const width = HUMAN_CARD_WIDTH;
  const height = HUMAN_CARD_HEIGHT;
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

export function shareStats(model = {}) {
  const stats = model.stats || {};
  const lineCount = (stats.evolution_lines || 0) + (stats.relation_lines || 0);
  return [
    ["POINTS", stats.points || model.points?.length || 0],
    ["LINES", lineCount || model.lines?.length || 0],
    ["FACES", stats.faces || model.faces?.length || 0],
    ["VOLUMES", stats.volumes || model.volumes?.length || 0],
    ["ROOT", stats.roots || Number(Boolean(model.root))],
  ];
}

function cleanNarrative(value, limit = 220) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
}

function narrativeStrength(item) {
  const evidence = item.observations
    || item.source_receipts?.length
    || item.member_receipts?.length
    || 0;
  return (Number(item.confidence) || 0) * 1000 + evidence;
}

export function shareNarrative(model = {}) {
  const candidates = [
    ...(model.volumes || []).map((item) => ({ ...item, kind: "VOLUME" })),
    ...(model.faces || []).map((item) => ({ ...item, kind: "FACE" })),
  ].filter((item) => item.signature);
  candidates.sort((left, right) => {
    const kindDelta = Number(right.kind === "VOLUME") - Number(left.kind === "VOLUME");
    if (kindDelta) return kindDelta;
    return narrativeStrength(right) - narrativeStrength(left)
      || String(left.id || "").localeCompare(String(right.id || ""));
  });
  return {
    root: cleanNarrative(model.root?.signature, 240)
      || "A living personal model, shaped by real context over time.",
    highlights: candidates.slice(0, 3).map((item) => ({
      kind: item.kind,
      text: cleanNarrative(item.signature, 110),
    })),
  };
}

function drawCover(context, source, width, height) {
  const sourceWidth = source.width || source.videoWidth || width;
  const sourceHeight = source.height || source.videoHeight || height;
  const scale = Math.max(width / sourceWidth, height / sourceHeight);
  const drawWidth = sourceWidth * scale;
  const drawHeight = sourceHeight * scale;
  context.drawImage(
    source,
    (width - drawWidth) / 2,
    (height - drawHeight) / 2,
    drawWidth,
    drawHeight,
  );
}

function drawBrandMark(context, x, y) {
  context.save();
  context.strokeStyle = "rgba(255, 255, 255, 0.22)";
  context.lineWidth = 1;
  context.beginPath();
  context.arc(x, y, 18, 0, Math.PI * 2);
  context.stroke();

  const nodes = [
    [0, -6, "#ff6b8a"],
    [-6, 5, "#ff64d6"],
    [7, 5, "#7798ff"],
  ];
  context.strokeStyle = "rgba(255, 255, 255, 0.19)";
  context.beginPath();
  context.moveTo(x, y - 6);
  context.lineTo(x - 6, y + 5);
  context.lineTo(x + 7, y + 5);
  context.closePath();
  context.stroke();
  nodes.forEach(([dx, dy, color]) => {
    context.fillStyle = color;
    context.shadowColor = color;
    context.shadowBlur = 10;
    context.beginPath();
    context.arc(x + dx, y + dy, 3, 0, Math.PI * 2);
    context.fill();
  });
  context.restore();
}

export function drawConstellationCard(context, source, model = {}) {
  const width = CONSTELLATION_CARD_WIDTH;
  const height = CONSTELLATION_CARD_HEIGHT;
  const narrative = shareNarrative(model);

  context.save();
  context.fillStyle = "#070610";
  context.fillRect(0, 0, width, height);

  const aura = context.createRadialGradient(760, 300, 20, 760, 300, 610);
  aura.addColorStop(0, "rgba(105, 92, 210, 0.24)");
  aura.addColorStop(0.46, "rgba(49, 35, 91, 0.13)");
  aura.addColorStop(1, "rgba(7, 6, 16, 0)");
  context.fillStyle = aura;
  context.fillRect(0, 0, width, height);

  context.save();
  context.globalAlpha = 0.96;
  drawCover(context, source, width, height);
  context.restore();

  const textScrim = context.createLinearGradient(0, 0, 680, 0);
  textScrim.addColorStop(0, "rgba(7, 6, 16, 0.96)");
  textScrim.addColorStop(0.56, "rgba(7, 6, 16, 0.46)");
  textScrim.addColorStop(1, "rgba(7, 6, 16, 0)");
  context.fillStyle = textScrim;
  context.fillRect(0, 0, 760, height);

  const edge = context.createLinearGradient(0, height - 170, 0, height);
  edge.addColorStop(0, "rgba(7, 6, 16, 0)");
  edge.addColorStop(1, "rgba(7, 6, 16, 0.88)");
  context.fillStyle = edge;
  context.fillRect(0, height - 170, width, 170);

  drawBrandMark(context, 72, 66);
  context.shadowBlur = 0;
  context.fillStyle = "rgba(248, 246, 255, 0.96)";
  context.font = "700 22px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  context.fillText("Persome", 105, 62);
  context.fillStyle = "rgba(164, 158, 181, 0.9)";
  context.font = "700 10px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  context.fillText("PERSONAL MODEL", 105, 80);

  context.fillStyle = "rgba(195, 188, 210, 0.86)";
  context.font = "700 12px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  context.fillText("A LIVING MAP OF WHAT YOU NOTICE, REPEAT, AND BECOME", 54, 286);

  const headline = context.createLinearGradient(54, 310, 430, 430);
  headline.addColorStop(0, "#fff8fc");
  headline.addColorStop(0.52, "#ff85cf");
  headline.addColorStop(1, "#8ea4ff");
  context.fillStyle = headline;
  context.font = "650 52px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  context.fillText("My personal", 52, 349);
  context.fillText("constellation.", 52, 403);

  context.fillStyle = "rgba(255, 100, 214, 0.9)";
  context.font = "750 9px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  context.fillText("CURRENT MODEL", 55, 442);
  context.fillStyle = "rgba(229, 224, 238, 0.88)";
  context.font = "500 14px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  drawWrappedText(context, narrative.root, 54, 466, 430, 20, 3);

  if (narrative.highlights.length) {
    const panelX = 746;
    const panelY = 468;
    const panelWidth = 404;
    const panelHeight = 132;
    context.fillStyle = "rgba(9, 8, 18, 0.78)";
    context.strokeStyle = "rgba(255, 255, 255, 0.12)";
    context.lineWidth = 1;
    context.beginPath();
    context.roundRect(panelX, panelY, panelWidth, panelHeight, 16);
    context.fill();
    context.stroke();
    context.fillStyle = "rgba(162, 154, 181, 0.9)";
    context.font = "750 9px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    context.fillText("KEY PATTERNS", panelX + 18, panelY + 24);
    narrative.highlights.forEach((highlight, index) => {
      const rowY = panelY + 49 + index * 25;
      context.fillStyle = highlight.kind === "VOLUME" ? "#7798ff" : "#ff64d6";
      context.beginPath();
      context.arc(panelX + 20, rowY - 4, 3, 0, Math.PI * 2);
      context.fill();
      context.fillStyle = "rgba(226, 221, 237, 0.9)";
      context.font = "550 11px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
      const line = wrappedLines(context, highlight.text, panelWidth - 52, 1)[0] || "";
      context.fillText(line, panelX + 32, rowY);
    });
  }

  let statX = 56;
  shareStats(model).forEach(([label, value]) => {
    context.fillStyle = "rgba(248, 246, 255, 0.94)";
    context.font = "650 18px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    context.fillText(String(value), statX, 566);
    context.fillStyle = "rgba(137, 130, 153, 0.92)";
    context.font = "700 9px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    context.fillText(label, statX, 583);
    statX += label === "VOLUMES" ? 92 : 78;
  });

  context.fillStyle = "rgba(152, 145, 171, 0.9)";
  context.font = "650 10px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  context.fillText("GENERATED LOCALLY · SHARED BY YOU", 54, 628);
  context.textAlign = "right";
  context.fillText("github.com/Intuition-Lab/personal-model", width - 52, 628);
  context.restore();
}
