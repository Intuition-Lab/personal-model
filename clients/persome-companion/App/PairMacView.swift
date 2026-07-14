#if canImport(SwiftUI)
import SwiftUI

struct PairMacView: View {
    @ObservedObject var model: CompanionAppModel
    @State private var showingScanner = false
    @State private var pastedPayload = ""

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            Image(systemName: "person.crop.circle.badge.checkmark")
                .font(.system(size: 64, weight: .light))
            VStack(spacing: 8) {
                Text("Connect your Personal Model").font(.title2.bold())
                Text("Scan the code shown by Persome on your Mac. Your model stays on that Mac.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
            }
            Button("Scan Mac pairing code") { showingScanner = true }
                .buttonStyle(.borderedProminent)
            DisclosureGroup("Enter pairing payload manually") {
                TextEditor(text: $pastedPayload).frame(minHeight: 100)
                Button("Connect") { Task { await model.pair(qrValue: pastedPayload) } }
                    .disabled(pastedPayload.isEmpty)
            }
            if let error = model.lastError {
                Text(error).font(.footnote).foregroundStyle(.red)
            }
            Text(model.connectionState).font(.footnote).foregroundStyle(.secondary)
            Spacer()
        }
        .padding(24)
        .sheet(isPresented: $showingScanner) {
            PairingScannerView { value in
                showingScanner = false
                Task { await model.pair(qrValue: value) }
            }
        }
    }
}
#endif
