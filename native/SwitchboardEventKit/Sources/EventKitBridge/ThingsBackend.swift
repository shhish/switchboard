import AppKit
import Carbon
import Foundation

// NSAppleScript is main-thread-only. Descriptors and the compiled script stay
// here; only Sendable JSON snapshots cross into the socket backend actor.
@MainActor
public protocol ThingsScriptRunner {
  func run(_ handler: String, parameters: [JSONValue]) throws -> [String: JSONValue]
}

@MainActor
public enum ThingsDescriptors {
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
      [
        "listTodos", "searchTodos", "listProjects", "listAreas", "addTodo", "completeTodo",
        "updateTodo",
      ].contains(
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

  static func json(_ descriptor: NSAppleEventDescriptor) throws -> JSONValue {
    if descriptor.descriptorType == typeAERecord {
      guard let fields = descriptor.forKeyword(AEKeyword(keyASUserRecordFields)),
        fields.numberOfItems % 2 == 0
      else {
        throw BridgeError("backend_error")
      }
      var record: [String: JSONValue] = [:]
      for index in stride(from: 1, through: fields.numberOfItems, by: 2) {
        guard let key = fields.atIndex(index)?.stringValue, let value = fields.atIndex(index + 1)
        else {
          throw BridgeError("backend_error")
        }
        let decoded = try json(value)
        if [
          "project_id", "project_name", "area_id", "area_name", "due", "deadline",
          "activation_date",
        ].contains(key), decoded == .string("") {
          record[key] = .null
        } else {
          record[key] = decoded
        }
      }
      return .object(record)
    }
    if descriptor.descriptorType == typeAEList {
      var values: [JSONValue] = []
      for index in 0..<descriptor.numberOfItems {
        guard let item = descriptor.atIndex(index + 1) else { throw BridgeError("backend_error") }
        values.append(try json(item))
      }
      return .array(values)
    }
    if [DescType(typeBoolean), DescType(typeTrue), DescType(typeFalse)].contains(
      descriptor.descriptorType)
    {
      return .bool(descriptor.booleanValue)
    }
    guard let text = descriptor.stringValue else { throw BridgeError("backend_error") }
    return .string(text)
  }
}

@MainActor
public final class AppleScriptThingsRunner: ThingsScriptRunner {
  private let script: NSAppleScript

  public init() throws {
    var error: NSDictionary?
    if let compiled = Bundle.main.url(forResource: "Things", withExtension: "scpt") {
      guard let script = NSAppleScript(contentsOf: compiled, error: &error) else {
        throw BridgeError("backend_error")
      }
      self.script = script
      return
    }
    // SwiftPM build/test has the same fixed source resource. Packaged apps use
    // the precompiled script produced by build-eventkit.sh.
    guard let url = Bundle.module.url(forResource: "Things", withExtension: "applescript"),
      let source = try? String(contentsOf: url, encoding: .utf8),
      let script = NSAppleScript(source: source)
    else { throw BridgeError("backend_error") }
    // Compile this immutable resource once. No caller data enters source code.
    guard script.compileAndReturnError(&error) else { throw BridgeError("backend_error") }
    self.script = script
  }

  public func run(_ handler: String, parameters: [JSONValue]) throws -> [String: JSONValue] {
    let event = try ThingsDescriptors.call(handler, parameters: parameters)
    try Task.checkCancellation()
    // Bound synchronous Apple Event waits too; NSAppleScript has no cancellation API.
    event.setAttribute(
      NSAppleEventDescriptor(int32: 10 * 60), forKeyword: AEKeyword(keyTimeoutAttr))
    var error: NSDictionary?
    let response = script.executeAppleEvent(event, error: &error)
    if let error {
      let code = error[NSAppleScript.errorNumber] as? Int
      throw BridgeError(
        [
          -1743: "unauthorized", -1744: "unauthorized", -27001: "resource_denied",
          -27002: "not_found", -27003: "invalid_arguments", -27004: "unsupported",
        ][code ?? 0] ?? "backend_error")
    }
    guard case .object(let result) = try ThingsDescriptors.json(response) else {
      throw BridgeError("backend_error")
    }
    return result
  }
}

@MainActor
public final class ThingsBackend {
  private var runner: (any ThingsScriptRunner)?
  private static let bundleID = "com.culturedcode.ThingsMac"
  public nonisolated init() {}
  public init(runner: any ThingsScriptRunner) { self.runner = runner }

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
        at: URL(fileURLWithPath: "/Applications/Things3.app"), configuration: config)
    } catch { throw BridgeError("backend_error") }
  }

  // Separate deterministic dispatch from launch/TCC so tests never contact Things.
  public func dispatch(_ request: Request) throws -> [String: JSONValue] {
    if runner == nil { runner = try AppleScriptThingsRunner() }
    let policyKeys = ["allow_all", "projects", "areas", "deny_projects", "deny_areas"]
    let policy = policyKeys.compactMap { request.args[$0] }
    let handler: String
    let parameters: [JSONValue]
    switch request.method {
    case .thingsLists:
      handler = "listTodos"
      parameters = [request.args["list"]!, request.args["limit"] ?? .number(50)]
    case .thingsSearch:
      handler = "searchTodos"
      parameters = [request.args["query"]!, request.args["limit"] ?? .number(20)]
    case .thingsProjects:
      handler = "listProjects"
      parameters = []
    case .thingsAreas:
      handler = "listAreas"
      parameters = []
    case .thingsAdd:
      handler = "addTodo"
      parameters =
        ["title", "notes", "when", "deadline", "project_id", "area_id"].map {
          request.args[$0] ?? .string("")
        } + [request.args["tags"] ?? .array([]), request.args["checklist"] ?? .array([])] + policy
    case .thingsComplete:
      handler = "completeTodo"
      parameters = [request.args["todo_id"]!] + policy
    case .thingsUpdate:
      handler = "updateTodo"
      // Presence flags distinguish omission from clearing notes, deadline or tags.
      let keys = ["title", "notes", "when", "deadline", "project_id", "area_id", "tags"]
      parameters =
        [request.args["todo_id"]!]
        + keys.flatMap { key -> [JSONValue] in
          let value = request.args[key]
          return [
            .bool(value != nil),
            value == nil || value == .null
              ? (key == "tags" ? .array([]) : .string("")) : value!,
          ]
        } + policy
    default: throw BridgeError("invalid_arguments")
    }
    return try runner!.run(handler, parameters: parameters)
  }

  public func execute(_ request: Request) async throws -> [String: JSONValue] {
    try Task.checkCancellation()
    if request.method == .thingsStatus {
      return ["running": .bool(running()), "automation": .string(permission(ask: false))]
    }
    try await launch()
    if request.method == .thingsAccess {
      return ["running": .bool(running()), "automation": .string(permission(ask: true))]
    }
    guard permission(ask: false) == "authorized" else { throw BridgeError("unauthorized") }
    try Task.checkCancellation()
    var result = try dispatch(request)
    result["timezone"] = .string(TimeZone.current.identifier)
    result["timezone_note"] = .string(
      "Things dates use the system timezone: \(TimeZone.current.identifier)")
    return result
  }
}
