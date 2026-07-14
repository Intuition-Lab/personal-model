#if canImport(SwiftUI) && canImport(Security)
import CryptoKit
import BackgroundTasks
import Foundation
import PersomeCompanionCore
import Security
import SwiftUI
import UIKit

@MainActor
final class CompanionAppModel: ObservableObject {
    @Published var session: CompanionSession?
    @Published var connectionState = "Not connected"
    @Published var lastError: String?
    @Published var pendingCount = 0
    @Published var lastSyncedAt: Date?
    @Published var isSyncing = false

    private let keychain = SessionKeychain()
    private var backgroundRegistered = false

    init() {
        session = try? keychain.load()
        if session != nil { connectionState = "Connected to your Mac" }
    }

    func pair(qrValue: String) async {
        do {
            let payload = try PairingPayload.decodeQR(qrValue)
            guard !payload.isExpired else { throw PairingError.expired }
            let device = MobileDevice(
                id: UIDevice.current.identifierForVendor?.uuidString ?? UUID().uuidString,
                platform: .ios,
                name: UIDevice.current.name
            )
            connectionState = "Verifying your Mac…"
            let urlSession = pinnedSession(fingerprint: payload.fingerprint)
            var request = URLRequest(url: payload.endpoint.appending(path: "v1/pair"))
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder.persome().encode(
                PairingRequest(payload: payload, device: device)
            )
            let (data, response) = try await urlSession.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw PairingError.invalidResponse
            }
            guard http.statusCode == 201 else {
                throw PairingError.rejected(statusCode: http.statusCode)
            }
            let result = try JSONDecoder().decode(PairingResponse.self, from: data)
            let paired = CompanionSession(
                bridgeURL: payload.endpoint,
                certificateFingerprint: payload.fingerprint,
                deviceID: result.deviceID,
                sessionToken: result.sessionToken
            )
            try keychain.save(paired)
            UserDefaults(suiteName: CompanionAppGroup.identifier)?.set(
                result.deviceID,
                forKey: CompanionAppGroup.deviceIDKey
            )
            session = paired
            connectionState = "Connected to your Mac"
            lastError = nil
            await syncNow()
        } catch {
            connectionState = "Not connected"
            lastError = String(describing: error)
        }
    }

    func disconnect() {
        try? keychain.delete()
        UserDefaults(suiteName: CompanionAppGroup.identifier)?.removeObject(
            forKey: CompanionAppGroup.deviceIDKey
        )
        session = nil
        connectionState = "Not connected"
        pendingCount = 0
    }

    func activate() async {
        registerBackgroundSync()
        await refreshPendingCount()
        await syncNow()
    }

    func syncNow() async {
        guard let session, !isSyncing else { return }
        isSyncing = true
        defer { isSyncing = false }
        do {
            let queue = try sharedQueue()
            pendingCount = await queue.count()
            guard pendingCount > 0 else {
                connectionState = "Connected to your Mac"
                return
            }
            connectionState = "Sending \(pendingCount) item\(pendingCount == 1 ? "" : "s")…"
            let client = CompanionClient(
                configuration: CompanionConfiguration(
                    bridgeURL: session.bridgeURL,
                    sessionToken: session.sessionToken
                ),
                session: pinnedSession(fingerprint: session.certificateFingerprint)
            )
            let engine = SyncEngine(queue: queue, transport: client)
            _ = await engine.flush()
            pendingCount = await queue.count()
            lastSyncedAt = Date()
            connectionState = pendingCount == 0
                ? "Everything is in your Personal Model"
                : "\(pendingCount) item\(pendingCount == 1 ? "" : "s") waiting"
            lastError = nil
        } catch {
            connectionState = "Waiting for your Mac"
            lastError = String(describing: error)
        }
    }

    func refreshPendingCount() async {
        pendingCount = (try? await sharedQueue().count()) ?? 0
    }

    func scheduleBackgroundSync() {
        guard session != nil else { return }
        let request = BGAppRefreshTaskRequest(identifier: BackgroundSync.identifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    private func registerBackgroundSync() {
        guard !backgroundRegistered else { return }
        backgroundRegistered = true
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: BackgroundSync.identifier,
            using: nil
        ) { [weak self] task in
            guard let refreshTask = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            self?.scheduleBackgroundSync()
            let operation = Task { @MainActor [weak self] in
                await self?.syncNow()
                refreshTask.setTaskCompleted(success: true)
            }
            refreshTask.expirationHandler = { operation.cancel() }
        }
    }

    private func sharedQueue() throws -> EventQueue {
        guard let container = FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier: CompanionAppGroup.identifier
        ) else {
            throw CompanionAppError.missingAppGroup
        }
        return try EventQueue(fileURL: container.appending(path: "mobile-events.json"))
    }
}

private struct PairingResponse: Codable {
    let sessionToken: String
    let deviceID: String

    enum CodingKeys: String, CodingKey {
        case sessionToken = "session_token"
        case deviceID = "device_id"
    }
}

final class CertificatePinningDelegate: NSObject, URLSessionDelegate, @unchecked Sendable {
    private let expectedFingerprint: String

    init(expectedFingerprint: String) {
        self.expectedFingerprint = expectedFingerprint
    }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard
            challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
            let trust = challenge.protectionSpace.serverTrust,
            let certificate = SecTrustGetCertificateAtIndex(trust, 0)
        else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let digest = SHA256.hash(data: SecCertificateCopyData(certificate) as Data)
            .map { String(format: "%02x", $0) }
            .joined()
        guard digest == expectedFingerprint else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

private enum BackgroundSync {
    static let identifier = "app.persome.companion.sync"
}

enum CompanionAppError: Error {
    case missingAppGroup
}

func pinnedSession(fingerprint: String) -> URLSession {
    URLSession(
        configuration: .ephemeral,
        delegate: CertificatePinningDelegate(expectedFingerprint: fingerprint),
        delegateQueue: nil
    )
}
#endif
