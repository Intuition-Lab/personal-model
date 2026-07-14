# Persome Companion

Mobile capture companion for the local-first Persome Runtime.

The first vertical slice is deliberately small:

1. A Share Extension turns owner-selected text or URLs into `MobileEvent`.
2. `EventQueue` persists events locally and survives offline periods.
3. `SyncEngine` delivers in order and acknowledges only successful events.
4. `CompanionClient` sends to a paired Mac bridge at `POST /v1/events` with a
   short-lived device session and an idempotency key.
5. The bridge forwards to the Runtime's loopback-only
   `POST /mobile/events/ingest`; the phone never receives the Runtime bearer.

Run the protocol tests with:

```bash
swift test
```

The full iOS app and Share Extension targets require Xcode. This machine
currently has Apple Command Line Tools only, so the package keeps all protocol,
queue, and sync logic independently buildable while `ShareExtension/` contains
the UIKit adapter ready to add to the Xcode project.

## Paired Mac bridge

The zero-dependency Node bridge exposes TLS 1.3 on the LAN, stores only hashes
of device session tokens, limits a pairing to five guesses, supports revocation,
and forwards accepted events to the Runtime over loopback.

Provide a locally trusted/pinned certificate and key, then:

```bash
npm run bridge -- cert       # one-time self-signed TLS identity

PERSOME_LOCAL_API_TOKEN=... \
npm run bridge -- start

npm run bridge -- pair       # JSON payload for the pairing QR
npm run bridge -- devices
npm run bridge -- revoke DEVICE_ID
```

The QR payload includes the bridge certificate SHA-256 fingerprint. The iOS
client must pin that fingerprint before submitting the one-time code.

`App/` now contains the SwiftUI pairing flow: VisionKit QR scanning, strict
HTTPS/fingerprint validation, a certificate-pinned pairing request, and
ThisDeviceOnly Keychain storage for the resulting device session.

The Share Extension writes directly to an App Group queue shared with the main
app; it does not rely on cross-process notifications. After installing Xcode
and XcodeGen, generate the project with `xcodegen generate`.

The main app drains that queue on launch, foreground activation, manual refresh,
and `BGAppRefreshTask`. It uses the pinned bridge certificate for every event,
acknowledges only successful deliveries, and exposes pending/last-sync state.
