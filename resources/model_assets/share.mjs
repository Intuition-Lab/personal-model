export const SHARE_CARD_WIDTH = 1200;
export const SHARE_CARD_HEIGHT = 675;
export const SHARE_FILE_NAME = "my-persome-constellation.png";
export const SHARE_URL = "https://github.com/Intuition-Lab/personal-model";
export const SHARE_HASHTAGS = ["Persome", "PersonalAI"];
export const SHARE_TEXT = [
  "My Mac has been quietly learning the shape of how I work.",
  "",
  "This is my personal constellation — built locally from real context with Persome.",
  "",
  "Build yours →",
].join("\n");

export function buildXIntentUrl({
  text = SHARE_TEXT,
  url = SHARE_URL,
  hashtags = SHARE_HASHTAGS,
} = {}) {
  const params = new URLSearchParams();
  params.set("text", text);
  params.set("url", url);
  params.set("hashtags", hashtags.join(","));
  return `https://x.com/intent/tweet?${params.toString()}`;
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

export function drawShareCard(context, source, model = {}) {
  const width = SHARE_CARD_WIDTH;
  const height = SHARE_CARD_HEIGHT;

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
  context.fillText("A LIVING MAP OF WHAT YOU NOTICE, REPEAT, AND BECOME", 54, 326);

  const headline = context.createLinearGradient(54, 350, 430, 510);
  headline.addColorStop(0, "#fff8fc");
  headline.addColorStop(0.52, "#ff85cf");
  headline.addColorStop(1, "#8ea4ff");
  context.fillStyle = headline;
  context.font = "650 58px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  context.fillText("My personal", 52, 397);
  context.fillText("constellation.", 52, 458);

  let statX = 56;
  shareStats(model).forEach(([label, value]) => {
    context.fillStyle = "rgba(248, 246, 255, 0.94)";
    context.font = "650 18px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    context.fillText(String(value), statX, 554);
    context.fillStyle = "rgba(137, 130, 153, 0.92)";
    context.font = "700 9px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    context.fillText(label, statX, 571);
    statX += label === "VOLUMES" ? 92 : 78;
  });

  context.fillStyle = "rgba(152, 145, 171, 0.9)";
  context.font = "650 10px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  context.fillText("GENERATED LOCALLY · SHARED BY YOU", 54, 628);
  context.textAlign = "right";
  context.fillText("github.com/Intuition-Lab/personal-model", width - 52, 628);
  context.restore();
}
