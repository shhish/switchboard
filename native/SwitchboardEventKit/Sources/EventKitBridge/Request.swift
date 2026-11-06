import Foundation

public enum JSONValue: Codable, Sendable, Equatable {
  case string(String)
  case number(Double)
  case bool(Bool)
  case array([JSONValue])
  case object([String: JSONValue])
  case null

  public init(from decoder: any Decoder) throws {
    let c = try decoder.singleValueContainer()
    if c.decodeNil() {
      self = .null
    } else if let v = try? c.decode(Bool.self) {
      self = .bool(v)
    } else if let v = try? c.decode(String.self) {
      self = .string(v)
    } else if let v = try? c.decode(Double.self), v.isFinite {
      self = .number(v)
    } else if let v = try? c.decode([JSONValue].self) {
      self = .array(v)
    } else {
      self = .object(try c.decode([String: JSONValue].self))
    }
  }

  public func encode(to encoder: any Encoder) throws {
    var c = encoder.singleValueContainer()
    switch self {
    case .string(let v): try c.encode(v)
    case .number(let v): try c.encode(v)
    case .bool(let v): try c.encode(v)
    case .array(let v): try c.encode(v)
    case .object(let v): try c.encode(v)
    case .null: try c.encodeNil()
    }
  }
}

public struct BridgeError: Error, Sendable {
  public let code: String
  public init(_ code: String) { self.code = code }
}

public enum Operation: String, Codable, Sendable {
  case thingsStatus = "things.status"
  case thingsAccess = "things.request_access"
  case thingsLists = "things.lists"
  case thingsSearch = "things.search"
  case thingsProjects = "things.projects"
  case thingsAreas = "things.areas"
  case thingsAdd = "things.add"
  case thingsComplete = "things.complete"
  case thingsUpdate = "things.update"
  case netProbe = "net.probe"
  case netAccess = "net.request_access"
  case remoteSSH = "remote.ssh"
  case notesSearch = "notes.search"
  case notesGet = "notes.get"
  case status
  case requestAccess = "request_access"
  case calendars = "calendars.list"
  case events = "events.list"
  case create = "events.create"
  case updateEvent = "events.update"
  case lists = "reminder_lists.list"
  case reminders = "reminders.list"
  case add = "reminders.add"
  case complete = "reminders.complete"
  case updateReminder = "reminders.update"
}

public struct Request: Sendable {
  public let method: Operation
  public let args: [String: JSONValue]

  public init(data: Data) throws {
    // Reject unknown envelope fields before decoding the typed arguments.
    let raw = try JSONSerialization.jsonObject(with: data)
    guard let envelope = raw as? [String: Any], Set(envelope.keys) == ["method", "args"],
      let name = envelope["method"] as? String, let operation = Operation(rawValue: name),
      envelope["args"] is [String: Any]
    else { throw BridgeError("invalid_arguments") }
    struct Envelope: Decodable { let args: [String: JSONValue] }
    self.method = operation
    self.args = try JSONDecoder().decode(Envelope.self, from: data).args
    try validate()
  }

  public func text(_ key: String) -> String? {
    if case .string(let v) = args[key] { return v }
    return nil
  }
  public func flag(_ key: String) -> Bool {
    if case .bool(let v) = args[key] { return v }
    return false
  }
  public func number(_ key: String) -> Int? {
    if case .number(let v) = args[key], v.rounded() == v, abs(v) < 1_000_000_000 { return Int(v) }
    return nil
  }
  public func strings(_ key: String) -> [String]? {
    guard case .array(let values) = args[key] else { return nil }
    return values.compactMap {
      if case .string(let v) = $0 { return v }
      return nil
    }
  }
  public func date(_ key: String) throws -> Date? {
    guard let value = text(key) else { return nil }
    return try Self.parseDate(value)
  }

  public static func parseDate(_ value: String) throws -> Date {
    let formatter = ISO8601DateFormatter()
    for options: ISO8601DateFormatter.Options in [
      [.withInternetDateTime, .withFractionalSeconds], [.withInternetDateTime],
    ] {
      formatter.formatOptions = options
      if let date = formatter.date(from: value) { return date }
    }
    // Naive timestamps use the system zone, including its DST rules.
    for format in ["yyyy-MM-dd'T'HH:mm:ss.SSS", "yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm"] {
      let local = DateFormatter()
      local.locale = Locale(identifier: "en_US_POSIX")
      local.timeZone = .current
      local.dateFormat = format
      local.isLenient = false
      if let date = local.date(from: value), local.string(from: date) == value { return date }
    }
    throw BridgeError("invalid_arguments")
  }

  public static func parseDue(_ value: String) throws -> Date {
    if value.count != 10 { return try parseDate(value) }
    try validateDay(value)
    return try parseDate(value + "T00:00:00")
  }

  static func validateInterval(start: Date, end: Date) throws {
    guard end > start, end.timeIntervalSince(start) <= 366 * 86400 else {
      throw BridgeError("invalid_arguments")
    }
  }

