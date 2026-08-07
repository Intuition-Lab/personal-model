"""Offline HTML shell for the canonical personal-model snapshot viewer."""

from __future__ import annotations

import re

_MODEL_BASE_RE = re.compile(r"^/model(?:/[A-Za-z0-9_-]{32,128})?/$")

_MEMORY_VIEW_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Persome Personal Model</title>
  <base href="__PERSOME_MODEL_BASE__">
  <link rel="icon" href="data:,">
  <link rel="stylesheet" href="assets/viewer.css">
  <script type="importmap">
  {"imports": {
    "three": "./assets/three.module.js",
    "three/addons/": "./assets/jsm/"
  }}
  </script>
</head>
<body>
  <main id="viewer" aria-label="Persome personal model explorer">
    <div id="line-explorer" class="line-explorer" hidden>
      <label for="line-select">Open a model line</label>
      <select id="line-select">
        <option value="">Choose a relationship…</option>
      </select>
    </div>

    <div id="canvas"></div>

    <header class="topbar">
      <div class="brand-tools">
        <div class="brand">
          <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
          <span class="brand-lockup">
            <strong>Persome</strong>
            <span>Personal Model</span>
          </span>
        </div>
        <button id="open-search" class="search-trigger" type="button" aria-haspopup="dialog" aria-controls="model-search-panel" title="Find in your model (⌘K)">
          <span aria-hidden="true">⌕</span><b>Find in your model</b><kbd>⌘K</kbd>
        </button>
      </div>
      <div class="layers" role="group" aria-label="Visible model layers">
        <button type="button" data-layer="points" aria-pressed="true" title="Toggle Points"><i></i><span>Points</span><small id="layer-count-points" aria-hidden="true">0</small></button>
        <button type="button" data-layer="lines" aria-pressed="true" title="Toggle Lines"><i></i><span>Lines</span><small id="layer-count-lines" aria-hidden="true">0</small></button>
        <button type="button" data-layer="faces" aria-pressed="true" title="Toggle Faces"><i></i><span>Faces</span><small id="layer-count-faces" aria-hidden="true">0</small></button>
        <button type="button" data-layer="volumes" aria-pressed="true" title="Toggle Volumes"><i></i><span>Volumes</span><small id="layer-count-volumes" aria-hidden="true">0</small></button>
        <button type="button" data-layer="root" aria-pressed="true" title="Toggle Root"><i></i><span>Root</span><small id="layer-count-root" aria-hidden="true">0</small></button>
      </div>
      <div class="view-actions">
        <span class="privacy-badge"><i aria-hidden="true"></i>Local only</span>
        <button id="human-card" class="share-button" type="button" aria-label="Export your HUMAN.md Card" title="Export your HUMAN.md Card" disabled>
          <span aria-hidden="true">H</span><b>Card</b>
        </button>
        <button id="share-x" class="share-button" type="button" aria-label="Share your constellation to X" title="Share your constellation to X" disabled>
          <span aria-hidden="true">X</span><b>Share</b>
        </button>
        <div class="zoom-controls" role="group" aria-label="Zoom controls">
          <button id="zoom-out" class="icon-button" type="button" aria-label="Zoom out" title="Zoom out (−)">−</button>
          <button id="zoom-reset" class="zoom-value" type="button" aria-label="Reset zoom to 100 percent" title="Reset zoom (0)">100%</button>
          <button id="zoom-in" class="icon-button" type="button" aria-label="Zoom in" title="Zoom in (+)">+</button>
        </div>
        <button id="rotate" class="icon-button action-button" type="button" aria-label="Toggle rotation" aria-pressed="false" title="Toggle rotation"><span aria-hidden="true">↻</span><b>Orbit</b></button>
        <button id="reset" class="icon-button action-button" type="button" aria-label="Reset camera" title="Reset camera"><span aria-hidden="true">⌁</span><b>Frame</b></button>
      </div>
    </header>

    <section class="story" aria-labelledby="story-title">
      <p class="story-kicker"><span aria-hidden="true"></span>Live personal model</p>
      <h1 id="story-title">The shape<br><em>of you.</em></h1>
      <p id="model-identity" class="model-identity">A living map of what you notice, repeat, and become.</p>
      <p class="model-flow"><span>Observe</span><i></i><span>Connect</span><i></i><span>Understand</span></p>
    </section>

    <section id="status" class="status" aria-label="Model composition" aria-live="polite">
      <span class="status-loading">Loading your model…</span>
    </section>

    <details class="legend" aria-label="Model layer legend" open>
      <summary><span>How to read your model</span><small>7 cues</small></summary>
      <div class="legend-body">
        <div><span class="swatch point"></span><b>Point</b><span>sourced observation</span></div>
        <div><span class="swatch line"></span><b>Line</b><span>stored evolution or relation</span></div>
        <div><span class="swatch guide"></span><b>Guide</b><span>inferred placement · not evidence</span></div>
        <div><span class="swatch context"></span><b>Entity</b><span>the other end of a relation</span></div>
        <div><span class="swatch face"></span><b>Face</b><span>stable pattern</span></div>
        <div><span class="swatch volume"></span><b>Volume</b><span>cross-pattern structure</span></div>
        <div><span class="swatch root"></span><b>Root</b><span>current personal model</span></div>
      </div>
    </details>

    <details id="mobile-guide" class="mobile-guide" aria-label="Hierarchy guide explanation">
      <summary><span class="swatch guide" aria-hidden="true"></span><b>Guide</b><span>inferred placement · not evidence</span></summary>
      <p>These connectors help arrange the hierarchy. Open Evidence on a model object for sourced support.</p>
    </details>

    <section id="model-search-panel" class="search-panel" role="dialog" aria-modal="true" aria-labelledby="search-title" hidden>
      <div class="search-dialog">
        <header>
          <div>
            <p class="search-kicker">Local model explorer</p>
            <h2 id="search-title">Find a thought, pattern, or relation</h2>
          </div>
          <button id="close-search" class="icon-button" type="button" aria-label="Close model search" title="Close (Esc)">×</button>
        </header>
        <label class="visually-hidden" for="model-search">Search your personal model</label>
        <div class="search-field">
          <span aria-hidden="true">⌕</span>
          <input id="model-search" type="search" role="combobox" autocomplete="off" spellcheck="false" placeholder="Search the local snapshot…" aria-autocomplete="list" aria-expanded="false" aria-controls="search-results" aria-describedby="search-summary">
          <kbd>ESC</kbd>
        </div>
        <p id="search-summary" class="search-summary" aria-live="polite">Showing the clearest parts of your model.</p>
        <div id="search-results" class="search-results" role="listbox" aria-label="Model search results"></div>
        <p id="search-empty" class="search-empty" hidden>No matching model object. Try a shorter phrase.</p>
        <footer><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> focus</span><span>Search queries stay on this Mac</span></footer>
      </div>
    </section>

    <aside id="detail" class="detail" aria-labelledby="detail-title" hidden>
      <button id="close-detail" class="icon-button close" type="button" aria-label="Close" title="Close (Esc)">×</button>
      <p class="detail-eyebrow"><span id="detail-kind" class="detail-kind"></span><span id="detail-provenance">Evidence-backed</span></p>

      <div class="focus-note" role="note">
        <span><b>Focused neighborhood</b><small>Navigation view · not evidence</small></span>
        <button id="clear-focus" type="button">Show all</button>
      </div>

      <h1 id="detail-title" class="detail-claim" aria-describedby="detail-hint"></h1>
      <textarea id="detail-claim-input" class="detail-claim detail-claim-input" rows="1" maxlength="4000" aria-label="Edit this claim in your own words" hidden></textarea>
      <p id="detail-hint" class="detail-hint" hidden></p>
      <p id="detail-status" class="detail-status" role="status"></p>

      <div id="detail-meta" class="detail-meta"></div>
      <div id="detail-summary" class="detail-summary"></div>

      <details id="detail-evidence-fold" class="detail-fold">
        <summary><span>Evidence</span><b id="detail-evidence-count" class="detail-fold-count"></b><i aria-hidden="true"></i></summary>
        <nav id="evidence-breadcrumbs" class="evidence-breadcrumbs" aria-label="Evidence drill-down"></nav>
        <div id="detail-receipts" class="detail-receipts"></div>
      </details>
      <details id="detail-history-fold" class="detail-fold">
        <summary><span>History</span><b id="detail-history-count" class="detail-fold-count"></b><i aria-hidden="true"></i></summary>
        <div id="detail-history-list" class="detail-receipts"></div>
      </details>

      <div id="detail-actions" class="detail-actions" hidden>
        <button id="detail-reject" class="detail-reject" type="button">This is wrong about me</button>
      </div>
    </aside>

    <div id="empty" class="empty" hidden>
      <strong>No model yet</strong>
      <span>Keep the daemon running, then build the personal model.</span>
    </div>

    <div id="error" class="error" role="alert" hidden></div>

    <!-- Owned by the editor, not the loader. `#error` is cleared by every
         successful model poll, which would silently erase a save failure the
         owner has not read yet. -->
    <div id="edit-alert" class="error edit-alert" role="alert" hidden></div>

    <div id="share-notice" class="share-notice" role="status" aria-live="polite" hidden>
      <span aria-hidden="true">↓</span>
      <div><strong>HUMAN.md Card downloaded</strong><small>Detected secrets, PII, paths, IDs, and evidence receipts were excluded. Review summaries before sharing.</small></div>
    </div>

    <footer class="timeline">
      <button id="play" class="icon-button" type="button" aria-label="Play model history" aria-pressed="false" title="Play model history">▶</button>
      <div class="timeline-copy"><strong>Model evolution</strong><label for="as-of">Travel through your history</label></div>
      <div class="timeline-track"><input id="as-of" type="range" min="0" max="100" step="1" value="100"></div>
      <output id="as-of-label" for="as-of">Now</output>
    </footer>

    <p class="gesture-hint"><span aria-hidden="true">↗</span> Drag to orbit · Pinch to zoom · Select to focus · ⌘K to find</p>
  </main>
  <script type="module" src="assets/viewer.js"></script>
</body>
</html>
"""


def render_memory_view(base_path: str = "/model/") -> str:
    """Render the viewer with a strict same-session base URL."""
    if _MODEL_BASE_RE.fullmatch(base_path) is None:
        raise ValueError("invalid model viewer base path")
    return _MEMORY_VIEW_TEMPLATE.replace("__PERSOME_MODEL_BASE__", base_path)
