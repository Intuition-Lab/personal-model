import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public struct CompanionConfiguration: Sendable {
    public let bridgeURL: URL
    public let sessionToken: String

    public init(bridgeURL: URL, sessionToken: String) {
        self.bridgeURL = bridgeURL
        self.sessionToken = sessionToken
    }
}

public enum CompanionClientError: Error, Equatable {
    case invalidResponse
    case rejected(statusCode: Int)
}

public protocol EventTransport: Sendable {
    func send(_ event: MobileEvent) async throws
}

public struct CompanionClient: EventTransport {
    private let configuration: CompanionConfiguration
    private let session: URLSession

    public init(configuration: CompanionConfiguration, session: URLSession = .shared) {
        self.configuration = configuration
        self.session = session
    }

    public func send(_ event: MobileEvent) async throws {
        let endpoint = configuration.bridgeURL.appending(path: "v1/events")
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(configuration.sessionToken)", forHTTPHeaderField: "Authorization")
        request.setValue(event.eventID, forHTTPHeaderField: "Idempotency-Key")
        request.httpBody = try JSONEncoder.persome().encode(event)

        let (_, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw CompanionClientError.invalidResponse
        }
        guard (200 ..< 300).contains(http.statusCode) else {
            throw CompanionClientError.rejected(statusCode: http.statusCode)
        }
    }
}

public actor SyncEngine {
    private let queue: EventQueue
    private let transport: any EventTransport

    public init(queue: EventQueue, transport: any EventTransport) {
        self.queue = queue
        self.transport = transport
    }

    @discardableResult
    public func flush() async -> Int {
        var sent = 0
        for event in await queue.pending() {
            do {
                try await transport.send(event)
                try await queue.acknowledge(eventID: event.eventID)
                sent += 1
            } catch {
                break
            }
        }
        return sent
    }
}
