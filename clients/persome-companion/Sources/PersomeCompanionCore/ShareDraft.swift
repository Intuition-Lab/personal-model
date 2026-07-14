import Foundation

public struct ShareDraft: Equatable, Sendable {
    public var title: String?
    public var text: String?
    public var url: URL?
    public var note: String?

    public init(title: String? = nil, text: String? = nil, url: URL? = nil, note: String? = nil) {
        self.title = title
        self.text = text
        self.url = url
        self.note = note
    }

    public func event(device: MobileDevice, sourceApp: String?) throws -> MobileEvent {
        let kind: MobileEvent.Kind = url == nil ? .text : .share
        return try MobileEvent(
            device: device,
            kind: kind,
            sourceApp: sourceApp,
            title: title,
            text: text,
            url: url,
            note: note
        )
    }
}
