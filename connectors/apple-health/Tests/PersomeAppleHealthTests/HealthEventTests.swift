import Foundation
import Testing
@testable import PersomeAppleHealth

@Test func encodesPersomeContract() throws {
    let event = HealthEvent(
        eventID: "sample-1",
        source: HealthEventSource(device: "Apple Watch", deviceID: "local-watch"),
        metric: "heart_rate",
        value: .number(72),
        unit: "bpm",
        startedAt: try #require(ISO8601DateFormatter().date(from: "2026-07-15T01:30:00Z")),
        endedAt: nil,
        timezone: "Asia/Shanghai",
        metadata: [:]
    )
    let data = try JSONEncoder.persome.encode(HealthEventsImport(events: [event]))
    let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    let events = try #require(object["events"] as? [[String: Any]])

    #expect(object["schema_version"] as? Int == 1)
    #expect(events[0]["event_id"] as? String == "sample-1")
    #expect(events[0]["metric"] as? String == "heart_rate")
    #expect(events[0]["value"] as? Double == 72)
}
