# Persome Apple Health Connector

This Swift package is the iPhone-side bridge from Apple HealthKit (including
Apple Watch observations synchronized to the phone) to the owner-local Persome
Runtime. It requests read-only access, performs anchored incremental queries,
normalizes samples, and uploads batches to `POST /health-events/import`.

## Embed in an iPhone app

1. Add this directory as a local Swift package in Xcode.
2. Enable the HealthKit capability for the app target.
3. Add `NSHealthShareUsageDescription` to the app's `Info.plist`, explaining
   that selected observations are sent only to the owner's local Persome Runtime.
4. Create the connector after the user supplies the Runtime URL and local bearer
   token, then authorize and sync:

```swift
let client = PersomeHealthClient(
    runtimeURL: URL(string: "http://192.168.1.10:8742")!,
    bearerToken: ownerToken
)
let connector = AppleHealthConnector(client: client)
try await connector.requestAuthorization()
let result = try await connector.sync()
```

The current Runtime defaults to loopback-only access. An iPhone cannot reach
`127.0.0.1` on the Mac; production pairing therefore needs a separately reviewed
LAN/TLS transport or a phone-to-Mac relay. Do not expose the owner token or an
unencrypted health endpoint to a public network.

The first slice reads steps, heart rate, resting heart rate, active energy,
sleep analysis, and workouts. Anchors are advanced only after all batches for a
HealthKit type have uploaded successfully, so interrupted syncs safely replay
and rely on server-side idempotency.
