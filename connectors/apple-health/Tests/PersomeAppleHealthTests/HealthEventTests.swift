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
    let data = try JSONEncoder.persome.encode(
        HealthEventsImport(
            events: [event],
            deletedEvents: [HealthEventDeletion(eventID: "deleted-1")]
        )
    )
    let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    let events = try #require(object["events"] as? [[String: Any]])

    #expect(object["schema_version"] as? Int == 1)
    #expect(events[0]["event_id"] as? String == "sample-1")
    #expect(events[0]["metric"] as? String == "heart_rate")
    #expect(events[0]["value"] as? Double == 72)
    let deletedEvents = try #require(object["deleted_events"] as? [[String: Any]])
    #expect(deletedEvents[0]["event_id"] as? String == "deleted-1")
    #expect(deletedEvents[0]["provider"] as? String == "apple_health")
}

@Test func aggregatesCorrectionsAndDeletionsAcrossBoundedPages() async throws {
    let result = HealthImportResult(
        schemaVersion: 1,
        received: 1,
        inserted: 0,
        corrected: 1,
        duplicates: 0,
        deleted: 1
    )
    var fetchedAnchors: [String?] = []
    var persistedAnchors: [String?] = []
    var uploadCount = 0

    let totals = try await synchronizeAnchoredHealthPages(
        initialAnchor: nil as String?,
        fetch: { anchor in
            fetchedAnchors.append(anchor)
            if anchor == nil {
                return AnchoredHealthPage(
                    events: [],
                    deletedEvents: [HealthEventDeletion(eventID: "deleted-1")],
                    nextAnchor: "page-1",
                    hasMore: true
                )
            }
            return AnchoredHealthPage(
                events: [],
                deletedEvents: [HealthEventDeletion(eventID: "deleted-2")],
                nextAnchor: "page-2",
                hasMore: false
            )
        },
        upload: { _, _ in
            uploadCount += 1
            return result
        },
        persist: { persistedAnchors.append($0) }
    )

    #expect(fetchedAnchors.count == 2)
    #expect(persistedAnchors == ["page-1", "page-2"])
    #expect(uploadCount == 2)
    #expect(totals.corrected == 2)
    #expect(totals.deleted == 2)
}

@Test func doesNotAdvanceAnchorWhenUploadFails() async {
    enum ExpectedFailure: Error { case upload }
    var persisted = false

    await #expect(throws: ExpectedFailure.self) {
        _ = try await synchronizeAnchoredHealthPages(
            initialAnchor: nil as String?,
            fetch: { _ in
                AnchoredHealthPage(
                    events: [],
                    deletedEvents: [HealthEventDeletion(eventID: "deleted-1")],
                    nextAnchor: "page-1",
                    hasMore: false
                )
            },
            upload: { _, _ in throw ExpectedFailure.upload },
            persist: { _ in persisted = true }
        )
    }
    #expect(!persisted)
}

@Test func refusesToLoopWhenAFullPageHasNoNextAnchor() async {
    await #expect(throws: HealthSyncError.missingPaginationAnchor) {
        _ = try await synchronizeAnchoredHealthPages(
            initialAnchor: nil as String?,
            fetch: { _ in
                AnchoredHealthPage(
                    events: [],
                    deletedEvents: [HealthEventDeletion(eventID: "deleted-1")],
                    nextAnchor: nil,
                    hasMore: true
                )
            },
            upload: { _, _ in .zero },
            persist: { _ in }
        )
    }
}

@Test func directRuntimeClientAcceptsIPv6Loopback() throws {
    _ = try PersomeHealthClient(
        runtimeURL: try #require(URL(string: "http://[::1]:8742")),
        bearerToken: "test-token"
    )
}
