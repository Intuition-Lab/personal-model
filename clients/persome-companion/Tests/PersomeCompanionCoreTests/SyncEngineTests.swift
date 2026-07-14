import Foundation
import Testing
@testable import PersomeCompanionCore

private actor RecordingTransport: EventTransport {
    private(set) var ids: [String] = []
    let failOn: String?

    init(failOn: String? = nil) {
        self.failOn = failOn
    }

    func send(_ event: MobileEvent) async throws {
        if event.eventID == failOn { throw URLError(.notConnectedToInternet) }
        ids.append(event.eventID)
    }
}

@Test func syncAcknowledgesOnlyDeliveredEvents() async throws {
    let file = FileManager.default.temporaryDirectory
        .appending(path: UUID().uuidString)
        .appending(path: "queue.json")
    let queue = try EventQueue(fileURL: file)
    for id in ["one", "two", "three"] {
        try await queue.enqueue(
            try MobileEvent(
                eventID: id,
                device: MobileDevice(id: "iphone-1", platform: .ios),
                kind: .text,
                text: id
            )
        )
    }
    let transport = RecordingTransport(failOn: "two")
    let engine = SyncEngine(queue: queue, transport: transport)

    #expect(await engine.flush() == 1)
    #expect(await queue.pending().map(\.eventID) == ["two", "three"])
    #expect(await transport.ids == ["one"])
}
