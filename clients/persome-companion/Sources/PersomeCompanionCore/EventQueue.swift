import Foundation

public actor EventQueue {
    private let fileURL: URL
    private var events: [MobileEvent]

    public init(fileURL: URL) throws {
        self.fileURL = fileURL
        if FileManager.default.fileExists(atPath: fileURL.path) {
            let data = try Data(contentsOf: fileURL)
            events = try JSONDecoder.persome().decode([MobileEvent].self, from: data)
        } else {
            events = []
        }
    }

    public func enqueue(_ event: MobileEvent) throws {
        guard !events.contains(where: { $0.eventID == event.eventID }) else { return }
        events.append(event)
        try persist()
    }

    public func pending() -> [MobileEvent] {
        events
    }

    public func count() -> Int {
        events.count
    }

    public func acknowledge(eventID: String) throws {
        events.removeAll { $0.eventID == eventID }
        try persist()
    }

    private func persist() throws {
        let directory = fileURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let data = try JSONEncoder.persome().encode(events)
        try data.write(to: fileURL, options: [.atomic, .completeFileProtection])
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: fileURL.path
        )
    }
}
