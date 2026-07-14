import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { BridgeStore } from "../store.js";

const makeStore = (clock = () => Date.now()) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "persome-bridge-"));
  return new BridgeStore(path.join(root, "state.json"), clock);
};

test("pairing is one-time and stores only token hash", () => {
  const store = makeStore(() => 1_000);
  const pairing = store.createPairing();
  const device = { id: "iphone-1", platform: "ios", name: "iPhone" };
  const result = store.consumePairing({ pairingId: pairing.pairingId, code: pairing.code, device });

  assert.equal(result.ok, true);
  assert.equal(store.consumePairing({ pairingId: pairing.pairingId, code: pairing.code, device }).ok, false);
  assert.equal(store.authenticate(result.token).id, "iphone-1");
  assert.equal(JSON.stringify(store.load()).includes(result.token), false);
});

test("pairing expires and repeated guesses lock it", () => {
  let now = 1_000;
  const store = makeStore(() => now);
  const expired = store.createPairing({ ttlMs: 10 });
  now = 2_000;
  assert.equal(
    store.consumePairing({
      pairingId: expired.pairingId,
      code: expired.code,
      device: { id: "x", platform: "ios" },
    }).reason,
    "expired",
  );

  const locked = store.createPairing();
  for (let attempt = 0; attempt < 5; attempt += 1) {
    assert.equal(
      store.consumePairing({
        pairingId: locked.pairingId,
        code: "000000",
        device: { id: "x", platform: "ios" },
      }).ok,
      false,
    );
  }
  assert.equal(
    store.consumePairing({
      pairingId: locked.pairingId,
      code: "000000",
      device: { id: "x", platform: "ios" },
    }).reason,
    "locked",
  );
});

test("revoked device can no longer authenticate", () => {
  const store = makeStore();
  const pairing = store.createPairing();
  const paired = store.consumePairing({
    pairingId: pairing.pairingId,
    code: pairing.code,
    device: { id: "iphone-1", platform: "ios" },
  });
  assert.ok(store.authenticate(paired.token));
  assert.equal(store.revoke("iphone-1"), true);
  assert.equal(store.authenticate(paired.token), null);
});
