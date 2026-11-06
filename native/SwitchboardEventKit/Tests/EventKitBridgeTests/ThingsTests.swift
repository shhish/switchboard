import AppKit
import Carbon
import Foundation
import Testing

@testable import EventKitBridge

private let taskPolicy: [String: JSONValue] = [
  "allow_all": .bool(false), "projects": .array([.string("p1")]),
  "areas": .array([]), "deny_projects": .array([]), "deny_areas": .array([.string("blocked")]),
]

private func thingsRequest(_ method: String, _ args: [String: JSONValue] = [:]) throws -> Request {
  try Request(
    data: JSONEncoder().encode(
      JSONValue.object([
        "method": .string(method), "args": .object(args),
      ])))
}

@MainActor
private final class RecordingScript: ThingsScriptRunner {
  var calls: [(String, [JSONValue])] = []
  func run(_ handler: String, parameters: [JSONValue]) throws -> [String: JSONValue] {
    calls.append((handler, parameters))
    return ["fake": .bool(true)]
  }
}

@Test @MainActor func thingsDescriptorsPreserveUntrustedData() throws {
  let text = "雪 \" & do shell script \"never\" --\n\\"
  let parameters: [JSONValue] = [.string(text), .array([.string(text)]), .number(50), .bool(false)]
  let event = try ThingsDescriptors.call("searchTodos", parameters: parameters)
  #expect(event.eventClass == AEEventClass(kASAppleScriptSuite))
  #expect(event.eventID == AEEventID(kASSubroutineEvent))
  #expect(
    event.paramDescriptor(forKeyword: AEKeyword(keyASSubroutineName))?.stringValue == "searchtodos")
  let args = try #require(event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject)))
  #expect(args.numberOfItems == 4)
  #expect(args.atIndex(1)?.stringValue == text)
  #expect(args.atIndex(2)?.atIndex(1)?.stringValue == text)
  #expect(args.atIndex(3)?.int32Value == 50)
  #expect(args.atIndex(4)?.booleanValue == false)
  #expect(throws: BridgeError.self) {
    try ThingsDescriptors.call("do shell script", parameters: [])
  }
  #expect(throws: BridgeError.self) { try ThingsDescriptors.value(.object([:])) }
}

@Test @MainActor func thingsDispatchUsesFixedHandlersAndPolicyParameters() throws {
  let runner = RecordingScript()
  let backend = ThingsBackend(runner: runner)
  let fixtures: [(String, [String: JSONValue], String)] = [
    ("things.lists", ["list": .string("Someday"), "limit": .number(200)], "listTodos"),
    ("things.search", ["query": .string("quote\" 雪")], "searchTodos"),
    ("things.projects", [:], "listProjects"), ("things.areas", [:], "listAreas"),
    (
      "things.add",
      taskPolicy.merging([
        "title": .string("Milk"), "when": .string("tomorrow"), "deadline": .string("2026-11-01"),
        "project_id": .string("p1"), "tags": .array([.string("Errand")]), "checklist": .array([]),
      ]) { _, new in new }, "addTodo"
    ),
    (
      "things.complete", taskPolicy.merging(["todo_id": .string("t1")]) { _, new in new },
      "completeTodo"
    ),
  ]
  for (method, args, handler) in fixtures {
    #expect(try backend.dispatch(thingsRequest(method, args)) == ["fake": .bool(true)])
    let last = try #require(runner.calls.last)
    #expect(last.0 == handler)
  }
  let add = runner.calls[4].1
  #expect(
    add == [
      .string("Milk"), .string(""), .string("tomorrow"), .string("2026-11-01"),
      .string("p1"), .string(""), .array([.string("Errand")]), .array([]),
      .bool(false), .array([.string("p1")]), .array([]), .array([]), .array([.string("blocked")]),
    ])
  #expect(
    runner.calls[5].1 == [
      .string("t1"), .bool(false), .array([.string("p1")]), .array([]), .array([]),
      .array([.string("blocked")]),
    ])
}

