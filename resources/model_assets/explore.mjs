const KIND_PRIORITY = {
  root: 0,
  volume: 1,
  face: 2,
  point: 3,
  line: 4,
  context: 5,
};
const HISTORICAL_POINT_PENALTY = -72;
const INACTIVE_POINT_PENALTY = -56;
const EXACT_STATE_QUERY_BOOST = 120;

const MODEL_GESTURE_PASSTHROUGH_SELECTOR = ".detail, .line-explorer, .search-panel";

export function handleSearchShortcut(event, editing, openSearch) {
  const shortcut = (event?.metaKey || event?.ctrlKey)
    && String(event?.key || "").toLowerCase() === "k";
  if (!shortcut) return false;
  // Search moves focus into its dialog. While the owner is rewriting a claim,
  // that blur is also the save gesture, so consume the chord without opening
  // search rather than committing text that was still being drafted.
  event.preventDefault?.();
  if (!editing) openSearch?.();
  return true;
}

export function shouldHandleModelGesture(event) {
  // A pinch is model navigation even when it starts over a scroll container;
  // otherwise the browser zooms the entire page. Ordinary wheel input still
  // belongs to panels with real scrolling. The legend is not one of them.
  const pinch = Boolean(event?.ctrlKey)
    || String(event?.type || "").startsWith("gesture");
  if (pinch) return true;
  return !event?.target?.closest?.(MODEL_GESTURE_PASSTHROUGH_SELECTOR);
}

export function pointerUpOutcome(event, overTarget = false) {
  const selectionEligible = event?.pointerType !== "mouse" || event?.button === 0;
  return {
    selectionEligible,
    cursor: selectionEligible && overTarget ? "pointer" : "grab",
  };
}

export function reconcileSceneSelection(
  selection,
  items,
  layerVisible,
  kindLayers,
  replacement = null,
) {
  if (!selection?.kind || !selection?.id) {
    return { selection: null, invalidated: false, replaced: false };
  }

  let next = selection;
  let replaced = false;
  if (
    replacement?.kind === selection.kind
    && replacement?.fromId === selection.id
    && replacement?.toId
    && items?.has?.(selectionKey(selection.kind, replacement.toId))
  ) {
    next = { kind: selection.kind, id: replacement.toId };
    replaced = replacement.toId !== selection.id;
  }

  const key = selectionKey(next.kind, next.id);
  const layer = kindLayers?.[next.kind];
  if (!items?.has?.(key) || (layer && !layerVisible?.[layer])) {
    return { selection: null, invalidated: true, replaced: false };
  }
  return { selection: next, invalidated: false, replaced };
}

export function recoverInvalidSceneSelection(reconciliation, clearSelection, resetCamera) {
  if (!reconciliation?.invalidated) return false;
  clearSelection?.();
  resetCamera?.();
  return true;
}

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

function searchWeight(entry) {
  const weight = Number(entry.weight || 0);
  return Number.isFinite(weight) ? weight : 0;
}

export function prepareSearchEntries(entries) {
  // This work belongs to scene rebuilds, not the per-keystroke ranking path.
  return entries.map((entry) => {
    const title = String(entry.title || "Untitled").replace(/\s+/g, " ").trim();
    const subtitle = String(entry.subtitle || "").replace(/\s+/g, " ").trim();
    const aliases = Array.isArray(entry.aliases) ? entry.aliases.filter(Boolean) : [];
    const stateAliases = Array.isArray(entry.stateAliases)
      ? entry.stateAliases.filter(Boolean)
      : [];
    const normalizedTitle = normalize(title);
    return {
      ...entry,
      title,
      subtitle,
      aliases,
      stateAliases,
      normalizedTitle,
      normalizedSearchable: normalize([title, subtitle, ...aliases].join(" ")),
      normalizedStateAliases: stateAliases.map(normalize).filter(Boolean),
      normalizedTitleWords: normalizedTitle.split(/[^\p{L}\p{N}]+/u).filter(Boolean),
    };
  });
}

function timestamp(value, fallback = Number.NaN) {
  if (value === null || value === undefined || value === "") return fallback;
  const parsed = value instanceof Date ? value.getTime() : new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : fallback;
}

