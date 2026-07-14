"""Unified owner-local onboarding shell served beside the model viewer."""

from __future__ import annotations

import re

_BASE_RE = re.compile(r"^/model(?:/[A-Za-z0-9_-]{32,128})?/$")

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Set up Persome</title>
  <base href="__BASE__">
  <link rel="icon" href="data:,">
  <link rel="stylesheet" href="assets/onboarding.css">
</head>
<body>
  <main id="onboarding" aria-live="polite">
    <header class="brand"><span class="mark" aria-hidden="true"><i></i><i></i><i></i></span><strong>Persome</strong><span>Local only</span></header>
    <nav id="steps" aria-label="Setup progress"></nav>
    <section id="screen" class="screen"></section>
  </main>
  <script type="module" src="assets/onboarding.js"></script>
</body>
</html>"""


def render_onboarding_view(base_path: str) -> str:
    if _BASE_RE.fullmatch(base_path) is None:
        raise ValueError("invalid onboarding base path")
    return _TEMPLATE.replace("__BASE__", base_path)
