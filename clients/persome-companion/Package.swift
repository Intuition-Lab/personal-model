// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "PersomeCompanion",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "PersomeCompanionCore", targets: ["PersomeCompanionCore"])
    ],
    targets: [
        .target(name: "PersomeCompanionCore"),
        .testTarget(
            name: "PersomeCompanionCoreTests",
            dependencies: ["PersomeCompanionCore"]
        ),
    ]
)