@Test func thingsRequestsRejectInvalidArguments() throws {
  let invalid: [(String, [String: JSONValue])] = [
    ("things.delete", [:]), ("things.status", ["ask": .bool(true)]),
    ("things.request_access", ["client": .string("owner")]),
    ("things.lists", ["list": .string("Logbook")]), ("things.lists", [:]),
    ("things.lists", ["list": .string("Today"), "limit": .number(201)]),
    ("things.search", ["query": .string("Milk"), "limit": .number(51)]),
    ("things.search", ["query": .string("Milk"), "limit": .bool(true)]),
    ("things.search", ["query": .string(String(repeating: "x", count: 201))]),
    ("things.complete", ["todo_id": .string("t1")]),
    ("things.add", ["title": .string("Milk")]),
  ]
  for (method, args) in invalid {
    #expect(throws: BridgeError.self) { try thingsRequest(method, args) }
  }
  for extra: [String: JSONValue] in [
    ["title": .string(" ")], ["when": .string("2026-02-30")], ["when": .string("next week")],
    ["deadline": .string("2026-1-01")], ["project_id": .string("p1"), "area_id": .string("a1")],
    ["tags": .array(Array(repeating: .string("x"), count: 11))],
    ["tags": .array([.string("x,y")])], ["checklist": .array([.string("a\nb")])],
    ["checklist": .array(Array(repeating: .string("x"), count: 21))],
    ["allow_all": .string("true")],
    ["projects": .array([.bool(true)])],
  ] {
    let args = taskPolicy.merging(["title": .string("Milk")]) { _, new in new }.merging(extra) {
      _, new in new
    }
    #expect(throws: BridgeError.self) { try thingsRequest("things.add", args) }
  }
}

@Test(arguments: ["today", "tonight", "evening", "tomorrow", "anytime", "someday", "2026-11-01"])
func thingsWhenValuesAreTyped(_ value: String) throws {
  let args = taskPolicy.merging(["title": .string("Milk"), "when": .string(value)]) { _, new in new
  }
  #expect(try thingsRequest("things.add", args).text("when") == value)
}

@Test @MainActor func thingsStructuredRecordsDecodeWithoutLocaleDependentDates() throws {
  let fields = NSAppleEventDescriptor.list()
  for (index, value) in ["id", "t1", "area_id", "", "deadline", "2026-11-01"].enumerated() {
    fields.insert(NSAppleEventDescriptor(string: value), at: index + 1)
  }
  let record = NSAppleEventDescriptor.record()
  record.setDescriptor(fields, forKeyword: AEKeyword(keyASUserRecordFields))
  #expect(
    try ThingsDescriptors.json(record)
      == .object([
        "id": .string("t1"), "area_id": .null, "deadline": .string("2026-11-01"),
      ]))
}

@Test @MainActor func fixedThingsScriptCompilesWithoutExecutingIt() throws {
  // Compilation reads the installed dictionary but sends no events to Things.
  _ = try AppleScriptThingsRunner()
}

@Test @MainActor func fixedScriptEnforcesDestinationPolicyWithoutContactingThings() throws {
  let url = try #require(Bundle.module.url(forResource: "Things", withExtension: "applescript"))
  let source = try String(contentsOf: url, encoding: .utf8)
  let script = try #require(NSAppleScript(source: source))
  var compilationError: NSDictionary?
  try #require(script.compileAndReturnError(&compilationError))
  // Only this pure local handler executes. No Things tell block is entered.
  let fixtures: [(String, String, Bool, [String], [String], [String], [String], Bool)] = [
    ("", "", false, [], [], [], [], true),
    ("p1", "a1", false, [], [], [], [], false),
    ("p1", "a1", false, ["p1"], [], [], [], true),
    ("p1", "a1", false, [], ["a1"], [], [], true),
    ("p1", "a1", true, [], [], ["p1"], [], false),
    ("p1", "a1", true, [], [], [], ["a1"], false),
    ("p1", "a1", false, ["p1"], [], [], ["a1"], false),
    ("", "a1", false, [], [], [], [], false),
  ]
  for (pid, aid, all, projects, areas, denyProjects, denyAreas, allowed) in fixtures {
    let event = NSAppleEventDescriptor(
      eventClass: AEEventClass(kASAppleScriptSuite), eventID: AEEventID(kASSubroutineEvent),
      targetDescriptor: nil, returnID: AEReturnID(kAutoGenerateReturnID),
      transactionID: AETransactionID(kAnyTransactionID))
    event.setParam(
      NSAppleEventDescriptor(string: "checkdestination"), forKeyword: AEKeyword(keyASSubroutineName)
    )
    let parameters: [JSONValue] = [
      .string(pid), .string(aid), .bool(all), .array(projects.map(JSONValue.string)),
      .array(areas.map(JSONValue.string)), .array(denyProjects.map(JSONValue.string)),
      .array(denyAreas.map(JSONValue.string)),
    ]
    event.setParam(
      try ThingsDescriptors.value(.array(parameters)), forKeyword: AEKeyword(keyDirectObject))
    var error: NSDictionary?
    _ = script.executeAppleEvent(event, error: &error)
    #expect((error == nil) == allowed)
    if !allowed { #expect(error?[NSAppleScript.errorNumber] as? Int == -27001) }
  }
}

@Test @MainActor func thingsDictionaryRecordLabelsDecodeCorrectly() throws {
  // Fixed terminology only; this script returns literals, sending no app events.
  let script = try #require(
    NSAppleScript(
      source: """
        using terms from application "/Applications/Things3.app"
          return {|id|:"t1", |name|:"Milk", |notes|:"", |status|:"open", |due|:"2026-11-01", |deadline|:"2026-11-01", |activation_date|:"", |project_id|:"p1", |project_name|:"Shopping", |area_id|:"a1", |area_name|:"Home", |tags|:{"Errand"}}
        end using terms from
        """))
  var error: NSDictionary?
  let descriptor = script.executeAndReturnError(&error)
  try #require(error == nil)
  #expect(
    try ThingsDescriptors.json(descriptor)
      == .object([
        "id": .string("t1"), "name": .string("Milk"), "notes": .string(""),
        "status": .string("open"), "due": .string("2026-11-01"), "deadline": .string("2026-11-01"),
        "activation_date": .null, "project_id": .string("p1"), "project_name": .string("Shopping"),
        "area_id": .string("a1"), "area_name": .string("Home"), "tags": .array([.string("Errand")]),
      ]))
}

