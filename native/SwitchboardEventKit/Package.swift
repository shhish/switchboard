// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "SwitchboardEventKit",
  platforms: [.macOS(.v15)],
  products: [.executable(name: "SwitchboardEventKit", targets: ["SwitchboardEventKit"])],
  targets: [
    .target(name: "EventKitBridge", resources: [.copy("Resources/Things.applescript")]),
    .executableTarget(name: "SwitchboardEventKit", dependencies: ["EventKitBridge"]),
    .testTarget(name: "EventKitBridgeTests", dependencies: ["EventKitBridge"]),
  ],
  swiftLanguageModes: [.v6]
)
