import Foundation

public struct MobileDevice: Codable, Equatable, Sendable {
    public enum Platform: String, Codable, Sendable {
        case ios
        case android
    }

    public let id: String
    public let platform: Platform
    public let name: String?

    public init(id: String, platform: Platform, name: String? = nil) {
        self.id = id
        self.platform = platform
        self.name = name
    }
}

public struct MobileEvent: Codable, Equatable, Identifiable, Sendable {
    public enum Kind: String, Codable, Sendable {
        case share, text, url, voice, photo, file, location, usage
    }

    public enum Sensitivity: String, Codable, Sendable {
        case `private`, sensitive
    }

    public let schemaVersion: Int
    public let eventID: String
    public let capturedAt: Date
    public let device: MobileDevice
    public let kind: Kind
    public let sourceApp: String?
    public let title: String?
    public let text: String?
    public let url: URL?
    public let note: String?
    public let sensitivity: Sensitivity

    public var id: String { eventID }

    public init(
        eventID: String = UUID().uuidString,
        capturedAt: Date = Date(),
        device: MobileDevice,
        kind: Kind,
        sourceApp: String? = nil,
        title: String? = nil,
        text: String? = nil,
        url: URL? = nil,
        note: String? = nil,
        sensitivity: Sensitivity = .private
    ) throws {
        guard [title, text, url?.absoluteString, note]
            .contains(where: { !($0 ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty })
        else {
            throw MobileEventError.emptyContent
        }
        self.schemaVersion = 1
        self.eventID = eventID
        self.capturedAt = capturedAt
        self.device = device
        self.kind = kind
        self.sourceApp = sourceApp
        self.title = title
        self.text = text
        self.url = url
        self.note = note
        self.sensitivity = sensitivity
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case eventID = "event_id"
        case capturedAt = "captured_at"
        case device, kind
        case sourceApp = "source_app"
        case title, text, url, note, sensitivity
    }
}

public enum MobileEventError: Error, Equatable {
    case emptyContent
}

public extension JSONEncoder {
    static func persome() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(
                date.formatted(Date.ISO8601FormatStyle(includingFractionalSeconds: true))
            )
        }
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }
}

public extension JSONDecoder {
    static func persome() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)
            let fractional = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
            guard let date = (try? Date(value, strategy: fractional))
                ?? (try? Date(value, strategy: .iso8601))
            else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "Invalid ISO 8601 date"
                )
            }
            return date
        }
        return decoder
    }
}