function temporalStart(item) {
  for (const value of [item?.valid_from, item?.created_at, item?.occurred_at]) {
    const parsed = timestamp(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return Number.NaN;
}

function pointTemporalEnd(point, pointById = null) {
  const candidates = [];
  const explicitEnd = timestamp(point?.valid_until);
  if (Number.isFinite(explicitEnd)) candidates.push(explicitEnd);
  if (pointById?.get) {
    (point?.superseded_by || []).forEach((successorId) => {
      const successorStart = temporalStart(pointById.get(successorId));
      if (Number.isFinite(successorStart)) candidates.push(successorStart);
    });
  }
  return candidates.length ? Math.min(...candidates) : Number.NaN;
}

export function pointSearchMetadata(point, cutoff = new Date(), pointById = null) {
  const status = String(point?.status || "").trim().toLocaleLowerCase();
  const cutoffTime = timestamp(cutoff, Date.now());
  const validFrom = temporalStart(point);
  const validUntil = pointTemporalEnd(point, pointById);
  const started = !Number.isFinite(validFrom) || validFrom <= cutoffTime;
  const ended = Number.isFinite(validUntil) && validUntil <= cutoffTime;
  const insideValidity = started && !ended;
  if (!started) {
    return {
      state: "future",
      subtitle: "Modeled observation · not yet valid at this date",
      aliases: ["future", "not yet valid"],
      weightAdjustment: INACTIVE_POINT_PENALTY,
    };
  }
  // Superseding a Point rewrites its present-day lifecycle fields to
  // is_latest=false/status=shadow. Before its valid_until, however, that
  // predecessor is the head at the selected historical cutoff.
  const historicalHead = !point?.is_latest && Number.isFinite(validUntil) && insideValidity;
  const isCurrent = insideValidity && (
    (Boolean(point?.is_latest) && status === "active") || historicalHead
  );
  if (isCurrent) {
    return {
      state: "current",
      subtitle: "Modeled observation · current",
      aliases: ["current", "active"],
      weightAdjustment: 0,
    };
  }

  if (!point?.is_latest || ended) {
    const historyStates = [...new Set([
      ended ? "ended" : "",
      status && status !== "active" ? status : "",
    ].filter(Boolean))];
    const statusSuffix = historyStates.length ? ` · ${historyStates.join(" · ")}` : "";
    return {
      state: "history",
      subtitle: `Modeled observation · historical${statusSuffix}`,
      aliases: ["history", "historical", "not current", ...historyStates],
      // Exact content remains findable, but a retired revision cannot outrank
      // the current version of the same claim.
      weightAdjustment: HISTORICAL_POINT_PENALTY,
    };
  }

  const inactiveState = status || "inactive";
  return {
    state: inactiveState,
    subtitle: `Modeled observation · ${inactiveState} · not active`,
    aliases: [inactiveState, "inactive", "not active"],
    weightAdjustment: INACTIVE_POINT_PENALTY,
  };
}

export function pointVisibleAt(point, cutoff = new Date(), pointById = null) {
  return pointSearchMetadata(point, cutoff, pointById).state === "current";
}

export function pointKnownAt(point, cutoff = new Date()) {
  const validFrom = temporalStart(point);
  return !Number.isFinite(validFrom) || validFrom <= timestamp(cutoff, Date.now());
}

export function lineKnownAt(line, model, cutoff = new Date(), pointById = null) {
  if (line?.kind === "evolution") {
    const target = pointById?.get?.(line.target)
      || (model?.points || []).find((point) => point.id === line.target);
    return Boolean(target) && pointKnownAt(target, cutoff);
  }
  const started = temporalStart(line);
  return !Number.isFinite(started) || started <= timestamp(cutoff, Date.now());
}

function scoreEntry(entry, terms, normalizedQuery) {
  const title = typeof entry.normalizedTitle === "string"
    ? entry.normalizedTitle
    : normalize(entry.title);
  const searchable = typeof entry.normalizedSearchable === "string"
    ? entry.normalizedSearchable
    : normalize([
      entry.title,
      entry.subtitle,
      ...(entry.aliases || []),
    ].filter(Boolean).join(" "));
  const titleWords = Array.isArray(entry.normalizedTitleWords)
    ? entry.normalizedTitleWords
    : title.split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  const stateAliases = Array.isArray(entry.normalizedStateAliases)
    ? entry.normalizedStateAliases
    : (entry.stateAliases || []).map(normalize).filter(Boolean);
  if (!terms.length) return searchWeight(entry);

  let score = stateAliases.includes(normalizedQuery) ? EXACT_STATE_QUERY_BOOST : 0;
  for (const term of terms) {
    if (title === term) score += 180;
    else if (title.startsWith(term)) score += 110;
    else if (titleWords.some((word) => word.startsWith(term))) score += 78;
    else if (title.includes(term)) score += 58;
    else if (searchable.includes(term)) score += 34;
    else {
      const fuzzy = subsequenceScore(title, term);
      if (!fuzzy) return null;
      score += fuzzy;
    }
  }
  return score + searchWeight(entry);
}

function compareRankedEntries(left, right) {
  return (
    right.score - left.score
    || (KIND_PRIORITY[left.entry.kind] ?? 99) - (KIND_PRIORITY[right.entry.kind] ?? 99)
    || String(left.entry.title || "").localeCompare(String(right.entry.title || ""))
    || String(left.entry.key || "").localeCompare(String(right.entry.key || ""))
  );
}

function insertBounded(ranked, candidate, limit) {
  if (
    ranked.length === limit
    && compareRankedEntries(candidate, ranked[ranked.length - 1]) >= 0
  ) return;

  let low = 0;
  let high = ranked.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (compareRankedEntries(candidate, ranked[middle]) < 0) high = middle;
    else low = middle + 1;
  }
  ranked.splice(low, 0, candidate);
  if (ranked.length > limit) ranked.pop();
}

export function rankSearchEntries(entries, query, limit = 9) {
  const boundedLimit = Math.max(1, Math.min(50, Number(limit) || 9));
  const normalizedQuery = normalize(query);
  const terms = normalizedQuery.split(" ").filter(Boolean);
  const ranked = [];
  entries.forEach((entry) => {
    const score = scoreEntry(entry, terms, normalizedQuery);
    if (score !== null) insertBounded(ranked, { entry, score }, boundedLimit);
  });
  return ranked.map(({ entry }) => entry);
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
  const pointForEndpoint = (id) => layout?.endpointPointIds?.get?.(id) || null;
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
  const addEndpoint = (id) => {
    const pointId = pointForEndpoint(id);
    add(pointId ? "point" : kindForId(id), pointId || id);
  };
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
      .filter((line) => [line.source, line.target].some((endpoint) => (
        endpoint === selection.id || pointForEndpoint(endpoint) === selection.id
      )))
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
