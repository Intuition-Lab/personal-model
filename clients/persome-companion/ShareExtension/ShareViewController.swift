#if canImport(UIKit) && canImport(UniformTypeIdentifiers)
import PersomeCompanionCore
import UIKit
import UniformTypeIdentifiers

/// Minimal Share Extension adapter. The host app supplies the app-group queue
/// URL and stable device identity; this controller only extracts user-selected
/// text/URLs and enqueues them before completing the extension request.
final class ShareViewController: UIViewController {
    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        Task { await captureSharedContent() }
    }

    private func captureSharedContent() async {
        var draft = ShareDraft()
        let items = extensionContext?.inputItems as? [NSExtensionItem] ?? []
        for attachment in items.flatMap({ $0.attachments ?? [] }) {
            if attachment.hasItemConformingToTypeIdentifier(UTType.url.identifier),
               let value = try? await attachment.loadItem(forTypeIdentifier: UTType.url.identifier),
               let url = value as? URL {
                draft.url = url
            } else if attachment.hasItemConformingToTypeIdentifier(UTType.plainText.identifier),
                      let value = try? await attachment.loadItem(
                        forTypeIdentifier: UTType.plainText.identifier
                      ), let text = value as? String {
                draft.text = text
            }
        }

        guard
            let deviceID = UserDefaults(suiteName: CompanionAppGroup.identifier)?
                .string(forKey: CompanionAppGroup.deviceIDKey),
            let container = FileManager.default.containerURL(
                forSecurityApplicationGroupIdentifier: CompanionAppGroup.identifier
            )
        else {
            extensionContext?.cancelRequest(withError: ShareExtensionError.notPaired)
            return
        }
        do {
            let event = try draft.event(
                device: MobileDevice(id: deviceID, platform: .ios, name: UIDevice.current.name),
                sourceApp: nil
            )
            let queue = try EventQueue(fileURL: container.appending(path: "mobile-events.json"))
            try await queue.enqueue(event)
        } catch {
            extensionContext?.cancelRequest(withError: error)
            return
        }
        extensionContext?.completeRequest(returningItems: nil)
    }
}

enum ShareExtensionError: Error {
    case notPaired
}
#endif
