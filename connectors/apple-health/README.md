# Persome Apple Health Connector

This Swift package is the iPhone-side bridge from Apple HealthKit (including
Apple Watch observations synchronized to the phone) to the owner-local Persome
Runtime. It requests read-only access, performs anchored incremental queries,
normalizes samples, and uploads bounded change pages through a
`HealthEventUploader`. Each page contains additions/corrections and HealthKit
deletion receipts; its anchor is persisted only after the entire page succeeds.

## Embed in an iPhone app

1. Add this directory as a local Swift package in Xcode.
2. Enable the HealthKit capability for the app target.
3. Add `NSHealthShareUsageDescription` to the app's `Info.plist`, explaining
   that selected observations are sent only to the owner's local Persome Runtime.
4. Create the connector with an uploader implemented by the host app, then
   authorize and sync:

```swift
let connector = AppleHealthConnector(client: secureRelayClient)
try await connector.requestAuthorization()
let result = try await connector.sync()
```

The Runtime is loopback-only. `PersomeHealthClient` therefore accepts only
`localhost`, `127.0.0.1`, or `::1` and is intended for the Mac relay and tests.
Never place the owner bearer on the phone or expose the Runtime over LAN.

The first slice reads steps, heart rate, resting heart rate, active energy,
sleep analysis, and workouts. Anchored queries use 500-operation pages rather
than materializing an unlimited history. Interrupted syncs safely replay the
last unacknowledged page and rely on server-side idempotency.
