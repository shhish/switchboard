import AppKit
import Carbon
import Foundation

// NSAppleScript is main-thread-only. Descriptors and the compiled script stay
// here; only Sendable JSON snapshots cross into the socket backend actor.
@MainActor
public protocol AppleNotesScriptRunner {
  func run(_ handler: String, parameters: [JSONValue]) throws -> [String: JSONValue]
}

@MainActor
public enum AppleNotesDescriptors {
  public static func value(_ value: JSONValue) throws -> NSAppleEventDescriptor {
    switch value {
    case .string(let text): return NSAppleEventDescriptor(string: text)
    case .bool(let flag): return NSAppleEventDescriptor(boolean: flag)
    case .number(let number):
      guard number.rounded() == number, abs(number) < 1_000_000_000 else {
        throw BridgeError("invalid_arguments")
      }
      return NSAppleEventDescriptor(int32: Int32(number))
    case .array(let values):
      let list = NSAppleEventDescriptor.list()
      for (index, item) in values.enumerated() { list.insert(try self.value(item), at: index + 1) }
      return list
    default: throw BridgeError("invalid_arguments")
    }
  }

  public static func call(_ handler: String, parameters: [JSONValue]) throws
    -> NSAppleEventDescriptor
  {
    guard
      ["searchNotes", "getNote"].contains(
        handler)
    else {
      throw BridgeError("invalid_arguments")
    }
    let event = NSAppleEventDescriptor(
      eventClass: AEEventClass(kASAppleScriptSuite), eventID: AEEventID(kASSubroutineEvent),
      targetDescriptor: nil, returnID: AEReturnID(kAutoGenerateReturnID),
      transactionID: AETransactionID(kAnyTransactionID))
    event.setParam(
      NSAppleEventDescriptor(string: handler.lowercased()),
      forKeyword: AEKeyword(keyASSubroutineName))
    event.setParam(try value(.array(parameters)), forKeyword: AEKeyword(keyDirectObject))
    return event
  }

}

@MainActor
public final class AppleScriptAppleNotesRunner: AppleNotesScriptRunner {
  private let script: NSAppleScript

  public static func errorCode(_ number: Int) -> String {
    [
      -1743: "unauthorized", -1744: "unauthorized", -27001: "resource_denied",
      -27002: "not_found", -27003: "invalid_arguments", -1712: "timeout",
    ][number] ?? "backend_error"
  }

  public init() throws {
    var error: NSDictionary?
    if let compiled = Bundle.main.url(forResource: "AppleNotes", withExtension: "scpt") {
      guard let script = NSAppleScript(contentsOf: compiled, error: &error) else {
        throw BridgeError("backend_error")
      }
      self.script = script
      return
    }
    // SwiftPM build/test has the same fixed source resource. Packaged apps use
    // the precompiled script produced by build-eventkit.sh.
    guard let url = Bundle.module.url(forResource: "AppleNotes", withExtension: "applescript"),
      let source = try? String(contentsOf: url, encoding: .utf8),
      let script = NSAppleScript(source: source)
    else { throw BridgeError("backend_error") }
    // Compile this immutable resource once. No caller data enters source code.
    guard script.compileAndReturnError(&error) else { throw BridgeError("backend_error") }
    self.script = script
  }

  public func run(_ handler: String, parameters: [JSONValue]) throws -> [String: JSONValue] {
    let event = try AppleNotesDescriptors.call(handler, parameters: parameters)
    try Task.checkCancellation()
    // Bound synchronous Apple Event waits too; NSAppleScript has no cancellation API.
    event.setAttribute(
      NSAppleEventDescriptor(int32: 60 * 60), forKeyword: AEKeyword(keyTimeoutAttr))
    var error: NSDictionary?
    let response = script.executeAppleEvent(event, error: &error)
    if let error {
      let code = error[NSAppleScript.errorNumber] as? Int
      throw BridgeError(Self.errorCode(code ?? 0))
    }
    guard case .object(let result) = try ThingsDescriptors.json(response) else {
      throw BridgeError("backend_error")
    }
    return result
  }
}

@MainActor
public final class AppleNotesBackend {
  private var runner: (any AppleNotesScriptRunner)?
  private static let bundleID = "com.apple.Notes"
  public nonisolated init() {}
  public init(runner: any AppleNotesScriptRunner) { self.runner = runner }

  private func permission(ask: Bool) -> String {
    let target = NSAppleEventDescriptor(bundleIdentifier: Self.bundleID)
    let code = AEDeterminePermissionToAutomateTarget(
      target.aeDesc, AEEventClass(typeWildCard), AEEventID(typeWildCard), ask)
    switch code {
    case noErr: return "authorized"
    case OSStatus(errAEEventWouldRequireUserConsent): return "not_determined"
    case OSStatus(errAEEventNotPermitted): return "denied"
    default: return "unavailable"
    }
  }

  private func running() -> Bool {
    !NSRunningApplication.runningApplications(withBundleIdentifier: Self.bundleID).isEmpty
  }

  private func launch() async throws {
    guard !running() else { return }
    let config = NSWorkspace.OpenConfiguration()
    config.activates = false
    config.addsToRecentItems = false
    do {
      _ = try await NSWorkspace.shared.openApplication(
        at: URL(fileURLWithPath: "/System/Applications/Notes.app"), configuration: config)
    } catch { throw BridgeError("backend_error") }
  }

  // Separate deterministic dispatch from launch/TCC so tests never contact Notes.
  public func dispatch(_ request: Request) throws -> [String: JSONValue] {
    if runner == nil { runner = try AppleScriptAppleNotesRunner() }
    let handler: String
    let parameters: [JSONValue]
    switch request.method {
    case .appleNotesSearch:
      handler = "searchNotes"
      parameters = [request.args["query"]!, request.args["limit"] ?? .number(10)]
    case .appleNotesGet:
      handler = "getNote"
      parameters = [request.args["id"]!]
    default: throw BridgeError("invalid_arguments")
    }
    return try runner!.run(handler, parameters: parameters)
  }

  public func execute(_ request: Request) async throws -> [String: JSONValue] {
    try Task.checkCancellation()
    if request.method == .appleNotesStatus {
      return ["running": .bool(running()), "automation": .string(permission(ask: false))]
    }
    try await launch()
    if request.method == .appleNotesAccess {
      return ["running": .bool(running()), "automation": .string(permission(ask: true))]
    }
    guard permission(ask: false) == "authorized" else { throw BridgeError("unauthorized") }
    try Task.checkCancellation()
    return try dispatch(request)
  }
}
