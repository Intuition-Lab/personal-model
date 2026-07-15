// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PersomeAppleHealth",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "PersomeAppleHealth", targets: ["PersomeAppleHealth"]),
    ],
    targets: [
        .target(name: "PersomeAppleHealth"),
        .testTarget(name: "PersomeAppleHealthTests", dependencies: ["PersomeAppleHealth"]),
    ]
)
