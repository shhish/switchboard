import Foundation
import Testing

@testable import EventKitBridge

private actor FakeBackend: EventKitBackend {
  private var seen: [EventKitBridge.Operation] = []
  func execute(_ request: Request) async throws -> [String: JSONValue] {
    seen.append(request.method)
    return ["operation": .string(request.method.rawValue)]
  }
  func calls() -> [EventKitBridge.Operation] { seen }
}

private func encoded(_ method: String, _ args: [String: JSONValue] = [:]) throws -> Data {
  try JSONEncoder().encode(JSONValue.object(["method": .string(method), "args": .object(args)]))
}

@Test func validOperationsReachAbstractBackend() async throws {
  let backend = FakeBackend()
  let fixtures: [(String, [String: JSONValue])] = [
    ("status", [:]), ("request_access", [:]), ("calendars.list", [:]),
    ("reminder_lists.list", [:]), ("reminders.list", ["limit": .number(200)]),
    (
      "events.list",
      ["start": .string("2026-11-01T12:00:00-05:00"), "end": .string("2026-11-02T12:00:00-05:00")]
    ),
    (
      "events.create",
      [
        "calendar_id": .string("cal1"), "title": .string("Dinner"),
        "start": .string("2026-11-01T18:00:00-05:00"), "end": .string("2026-11-01T19:00:00-05:00"),
        "alarms_minutes": .array([.number(15)]),
      ]
    ),
    (
      "reminders.add",
      [
        "list_id": .string("list1"), "title": .string("Milk"), "priority": .number(9),
        "due": .string("2026-11-01T12:00:00Z"),
      ]
    ),
    ("reminders.complete", ["reminder_id": .string("r1"), "list_ids": .array([.string("list1")])]),
    ("reminders.complete", ["reminder_id": .string("r1")]),
  ]
  for (method, args) in fixtures {
    let response = await Router.respond(try encoded(method, args), backend: backend)
    let value = try JSONDecoder().decode([String: JSONValue].self, from: response)
    #expect(value["ok"] == .bool(true))
  }
  #expect(await backend.calls().count == fixtures.count)
}

@Test func invalidRequestsNeverReachBackend() async throws {
  let backend = FakeBackend()
  let fixtures: [(String, [String: JSONValue])] = [
    ("unknown", [:]), ("status", ["extra": .bool(true)]),
    ("events.list", [:]), ("events.list", ["start": .string("bad"), "end": .string("bad")]),
    ("reminders.list", ["limit": .number(201)]), ("reminders.list", ["limit": .bool(true)]),
    ("reminders.list", ["limit": .number(1.5)]), ("reminders.list", ["limit": .number(0)]),
    ("reminders.list", ["list_ids": .array([])]),
    ("reminders.list", ["list_ids": .array([.bool(true)])]),
    ("reminders.list", ["include_completed": .number(1)]),
    ("reminders.complete", [:]),
    ("reminders.complete", ["reminder_id": .string("r1"), "list_ids": .array([])]),
    (
      "reminders.add",
      ["list_id": .string("l1"), "title": .string(String(repeating: "x", count: 201))]
    ),
    ("reminders.add", ["list_id": .string("l1"), "title": .string(" ")]),
    (
      "reminders.add",
      [
        "list_id": .string("l1"), "title": .string("Milk"),
        "notes": .string(String(repeating: "x", count: 2001)),
      ]
    ),
    (
      "reminders.add",
      ["list_id": .string("l1"), "title": .string("Milk"), "priority": .number(10)]
    ),
  ]
  for (method, args) in fixtures {
    let response = await Router.respond(try encoded(method, args), backend: backend)
    let value = try JSONDecoder().decode([String: JSONValue].self, from: response)
    #expect(value["ok"] == .bool(false))
  }
  for data in [
    Data("[]".utf8), Data("{\"method\":\"status\",\"args\":{},\"extra\":1}".utf8),
    Data(repeating: 32, count: 65537),
  ] {
    let response = await Router.respond(data, backend: backend)
    let value = try JSONDecoder().decode([String: JSONValue].self, from: response)
    #expect(value["ok"] == .bool(false))
  }
  #expect(await backend.calls().isEmpty)
}

@Test func eventDatesAndAlarmsAreValidatedBeforeExecution() async throws {
  let backend = FakeBackend()
  let base: [String: JSONValue] = [
    "calendar_id": .string("cal1"), "title": .string("Dinner"),
    "start": .string("2026-11-01T19:00:00-05:00"), "end": .string("2026-11-01T18:00:00-05:00"),
  ]
  var alarms = base
  alarms["end"] = .string("2026-11-01T20:00:00-05:00")
  alarms["alarms_minutes"] = .array([.number(-1)])
  for args in [base, alarms] {
    let response = await Router.respond(try encoded("events.create", args), backend: backend)
    let value = try JSONDecoder().decode([String: JSONValue].self, from: response)
    #expect(value["ok"] == .bool(false))
  }
  #expect(await backend.calls().isEmpty)
}

