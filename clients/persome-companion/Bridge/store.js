import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const emptyState = () => ({ version: 1, pairings: {}, devices: {}, receipts: {} });

export class BridgeStore {
  constructor(filePath, clock = () => Date.now()) {
    this.filePath = filePath;
    this.clock = clock;
  }

  load() {
    try {
      const parsed = JSON.parse(fs.readFileSync(this.filePath, "utf8"));
      return { ...emptyState(), ...parsed };
    } catch (error) {
      if (error.code === "ENOENT") return emptyState();
      throw error;
    }
  }

  save(state) {
    fs.mkdirSync(path.dirname(this.filePath), { recursive: true, mode: 0o700 });
    const temp = `${this.filePath}.${process.pid}.tmp`;
    fs.writeFileSync(temp, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
    fs.renameSync(temp, this.filePath);
    fs.chmodSync(this.filePath, 0o600);
  }

  createPairing({ ttlMs = 5 * 60_000 } = {}) {
    const state = this.load();
    const pairingId = crypto.randomUUID();
    const code = crypto.randomInt(100_000, 1_000_000).toString();
    state.pairings[pairingId] = {
      codeHash: digest(code),
      expiresAt: this.clock() + ttlMs,
      attempts: 0,
    };
    this.save(state);
    return { pairingId, code, expiresAt: state.pairings[pairingId].expiresAt };
  }

  consumePairing({ pairingId, code, device }) {
    const state = this.load();
    const pairing = state.pairings[pairingId];
    if (!pairing || pairing.expiresAt < this.clock()) return { ok: false, reason: "expired" };
    pairing.attempts += 1;
    if (pairing.attempts > 5) {
      delete state.pairings[pairingId];
      this.save(state);
      return { ok: false, reason: "locked" };
    }
    if (!safeEqual(pairing.codeHash, digest(code))) {
      this.save(state);
      return { ok: false, reason: "invalid" };
    }

    delete state.pairings[pairingId];
    const token = crypto.randomBytes(32).toString("base64url");
    state.devices[device.id] = {
      id: device.id,
      platform: device.platform,
      name: device.name ?? null,
      tokenHash: digest(token),
      pairedAt: new Date(this.clock()).toISOString(),
      lastSeenAt: null,
      revokedAt: null,
    };
    this.save(state);
    return { ok: true, token };
  }

  authenticate(token) {
    if (!token) return null;
    const state = this.load();
    const tokenHash = digest(token);
    for (const device of Object.values(state.devices)) {
      if (!device.revokedAt && safeEqual(device.tokenHash, tokenHash)) return device;
    }
    return null;
  }

  markSeen(deviceId) {
    const state = this.load();
    if (state.devices[deviceId]) {
      state.devices[deviceId].lastSeenAt = new Date(this.clock()).toISOString();
      this.save(state);
    }
  }

  hasReceipt(deviceId, eventId) {
    return Boolean(this.load().receipts[`${deviceId}:${eventId}`]);
  }

  recordReceipt(deviceId, eventId, runtimeReceipt) {
    const state = this.load();
    state.receipts[`${deviceId}:${eventId}`] = {
      acceptedAt: new Date(this.clock()).toISOString(),
      runtimeReceipt,
    };
    this.save(state);
  }

  listDevices() {
    return Object.values(this.load().devices).map(({ tokenHash, ...device }) => device);
  }

  revoke(deviceId) {
    const state = this.load();
    if (!state.devices[deviceId]) return false;
    state.devices[deviceId].revokedAt = new Date(this.clock()).toISOString();
    this.save(state);
    return true;
  }
}

const digest = (value) => crypto.createHash("sha256").update(String(value)).digest("hex");

function safeEqual(left, right) {
  const a = Buffer.from(left);
  const b = Buffer.from(right);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
