import Foundation

public enum PersomeHealthClientError: Error, Equatable {
    case nonLoopbackRuntime
    case nonHTTPResponse
    case rejected(status: Int, body: String)
}

public protocol HealthEventUploader: Sendable {
    func upload(
        events: [HealthEvent],
        deletedEvents: [HealthEventDeletion]
    ) async throws -> HealthImportResult
}

public extension HealthEventUploader {
    func upload(_ events: [HealthEvent]) async throws -> HealthImportResult {
        try await upload(events: events, deletedEvents: [])
    }
}

public actor PersomeHealthClient: HealthEventUploader {
    private let endpoint: URL
    private let bearerToken: String
    private let session: URLSession

    public init(runtimeURL: URL, bearerToken: String, session: URLSession = .shared) throws {
        guard Self.isLoopback(runtimeURL.host) else {
            throw PersomeHealthClientError.nonLoopbackRuntime
        }
        endpoint = runtimeURL.appending(path: "health-events/import")
        self.bearerToken = bearerToken
        self.session = session
    }

    public func upload(
        events: [HealthEvent],
        deletedEvents: [HealthEventDeletion]
    ) async throws -> HealthImportResult {
        precondition(!events.isEmpty || !deletedEvents.isEmpty)
        precondition(events.count + deletedEvents.count <= 1_000)
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
        request.httpBody = try JSONEncoder.persome.encode(
            HealthEventsImport(events: events, deletedEvents: deletedEvents)
        )

        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw PersomeHealthClientError.nonHTTPResponse
        }
        guard (200 ..< 300).contains(response.statusCode) else {
            throw PersomeHealthClientError.rejected(
                status: response.statusCode,
                body: String(decoding: data.prefix(2_048), as: UTF8.self)
            )
        }
        return try JSONDecoder().decode(APIEnvelope<HealthImportResult>.self, from: data).data
    }

    private static func isLoopback(_ host: String?) -> Bool {
        guard let host else { return false }
        return ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host.lowercased())
    }
}
