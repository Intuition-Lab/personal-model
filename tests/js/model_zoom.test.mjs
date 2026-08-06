import assert from "node:assert/strict";
import test from "node:test";

import { zoomMath } from "../../resources/model_assets/layout.mjs";

function wheel(deltaY, { deltaMode = 0, ctrlKey = false } = {}) {
  return { deltaY, deltaMode, ctrlKey };
}

// One comfortable macOS trackpad pinch-out spreads the fingers over roughly
// 300px of accumulated ctrl+wheel delta, delivered as dozens of small events.
function pinchGesture(totalPixels, events = 40) {
  const step = -totalPixels / events;
  let factor = 1;
  for (let index = 0; index < events; index += 1) {
    factor *= zoomMath.wheelFactor(wheel(step, { ctrlKey: true }), 657);
  }
  return factor;
}

test("a trackpad pinch is worth a meaningful part of the zoom range", () => {
  const factor = pinchGesture(300);
  // The regression this guards: normalising by devicePixelRatio made one whole
  // pinch worth 1.05x, so crossing 50%-400% took about twenty-nine of them.
  assert.ok(factor > 1.8, `one pinch should roughly double the zoom, got ${factor}`);
  assert.ok(factor < 2.2, `one pinch must not overshoot a doubling, got ${factor}`);

  const rangeRatio = 400 / 50;
  const pinchesToCrossRange = Math.log(rangeRatio) / Math.log(factor);
  assert.ok(
    pinchesToCrossRange > 2 && pinchesToCrossRange < 5,
    `crossing the whole range should take a handful of pinches, got ${pinchesToCrossRange}`,
  );
});

test("a gesture composes the same whether it arrives in one event or many", () => {
  const asOneEvent = zoomMath.wheelFactor(wheel(-300, { ctrlKey: true }), 657);
  const asManyEvents = pinchGesture(300, 60);
  assert.ok(
    Math.abs(asOneEvent - asManyEvents) < 1e-9,
    `${asOneEvent} vs ${asManyEvents} — an exponential curve must be path independent`,
  );
});

test("wheel notches are a smaller step than a pinch of the same nominal delta", () => {
  const notch = zoomMath.wheelFactor(wheel(-100), 657);
  const pinch = zoomMath.wheelFactor(wheel(-100, { ctrlKey: true }), 657);
  assert.ok(notch > 1.1 && notch < 1.25, `a wheel notch should be a visible step, got ${notch}`);
  assert.ok(pinch > notch, "a pinch tracks the fingers, so it must out-gain a wheel notch");
});

test("line and page deltas are normalised to pixels", () => {
  // Firefox reports deltaMode 1 with deltaY around 3 per notch. Reading that as
  // pixels made its wheel and pinch do nothing at all.
  assert.equal(zoomMath.wheelPixels(wheel(-3, { deltaMode: 1 }), 657), -48);
  assert.equal(zoomMath.wheelPixels(wheel(2, { deltaMode: 2 }), 300), 400);
  assert.equal(zoomMath.wheelPixels(wheel(-120), 657), -120);

  const firefoxNotch = zoomMath.wheelFactor(wheel(-3, { deltaMode: 1 }), 657);
  assert.ok(firefoxNotch > 1.05, `a line-mode notch must actually zoom, got ${firefoxNotch}`);
});

test("a single absurd event cannot teleport the camera", () => {
  assert.equal(zoomMath.wheelPixels(wheel(-100000), 657), -400);
  assert.equal(zoomMath.wheelPixels(wheel(100000, { deltaMode: 2 }), 4000), 400);
  const factor = zoomMath.wheelFactor(wheel(-1e9, { ctrlKey: true }), 657);
  assert.ok(Number.isFinite(factor) && factor < 3, `one event stays bounded, got ${factor}`);
});

test("zoom direction follows the platform convention", () => {
  assert.ok(zoomMath.wheelFactor(wheel(-100), 657) > 1, "wheel up and pinch out zoom in");
  assert.ok(zoomMath.wheelFactor(wheel(100), 657) < 1, "wheel down and pinch in zoom out");
});

test("opposite gestures of equal size return to where they started", () => {
  const inFactor = zoomMath.wheelFactor(wheel(-140, { ctrlKey: true }), 657);
  const outFactor = zoomMath.wheelFactor(wheel(140, { ctrlKey: true }), 657);
  assert.ok(Math.abs(inFactor * outFactor - 1) < 1e-12, "the curve must be symmetric");
});

test("malformed wheel events are inert rather than destructive", () => {
  // A divide-by-zero in the old normalisation turned one notch into a jump to
  // the zoom limit, so degenerate input must resolve to "no zoom", never NaN.
  assert.equal(zoomMath.wheelPixels(wheel(Number.NaN), 657), 0);
  assert.equal(zoomMath.wheelPixels(wheel(undefined), 657), 0);
  assert.equal(zoomMath.wheelFactor(wheel(Number.NaN), 657), 1);
  assert.equal(zoomMath.wheelPixels(wheel(-100), 0), -100);
  assert.ok(Number.isFinite(zoomMath.wheelFactor(wheel(-100), Number.NaN)));
});

test("a trackpad's sub-percent steps survive as a continuous factor", () => {
  // The wheel path multiplies a continuous goal distance rather than a rounded
  // percent: a trackpad delivers dozens of sub-percent events a second, and
  // rounding each one would quantise the glide into visible stairs.
  const tiny = zoomMath.wheelFactor(wheel(-1, { ctrlKey: true }), 657);
  assert.ok(tiny > 1 && tiny < 1.01, `a 1px event must still register, got ${tiny}`);
  assert.notEqual(tiny, 1);
});

test("the button and keyboard zoom steps are unchanged", () => {
  // stepZoom still snaps to the 25% grid; only wheel and pinch went continuous.
  assert.equal(zoomMath.nextPercent(100, 1, 25, 50, 400), 125);
  assert.equal(zoomMath.nextPercent(141, -1, 25, 50, 400), 125);
  assert.equal(zoomMath.nextPercent(400, 1, 25, 50, 400), 400);
  assert.equal(zoomMath.nextPercent(50, -1, 25, 50, 400), 50);
});
