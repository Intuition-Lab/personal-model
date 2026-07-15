#if canImport(HealthKit) && os(iOS)
import Foundation
import HealthKit

@MainActor
public final class AppleHealthConnector {
    private static let pageSize = 500
    private let store: HKHealthStore
    private let client: any HealthEventUploader
    private let anchors: UserDefaults

    public init(
        client: any HealthEventUploader,
        store: HKHealthStore = HKHealthStore(),
        anchors: UserDefaults = .standard
    ) {
        self.client = client
        self.store = store
        self.anchors = anchors
    }

    public func requestAuthorization() async throws {
        guard HKHealthStore.isHealthDataAvailable() else { throw ConnectorError.unavailable }
        try await store.requestAuthorization(toShare: [], read: Set(Self.readTypes))
    }

    public func sync() async throws -> HealthImportResult {
        var totals = HealthImportResult.zero
        for type in Self.readTypes {
            let result = try await synchronizeAnchoredHealthPages(
                initialAnchor: anchor(for: type),
                fetch: { [store] anchor in
                    let page = try await Self.anchoredPage(
                        store: store,
                        type: type,
                        anchor: anchor
                    )
                    return AnchoredHealthPage(
                        events: page.samples.compactMap(Self.normalize),
                        deletedEvents: page.deleted.map {
                            HealthEventDeletion(eventID: $0.uuid.uuidString)
                        },
                        nextAnchor: page.nextAnchor,
                        hasMore: page.samples.count + page.deleted.count >= Self.pageSize
                    )
                },
                upload: { [client] events, deletedEvents in
                    try await client.upload(events: events, deletedEvents: deletedEvents)
                },
                persist: { [weak self] anchor in self?.save(anchor, for: type) }
            )
            totals = totals.adding(result)
        }
        return totals
    }

    private static func anchoredPage(
        store: HKHealthStore,
        type: HKSampleType,
        anchor: HKQueryAnchor?
    ) async throws -> (
        samples: [HKSample],
        deleted: [HKDeletedObject],
        nextAnchor: HKQueryAnchor?
    ) {
        try await withCheckedThrowingContinuation { continuation in
            let query = HKAnchoredObjectQuery(
                type: type,
                predicate: nil,
                anchor: anchor,
                limit: pageSize
            ) { _, samples, deleted, newAnchor, error in
                if let error { continuation.resume(throwing: error) }
                else {
                    continuation.resume(returning: (samples ?? [], deleted ?? [], newAnchor))
                }
            }
            store.execute(query)
        }
    }

    private func anchor(for type: HKSampleType) -> HKQueryAnchor? {
        guard let data = anchors.data(forKey: "persome.health.anchor.\(type.identifier)") else {
            return nil
        }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data)
    }

    private func save(_ anchor: HKQueryAnchor?, for type: HKSampleType) {
        guard let anchor, let data = try? NSKeyedArchiver.archivedData(
            withRootObject: anchor, requiringSecureCoding: true
        ) else { return }
        anchors.set(data, forKey: "persome.health.anchor.\(type.identifier)")
    }

    private static let readTypes: [HKSampleType] = {
        let quantityIDs: [HKQuantityTypeIdentifier] = [
            .stepCount, .heartRate, .restingHeartRate, .activeEnergyBurned,
        ]
        var types: [HKSampleType] = quantityIDs.compactMap(
            HKObjectType.quantityType(forIdentifier:)
        )
        if let sleep = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) { types.append(sleep) }
        types.append(HKObjectType.workoutType())
        return types
    }()

    private static func normalize(_ sample: HKSample) -> HealthEvent? {
        let device = sample.device
        let source = HealthEventSource(
            device: device?.name ?? device?.model,
            deviceID: device?.localIdentifier
        )
        let base = (
            id: sample.uuid.uuidString,
            source: source,
            start: sample.startDate,
            end: sample.endDate,
            timezone: TimeZone.current.identifier,
            metadata: ["healthkit_type": sample.sampleType.identifier]
        )

        if let quantity = sample as? HKQuantitySample,
           let mapping = quantityMapping[quantity.quantityType.identifier] {
            return HealthEvent(
                eventID: base.id, source: base.source, metric: mapping.metric,
                value: .number(quantity.quantity.doubleValue(for: mapping.unit)),
                unit: mapping.label, startedAt: base.start, endedAt: base.end,
                timezone: base.timezone, metadata: base.metadata
            )
        }
        if let sleep = sample as? HKCategorySample {
            return HealthEvent(
                eventID: base.id, source: base.source, metric: "sleep_stage",
                value: .text(String(sleep.value)), unit: "category",
                startedAt: base.start, endedAt: base.end,
                timezone: base.timezone, metadata: base.metadata
            )
        }
        if let workout = sample as? HKWorkout {
            return HealthEvent(
                eventID: base.id, source: base.source, metric: "workout",
                value: .text(String(workout.workoutActivityType.rawValue)), unit: "activity_type",
                startedAt: base.start, endedAt: base.end,
                timezone: base.timezone,
                metadata: base.metadata.merging(["duration_seconds": String(workout.duration)]) { a, _ in a }
            )
        }
        return nil
    }

    private static let quantityMapping: [String: (metric: String, unit: HKUnit, label: String)] = [
        HKQuantityTypeIdentifier.stepCount.rawValue: ("step_count", .count(), "count"),
        HKQuantityTypeIdentifier.heartRate.rawValue: ("heart_rate", .count().unitDivided(by: .minute()), "bpm"),
        HKQuantityTypeIdentifier.restingHeartRate.rawValue: ("resting_heart_rate", .count().unitDivided(by: .minute()), "bpm"),
        HKQuantityTypeIdentifier.activeEnergyBurned.rawValue: ("active_energy", .kilocalorie(), "kcal"),
    ]
}

public enum ConnectorError: Error { case unavailable }
#endif
