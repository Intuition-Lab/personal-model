import Foundation
import Testing
@testable import PersomeCompanionCore

@Test func pairingQRDecodesBridgePayload() throws {
    let fingerprint = String(repeating: "a", count: 64)
    let qr = """
    {
      "version": 1,
      "endpoint": "https://192.168.1.8:8744",
      "fingerprint": "\(fingerprint)",
      "pairingId": "pair-1",
      "code": "123456",
      "expiresAt": 1752526800000
    }
    """

    let payload = try PairingPayload.decodeQR(qr)
    #expect(payload.endpoint.absoluteString == "https://192.168.1.8:8744")
    #expect(payload.pairingID == "pair-1")
    #expect(payload.expiresAt == Date(timeIntervalSince1970: 1_752_526_800))

    let request = PairingRequest(
        payload: payload,
        device: MobileDevice(id: "iphone-1", platform: .ios)
    )
    let json = try #require(
        JSONSerialization.jsonObject(with: JSONEncoder.persome().encode(request)) as? [String: Any]
    )
    #expect(json["pairing_id"] as? String == "pair-1")
}

@Test func pairingRejectsUnpinnedOrInsecurePayloads() {
    #expect(throws: PairingError.insecureEndpoint) {
        try PairingPayload(
            endpoint: URL(string: "http://192.168.1.8:8744")!,
            fingerprint: String(repeating: "a", count: 64),
            pairingID: "pair-1",
            code: "123456",
            expiresAt: Date.distantFuture
        )
    }
    #expect(throws: PairingError.invalidFingerprint) {
        try PairingPayload(
            endpoint: URL(string: "https://192.168.1.8:8744")!,
            fingerprint: "not-a-fingerprint",
            pairingID: "pair-1",
            code: "123456",
            expiresAt: Date.distantFuture
        )
    }
}
