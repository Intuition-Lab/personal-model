import Foundation

public struct HealthEventSource: Codable, Sendable, Equatable {
    public let provider: String
    public let device: String?
    public let deviceID: String?

    enum CodingKeys: String, CodingKey {
        case provider, device
        case deviceID = "device_id"
    }

    public init(provider: String = "apple_health", device: String?, deviceID: String?) {
        self.provider = provider
        self.device = device
        self.deviceID = deviceID
    }
}

public struct HealthEvent: Codable, Sendable, Equatable {
    public let eventID: String
    public let source: HealthEventSource
    public let metric: String
    public let value: HealthValue
    public let unit: String
    public let startedAt: Date
    public let endedAt: Date?
    public let timezone: String
    public let metadata: [String: String]

    enum CodingKeys: String, CodingKey {
        case source, metric, value, unit, timezone, metadata
        case eventID = "event_id"
        case startedAt = "started_at"
        case endedAt = "ended_at"
    }
}

public enum HealthValue: Codable, Sendable, Equatable {
    case number(Double)
    case text(String)

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let number = try? container.decode(Double.self) {
            self = .number(number)
        } else {
            self = .text(try container.decode(String.self))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case let .number(number): try container.encode(number)
        case let .text(text): try container.encode(text)
        }
    }
}

public struct HealthEventsImport: Codable, Sendable {
    public let schemaVersion = 1
    public let events: [HealthEvent]

    enum CodingKeys: String, CodingKey {
        case events
        case schemaVersion = "schema_version"
    }

    public init(events: [HealthEvent]) {
        self.events = events
    }
}

public struct HealthImportResult: Codable, Sendable, Equatable {
    public let schemaVersion: Int
    public let received: Int
    public let inserted: Int
    public let duplicates: Int

    enum CodingKeys: String, CodingKey {
        case received, inserted, duplicates
        case schemaVersion = "schema_version"
    }
}

struct APIEnvelope<Value: Codable & Sendable>: Codable, Sendable {
    let success: Bool
    let data: Value
}

extension JSONEncoder {
    static var persome: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }
}
