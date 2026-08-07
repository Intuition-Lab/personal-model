const KIND_PRIORITY = {
  root: 0,
  volume: 1,
  face: 2,
  point: 3,
  line: 4,
  context: 5,
};

function normalize(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

function subsequenceScore(text, query) {
  let cursor = -1;
  let gap = 0;
  for (const character of query) {
    const next = text.indexOf(character, cursor + 1);
    if (next < 0) return 0;
    if (cursor >= 0) gap += next - cursor - 1;
    cursor = next;
  }
  return Math.max(4, 18 - Math.min(gap, 14));
}

function scoreEntry(entry, query) {
  const title = normalize(entry.title);
  const searchable = normalize([
    entry.title,
    entry.subtitle,
    ...(entry.aliases || []),
  ].filter(Boolean).join(" "));
  const terms = normalize(query).split(" ").filter(Boolean);
  if (!terms.length) return Number(entry.weight || 0);

  let score = 0;
  for (const term of terms) {
    if (title === term) score += 180;
    else if (title.startsWith(term)) score += 110;
    else if (title.split(/[^\p{L}\p{N}]+/u).some((word) => word.startsWith(term))) score += 78;
    else if (title.includes(term)) score += 58;
    else if (searchable.includes(term)) score += 34;
    else {
      const fuzzy = subsequenceScore(title, term);
      if (!fuzzy) return null;
      score += fuzzy;
    }
  }
  return score + Number(entry.weight || 0);
}

export function rankSearchEntries(entries, query, limit = 9) {
  const boundedLimit = Math.max(1, Math.min(50, Number(limit) || 9));
  return entries
    .map((entry) => ({ entry, score: scoreEntry(entry, query) }))
    .filter(({ score }) => score !== null)
    .sort((left, right) => (
      right.score - left.score
      || (KIND_PRIORITY[left.entry.kind] ?? 99) - (KIND_PRIORITY[right.entry.kind] ?? 99)
      || String(left.entry.title || "").localeCompare(String(right.entry.title || ""))
      || String(left.entry.key || "").localeCompare(String(right.entry.key || ""))
    ))
    .slice(0, boundedLimit)
    .map(({ entry }) => entry);
}

function selectionKey(kind, id) {
  return `${kind}:${id}`;
}

function mapValues(map, key) {
  const value = map?.get?.(key);
  return Array.isArray(value) ? value : [];
}

export function focusKeysForSelection(model, layout, selection) {
  const focus = new Set();
  if (!selection?.kind || !selection?.id) return focus;

  const points = new Set((model?.points || []).map((item) => item.id));
  const faces = new Set((model?.faces || []).map((item) => item.id));
  const volumes = new Set((model?.volumes || []).map((item) => item.id));
  const rootId = model?.root?.id;
  const kindForId = (id) => {
    if (points.has(id)) return "point";
    if (faces.has(id)) return "face";
    if (volumes.has(id)) return "volume";
    if (rootId === id) return "root";
    return "context";
  };
  const add = (kind, id) => {
    if (id) focus.add(selectionKey(kind, id));
  };
  const addEndpoint = (id) => add(kindForId(id), id);
  const addLine = (line) => {
    if (!line) return;
    add("line", line.id);
    addEndpoint(line.source);
    addEndpoint(line.target);
  };
  const addFace = (faceId, includePoints = true) => {
    if (!faceId) return;
    add("face", faceId);
    if (includePoints) mapValues(layout?.facePointIds, faceId).forEach((id) => add("point", id));
  };
  const addVolume = (volumeId, includeFaces = true) => {
    if (!volumeId) return;
    add("volume", volumeId);
    if (includeFaces) mapValues(layout?.volumeFaceIds, volumeId).forEach((id) => addFace(id, false));
  };

  add(selection.kind, selection.id);
  if (selection.kind === "line") {
    addLine((model?.lines || []).find((line) => line.id === selection.id));
    return focus;
  }

  if (selection.kind === "point" || selection.kind === "context") {
    (model?.lines || [])
      .filter((line) => line.source === selection.id || line.target === selection.id)
      .forEach(addLine);
  }

  if (selection.kind === "point") {
    const cluster = layout?.pointClusterById?.get?.(selection.id);
    if (String(cluster || "").startsWith("face:")) addFace(String(cluster).slice(5));
  } else if (selection.kind === "face") {
    addFace(selection.id);
    layout?.volumeFaceIds?.forEach?.((faceIds, volumeId) => {
      if (faceIds.includes(selection.id)) addVolume(volumeId, false);
    });
  } else if (selection.kind === "volume") {
    addVolume(selection.id);
    if ((layout?.rootVolumeIds || []).includes(selection.id) && rootId) add("root", rootId);
  } else if (selection.kind === "root") {
    (layout?.rootVolumeIds || []).forEach((id) => addVolume(id, false));
  }

  return focus;
}

export const exploreText = { normalize };
