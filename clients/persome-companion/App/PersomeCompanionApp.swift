#if canImport(SwiftUI)
import PersomeCompanionCore
import SwiftUI

@main
struct PersomeCompanionApp: App {
    @StateObject private var model = CompanionAppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            NavigationStack {
                if model.session == nil {
                    PairMacView(model: model)
                } else {
                    CompanionHomeView(model: model)
                }
            }
            .task { await model.activate() }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    Task { await model.syncNow() }
                } else if phase == .background {
                    model.scheduleBackgroundSync()
                }
            }
        }
    }
}
#endif
