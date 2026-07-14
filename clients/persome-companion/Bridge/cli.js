#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";
import { BridgeStore } from "./store.js";
import { createBridgeServer, RuntimeForwarder } from "./server.js";

const root = process.env.PERSOME_BRIDGE_ROOT ?? path.join(os.homedir(), ".persome", "companion");
const store = new BridgeStore(path.join(root, "bridge-state.json"));
const command = process.argv[2] ?? "help";

if (command === "cert") {
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  const certPath = process.env.PERSOME_BRIDGE_CERT ?? path.join(root, "bridge-cert.pem");
  const keyPath = process.env.PERSOME_BRIDGE_KEY ?? path.join(root, "bridge-key.pem");
  const address = lanAddress();
  const result = spawnSync(
    "openssl",
    [
      "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "365",
      "-subj", "/CN=Persome Companion Bridge",
      "-addext", `subjectAltName=IP:${address},IP:127.0.0.1,DNS:localhost`,
      "-keyout", keyPath,
      "-out", certPath,
    ],
    { stdio: "inherit" },
  );
  if (result.status !== 0) throw new Error("openssl certificate generation failed");
  fs.chmodSync(keyPath, 0o600);
  fs.chmodSync(certPath, 0o600);
  const fingerprint = new crypto.X509Certificate(fs.readFileSync(certPath))
    .fingerprint256.replaceAll(":", "").toLowerCase();
  console.log(JSON.stringify({ certPath, keyPath, fingerprint, address }, null, 2));
} else if (command === "pair") {
  const pairing = store.createPairing();
  const endpoint = process.env.PERSOME_BRIDGE_URL ?? `https://${lanAddress()}:8744`;
  const certPath = process.env.PERSOME_BRIDGE_CERT ?? path.join(root, "bridge-cert.pem");
  const fingerprint = fs.existsSync(certPath)
    ? new crypto.X509Certificate(fs.readFileSync(certPath)).fingerprint256.replaceAll(":", "").toLowerCase()
    : null;
  console.log(JSON.stringify({ version: 1, endpoint, fingerprint, ...pairing }, null, 2));
} else if (command === "devices") {
  console.log(JSON.stringify(store.listDevices(), null, 2));
} else if (command === "revoke") {
  const deviceId = process.argv[3];
  if (!deviceId || !store.revoke(deviceId)) process.exitCode = 1;
} else if (command === "start") {
  const token = process.env.PERSOME_LOCAL_API_TOKEN;
  if (!token) throw new Error("PERSOME_LOCAL_API_TOKEN is required");
  const certPath = process.env.PERSOME_BRIDGE_CERT ?? path.join(root, "bridge-cert.pem");
  const keyPath = process.env.PERSOME_BRIDGE_KEY ?? path.join(root, "bridge-key.pem");
  const server = createBridgeServer({
    store,
    certPath,
    keyPath,
    runtime: new RuntimeForwarder({ token }),
  });
  const host = process.env.PERSOME_BRIDGE_HOST ?? "0.0.0.0";
  const port = Number(process.env.PERSOME_BRIDGE_PORT ?? 8744);
  server.listen(port, host, () => console.log(`Persome Companion Bridge listening on ${host}:${port}`));
} else {
  console.log("Usage: npm run bridge -- <cert|start|pair|devices|revoke DEVICE_ID>");
}

function lanAddress() {
  for (const addresses of Object.values(os.networkInterfaces())) {
    for (const address of addresses ?? []) {
      if (address.family === "IPv4" && !address.internal) return address.address;
    }
  }
  return "127.0.0.1";
}
