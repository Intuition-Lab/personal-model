import Foundation

public struct PairingPayload: Codable, Equatable, Sendable {
    public let version: Int
    public let endpoint: URL
    public let fingerprint: String
    public let pairingID: String
    public let code: String
    public let expiresAt: Date

    public init(
        version: Int = 1,
        endpoint: URL,
        fingerprint: String,
        pairingID: String,
        code: String,
        expiresAt: Date
    ) throws {
        guard version == 1 else { throw PairingError.unsupportedVersion }
        guard endpoint.scheme == "https" else { throw PairingError.insecureEndpoint }
        let normalized = fingerprint.lowercased()
        guard normalized.count == 64, normalized.allSatisfy(\.isHexDigit) else {
            throw PairingError.invalidFingerprint
        }
        guard code.count == 6, code.allSatisfy(\.isNumber) else {
            throw PairingError.invalidCode
        }
        self.version = version
        self.endpoint = endpoint
        self.fingerprint = normalized
        self.pairingID = pairingID
        self.code = code
        self.expiresAt = expiresAt
    }

    public static func decodeQR(_ value: String) throws -> PairingPayload {
        guard let data = value.data(using: .utf8) else { throw PairingError.invalidQR }
        let wire = try JSONDecoder().decode(WirePayload.self, from: data)
        return try PairingPayload(
            version: wire.version,
            endpoint: wire.endpoint,
            fingerprint: wire.fingerprint,
            pairingID: wire.pairingID,
            code: wire.code,
            expiresAt: Date(timeIntervalSince1970: TimeInterval(wire.expiresAt) / 1_000)
        )
    }

    public var isExpired: Bool { expiresAt <= Date() }

    private struct WirePayload: Codable {
        let version: Int
        let endpoint: URL
        let fingerprint: String
        let pairingID: String
        let code: String
        let expiresAt: Int64

        enum CodingKeys: String, CodingKey {
            case version, endpoint, fingerprint, code, expiresAt
            case pairingID = "pairingId"
        }
    }
}

public struct PairingRequest: Codable, Equatable, Sendable {
    public let pairingID: String
    public let code: String
    public let device: MobileDevice

    public init(payload: PairingPayload, device: MobileDevice) {
        pairingID = payload.pairingID
        code = payload.code
        self.device = device
    }

    enum CodingKeys: String, CodingKey {
        case pairingID = "pairing_id"
        case code, device
    }
}

public struct CompanionSession: Codable, Equatable, Sendable {
    public let bridgeURL: URL
    public let certificateFingerprint: String
    public let deviceID: String
    public let sessionToken: String

    public init(
        bridgeURL: URL,
        certificateFingerprint: String,
        deviceID: String,
        sessionToken: String
    ) {
        self.bridgeURL = bridgeURL
        self.certificateFingerprint = certificateFingerprint
        self.deviceID = deviceID
        self.sessionToken = sessionToken
    }
}

public enum PairingError: Error, Equatable {
    case invalidQR
    case unsupportedVersion
    case insecureEndpoint
    case invalidFingerprint
    case invalidCode
    case expired
    case certificateMismatch
    case rejected(statusCode: Int)
    case invalidResponse
}