@Test @MainActor func thingsUpdatesUseOnlyDescriptorsWithPresenceFlags() throws {
  let runner = RecordingScript()
  let backend = ThingsBackend(runner: runner)
  let text = "雪 \" & do shell script \"never\" --\n\\"
  let args = taskPolicy.merging([
    "todo_id": .string("t1"), "title": .string(text), "notes": .string(""),
    "deadline": .null, "tags": .array([]), "area_id": .string("a2"),
  ]) { _, new in new }
  _ = try backend.dispatch(thingsRequest("things.update", args))
  let call = try #require(runner.calls.last)
  #expect(call.0 == "updateTodo")
  #expect(
    call.1 == [
      .string("t1"), .bool(true), .string(text), .bool(true), .string(""),
      .bool(false), .string(""), .bool(true), .string(""), .bool(false), .string(""),
      .bool(true), .string("a2"), .bool(true), .array([]),
      .bool(false), .array([.string("p1")]), .array([]), .array([]), .array([.string("blocked")]),
    ])
  let event = try ThingsDescriptors.call(call.0, parameters: call.1)
  let parameters = try #require(event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject)))
  #expect(parameters.atIndex(3)?.stringValue == text)
  #expect(parameters.atIndex(8)?.booleanValue == true)  // explicit deadline clear
}

@Test func thingsUpdateRevalidatesEveryField() throws {
  let base = taskPolicy.merging(["todo_id": .string("t1")]) { _, new in new }
  for extra: [String: JSONValue] in [
    ["notes": .string("")], ["deadline": .null], ["tags": .array([])],
    ["title": .string("Changed")], ["project_id": .string("p2")], ["area_id": .string("a2")],
    ["when": .string("today")], ["when": .string("tomorrow")], ["when": .string("anytime")],
    ["when": .string("someday")], ["when": .string("2026-11-01")],
    ["deadline": .string("2026-11-01")],
  ] {
    _ = try thingsRequest("things.update", base.merging(extra) { _, new in new })
  }
  for extra: [String: JSONValue] in [
    [:], ["todo_id": .string(""), "title": .string("x")], ["title": .string(" ")],
    ["title": .string(String(repeating: "x", count: 201))],
    ["notes": .string(String(repeating: "x", count: 2001))], ["notes": .null],
    ["when": .string("evening")], ["when": .string("tonight")], ["when": .null],
    ["when": .string("2026-02-30")], ["deadline": .string("")],
    ["deadline": .string("2026-1-01")], ["deadline": .bool(false)],
    ["project_id": .string("p1"), "area_id": .string("a1")],
    ["project_id": .string(" ")], ["area_id": .null], ["checklist": .array([])],
    ["tags": .array([.string("a,b")])], ["tags": .array([.string("a\nb")])],
    ["tags": .array([.string(" ")])], ["tags": .array([.bool(true)])],
    ["tags": .array(Array(repeating: .string("x"), count: 11))],
    ["allow_all": .string("true"), "title": .string("x")],
  ] {
    #expect(throws: BridgeError.self) {
      try thingsRequest("things.update", base.merging(extra) { _, new in new })
    }
  }
  for key in base.keys {
    var args = base.merging(["title": .string("x")]) { _, new in new }
    args.removeValue(forKey: key)
    #expect(throws: BridgeError.self) { try thingsRequest("things.update", args) }
  }
}
