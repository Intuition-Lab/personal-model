import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { BridgeStore } from "../store.js";
import { RuntimeForwarder } from "../server.js";

test("event receipt makes retries idempotent", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "persome-bridge-"));
  const store = new BridgeStore(path.join(root, "state.json"), () => 1_000);
  assert.equal(store.hasReceipt("iphone-1", "event-1"), false);
  store.recordReceipt("iphone-1", "event-1", { id: "capture-1" });
  assert.equal(store.hasReceipt("iphone-1", "event-1"), true);
  assert.deepEqual(store.load().receipts["iphone-1:event-1"].runtimeReceipt, {
    id: "capture-1",
  });
});

test("runtime skip is retryable and never acknowledged", async () => {
  const forwarder = new RuntimeForwarder({
    token: "local-only-token",
    fetchImpl: async () => new Response(
      JSON.stringify({ data: { id: null, skipped: true } }),
      { status: 200, headers: { "content-type": "application/json" } },
    ),
  });

  await assert.rejects(
    forwarder.forward({ event_id: "event-1" }),
    (error) => error.statusCode === 503,
  );
});