@Test func timezoneAndNaiveDates() throws {
  let zoned = try Request.parseDate("2026-11-01T18:00:00-05:00")
  let utc = try Request.parseDate("2026-11-01T23:00:00Z")
  #expect(zoned == utc)
  let naive = try Request.parseDate("2026-11-01T18:00:00")
  let parts = Calendar.current.dateComponents([.hour, .day, .month], from: naive)
  #expect(parts.hour == 18 && parts.day == 1 && parts.month == 11)
  #expect(throws: BridgeError.self) { try Request.parseDate("tomorrow") }
  #expect(throws: BridgeError.self) { try Request.parseDate("2026-02-30T18:00:00") }
}

private actor FailingBackend: EventKitBackend {
  func execute(_ request: Request) async throws -> [String: JSONValue] {
    throw NSError(domain: "PRIVATE_BACKEND_EXCEPTION", code: 1)
  }
}

@Test func errorMessagesNeverExposeBackendExceptions() async throws {
  let response = await Router.respond(try encoded("status"), backend: FailingBackend())
  let text = String(decoding: response, as: UTF8.self)
  #expect(text.contains("PRIVATE_BACKEND_EXCEPTION") == false)
  #expect(text.contains("backend_error"))
}

@Test func updateRequestsValidateEveryFieldBeforeBackend() async throws {
  let backend = FakeBackend()
  let event: [String: JSONValue] = [
    "event_id": .string("e1"), "calendar_ids": .array([.string("cal1")]),
  ]
  let reminder: [String: JSONValue] = [
    "reminder_id": .string("r1"), "list_ids": .array([.string("list1")]),
  ]
  let fixtures: [(String, [String: JSONValue], [[String: JSONValue]], [[String: JSONValue]])] = [
    (
      "events.update", event,
      [
        ["title": .string("Changed")], ["location": .string("")], ["notes": .string("")],
        ["all_day": .bool(false)], ["start": .string("2026-11-01T18:00:00Z")],
        ["end": .string("2026-11-01T19:00:00Z")],
      ],
      [
        [:], ["title": .string(" ")], ["title": .string(String(repeating: "x", count: 201))],
        ["location": .string(String(repeating: "x", count: 1001))],
        ["notes": .string(String(repeating: "x", count: 2001))], ["notes": .null],
        ["all_day": .number(1)], ["start": .string("bad")], ["end": .null],
        ["start": .string("2026-11-01T19:00:00Z"), "end": .string("2026-11-01T18:00:00Z")],
        ["start": .string("2026-11-01T19:00:00Z"), "end": .string("2028-11-01T18:00:00Z")],
        ["calendar_id": .string("override"), "title": .string("x")],
        ["calendar_ids": .array([]), "title": .string("x")],
        ["calendar_ids": .array([.bool(true)]), "title": .string("x")],
        ["event_id": .string(""), "title": .string("x")],
        ["start": .string(String(repeating: "x", count: 101))],
      ]
    ),
    (
      "reminders.update", reminder,
      [
        ["title": .string("Changed")], ["notes": .string("")], ["due": .null],
        ["due": .string("2026-11-01")], ["due": .string("2026-11-01T12:00:00-05:00")],
        ["priority": .number(0)], ["priority": .number(9)],
      ],
      [
        [:], ["title": .string(" ")], ["due": .string("2026-02-30")],
        ["due": .string("2026-1-01")], ["due": .string("tomorrow")], ["due": .bool(false)],
        ["priority": .number(-1)], ["priority": .number(10)], ["priority": .bool(true)],
        ["priority": .number(1.5)], ["notes": .null],
        ["title": .string(String(repeating: "x", count: 201))],
        ["notes": .string(String(repeating: "x", count: 2001))],
        ["list_id": .string("override"), "title": .string("x")],
        ["list_ids": .array([]), "title": .string("x")],
        ["reminder_id": .bool(true), "title": .string("x")],
      ]
    ),
  ]
  var expected = 0
  for (method, base, valid, invalid) in fixtures {
    for extra in valid + invalid {
      let args = base.merging(extra) { _, new in new }
      let response = await Router.respond(try encoded(method, args), backend: backend)
      let value = try JSONDecoder().decode([String: JSONValue].self, from: response)
      let allowed = valid.contains(extra)
      #expect(value["ok"] == .bool(allowed))
      if allowed { expected += 1 }
    }
    for missing in base.keys {
      var args = base.merging(["title": .string("x")]) { _, new in new }
      args.removeValue(forKey: missing)
      #expect(throws: BridgeError.self) { try Request(data: encoded(method, args)) }
    }
  }
  #expect(await backend.calls().count == expected)
}
