import AppKit
import EventKitBridge
import Foundation

@main
struct EventKitApp {
  @MainActor static func main() {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let server = SocketServer(backend: NativeBackend())
    do {
      try server.start()
      withExtendedLifetime(server) { app.run() }
    } catch {
      // Never print backend exceptions or calendar contents.
      FileHandle.standardError.write(
        Data(
          "EventKit helper could not start; check private socket directory and existing helper.\n"
            .utf8))
      exit(1)
    }
  }
}