  private func validate() throws {
    if method == .notesSearch || method == .notesGet {
      try Notes.validate(self)
      return
    }
    if method == .remoteSSH {
      try RemoteSSH.validate(self)
      return
    }
    if method == .netProbe || method == .netAccess {
      guard Set(args.keys) == ["url"], let url = text("url") else {
        throw BridgeError("invalid_arguments")
      }
      _ = try NetProbe.validate(url)
      return
    }
    if method.rawValue.hasPrefix("things.") {
      try validateThings()
      return
    }
    let fields: Set<String>
    let required: Set<String>
    switch method {
    case .status, .requestAccess, .calendars, .lists:
      fields = []
      required = []
    case .events:
      fields = ["start", "end", "calendar_ids", "limit"]
      required = ["start", "end"]
    case .create:
      fields = [
        "calendar_id", "title", "start", "end", "all_day", "location", "notes", "alarms_minutes",
      ]
      required = ["calendar_id", "title", "start", "end"]
    case .updateEvent:
      fields = [
        "event_id", "calendar_ids", "start", "end", "title", "location", "notes", "all_day",
      ]
      required = ["event_id", "calendar_ids"]
    case .reminders:
      fields = ["list_ids", "due_before", "include_completed", "limit"]
      required = []
    case .add:
      fields = ["list_id", "title", "due", "notes", "priority"]
      required = ["list_id", "title"]
    case .complete:
      fields = ["reminder_id", "list_ids"]
      required = ["reminder_id"]
    case .updateReminder:
      fields = ["reminder_id", "list_ids", "title", "due", "notes", "priority"]
      required = ["reminder_id", "list_ids"]
    case .notesSearch, .notesGet, .netProbe, .netAccess, .remoteSSH, .thingsStatus, .thingsAccess,
      .thingsLists,
      .thingsSearch,
      .thingsProjects, .thingsAreas, .thingsAdd, .thingsComplete, .thingsUpdate:
      throw BridgeError("invalid_arguments")
    }
    guard Set(args.keys).isSubset(of: fields), required.isSubset(of: Set(args.keys)) else {
      throw BridgeError("invalid_arguments")
    }
    if method == .updateEvent || method == .updateReminder {
      guard !Set(args.keys).subtracting(required).isEmpty else {
        throw BridgeError("invalid_arguments")
      }
    }
    for (key, value) in args {
      switch key {
      case "limit", "priority":
        guard let n = number(key), (key == "limit" ? 1...200 : 0...9).contains(n) else {
          throw BridgeError("invalid_arguments")
        }
      case "all_day", "include_completed":
        guard case .bool = value else { throw BridgeError("invalid_arguments") }
      case "calendar_ids", "list_ids":
        guard case .array(let values) = value, !values.isEmpty, values.count <= 200,
          let ids = strings(key), ids.count == values.count,
          ids.allSatisfy({ !$0.isEmpty && $0.count <= 1000 })
        else { throw BridgeError("invalid_arguments") }
      case "alarms_minutes":
        guard case .array(let alarms) = value, alarms.count <= 10,
          alarms.allSatisfy({
            if case .number(let n) = $0 { return n.rounded() == n && (0...525600).contains(n) }
            return false
          })
        else { throw BridgeError("invalid_arguments") }
      default:
        if method == .updateReminder && key == "due" && value == .null { continue }
        guard case .string(let text) = value,
          (method == .updateEvent || method == .updateReminder)
            && (key == "notes" || key == "location") && text.isEmpty
            || !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
          text.count
            <= (key == "title"
              ? 200
              : key == "notes"
                ? 2000 : ["start", "end", "due", "due_before"].contains(key) ? 100 : 1000)
        else { throw BridgeError("invalid_arguments") }
      }
    }
    for key in ["start", "end", "due", "due_before"] where args[key] != nil {
      if (method == .updateReminder || method == .add) && key == "due" {
        if let due = text(key) { _ = try Self.parseDue(due) }
      } else {
        _ = try date(key)
      }
    }
    if let start = try date("start"), let end = try date("end") {
      try Self.validateInterval(start: start, end: end)
    }
  }
}

public protocol EventKitBackend: Actor {
  func execute(_ request: Request) async throws -> [String: JSONValue]
}

public enum Router {
  public static func respond(
    _ data: Data, backend: any EventKitBackend, deadline: Double? = nil
  ) async -> Data {
    let response: [String: JSONValue]
    var operation: Operation?
    do {
      guard data.count <= 65536 else { throw BridgeError("invalid_arguments") }
      let request: Request
      do { request = try Request(data: data) } catch { throw BridgeError("invalid_arguments") }
      operation = request.method
      let result = try await ExecutionDeadline.run(
        seconds: deadline ?? ExecutionDeadline.seconds(for: request)
      ) { try await backend.execute(request) }
      response = ["ok": .bool(true), "result": .object(result)]
    } catch {
      let code = (error as? BridgeError)?.code ?? "backend_error"
      let messages = [
        "timeout": "Native operation timed out; inspect before retrying a write",
        "invalid_arguments": "Invalid request arguments",
        "unauthorized": "Full access required; use the local owner CLI to request access",
        "not_found": "Resource not found",
        "resource_denied": "Resource not allowlisted or not writable",
        "backend_error": "Native operation failed; inspect before retrying a write",
        "unsupported": "Things AppleScript cannot create native checklist or Evening items",
      ]
      var message = messages[code] ?? messages["backend_error"]!
      if operation == .updateEvent && code == "resource_denied" {
        message += "; change events with attendees or another organizer in Calendar"
      }
      response = [
        "ok": .bool(false),
        "error": .object([
          "code": .string(code), "message": .string(message),
        ]),
      ]
    }
    return (try? JSONEncoder().encode(response))
      ?? Data(
        "{\"ok\":false,\"error\":{\"code\":\"backend_error\",\"message\":\"Encoding failed\"}}".utf8
      )
  }
}
