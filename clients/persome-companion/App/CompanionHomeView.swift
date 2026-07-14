#if canImport(SwiftUI)
import SwiftUI

struct CompanionHomeView: View {
    @ObservedObject var model: CompanionAppModel

    var body: some View {
        List {
            Section {
                Label("Connected to your Mac", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                if let host = model.session?.bridgeURL.host {
                    LabeledContent("Bridge", value: host)
                }
                LabeledContent("Waiting", value: "\(model.pendingCount)")
                if let lastSyncedAt = model.lastSyncedAt {
                    LabeledContent("Last synced") {
                        Text(lastSyncedAt, style: .relative)
                    }
                }
            } header: {
                Text("Personal Model")
            }
            Section {
                Text("Use Share → Persome from Safari or any app to add something to your model.")
                Button {
                    Task { await model.syncNow() }
                } label: {
                    if model.isSyncing {
                        Label("Syncing…", systemImage: "arrow.triangle.2.circlepath")
                    } else {
                        Label("Sync now", systemImage: "arrow.clockwise")
                    }
                }
                .disabled(model.isSyncing)
                Text(model.connectionState).font(.footnote).foregroundStyle(.secondary)
                if let error = model.lastError {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
                Button("Disconnect", role: .destructive) { model.disconnect() }
            }
        }
        .navigationTitle("Persome")
    }
}
#endif
