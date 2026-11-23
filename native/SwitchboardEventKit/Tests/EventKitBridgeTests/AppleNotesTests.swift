import AppKit
import Carbon
import Foundation
import Testing

@testable import EventKitBridge

private func appleRequest(_ method: String, _ args: [String: JSONValue] = [:]) throws -> Request {
  try Request(
    data: JSONEncoder().encode(
      JSONValue.object([
        "method": .string(method), "args": .object(args),
      ])))
}

@MainActor
private final class FakeNotesScript: AppleNotesScriptRunner {
  var calls: [(String, [JSONValue])] = []
  var denied = false
  func run(_ handler: String, parameters: [JSONValue]) throws -> [String: JSONValue] {
    calls.append((handler, parameters))
    if denied { throw BridgeError("resource_denied") }
    return ["fake": .bool(true)]
  }
}

@Test @MainActor func appleNotesDispatchPreservesParametersAndLockedDenial() throws {
  let runner = FakeNotesScript()
  let backend = AppleNotesBackend(runner: runner)
  #expect(AppleScriptAppleNotesRunner.errorCode(-1743) == "unauthorized")
  #expect(AppleScriptAppleNotesRunner.errorCode(-1744) == "unauthorized")
  #expect(AppleScriptAppleNotesRunner.errorCode(-27001) == "resource_denied")
  #expect(AppleScriptAppleNotesRunner.errorCode(-1712) == "timeout")
  #expect(AppleScriptAppleNotesRunner.errorCode(-99999) == "backend_error")
  let query = "雪 \" & do shell script \"never\" --\n\\"
  _ = try backend.dispatch(appleRequest("apple_notes.search", ["query": .string(query)]))
  #expect(runner.calls[0].0 == "searchNotes")
  #expect(runner.calls[0].1 == [.string(query), .number(10)])
  let id = "x-coredata://store/ICNote/p1"
  runner.denied = true
  do {
    _ = try backend.dispatch(appleRequest("apple_notes.get", ["id": .string(id)]))
    Issue.record("Locked note must be denied")
  } catch let error as BridgeError { #expect(error.code == "resource_denied") }
  #expect(runner.calls[1].0 == "getNote")
  #expect(runner.calls[1].1 == [.string(id)])
  let event = try AppleNotesDescriptors.call(
    "searchNotes", parameters: [.string(query), .number(20)])
  #expect(
    event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.atIndex(1)?.stringValue == query)
  #expect(throws: BridgeError.self) {
    try AppleNotesDescriptors.call("deleteNote", parameters: [])
  }
}

@Test func appleNotesValidationAndDeadlines() throws {
  for (method, args): (String, [String: JSONValue]) in [
    ("apple_notes.delete", [:]), ("apple_notes.status", ["ask": .bool(true)]),
    ("apple_notes.request_access", ["client": .string("owner")]),
    ("apple_notes.search", [:]), ("apple_notes.search", ["query": .string(" \n")]),
    ("apple_notes.search", ["query": .string(String(repeating: "x", count: 501))]),
    ("apple_notes.search", ["query": .string("a"), "limit": .number(21)]),
    ("apple_notes.search", ["query": .string("a"), "limit": .number(0)]),
    ("apple_notes.search", ["query": .string("a"), "limit": .bool(true)]),
    ("apple_notes.get", ["id": .string("not-a-note")]),
    ("apple_notes.get", ["id": .string("x-coredata://a"), "body": .string("extra")]),
  ] { #expect(throws: BridgeError.self) { try appleRequest(method, args) } }
  #expect(try ExecutionDeadline.seconds(for: appleRequest("apple_notes.request_access")) == 180)
  #expect(
    try ExecutionDeadline.seconds(for: appleRequest("apple_notes.search", ["query": .string("a")]))
      == 60)
  #expect(
    try ExecutionDeadline.seconds(
      for: appleRequest("apple_notes.get", ["id": .string("x-coredata://a")])) == 60)
}

@Test @MainActor func appleNotesFixedScriptCompilesAndPureHandlersWork() throws {
  // Compile only: dictionary loading sends no Apple Events to Notes.
  let url = try #require(Bundle.module.url(forResource: "AppleNotes", withExtension: "applescript"))
  let source = try String(contentsOf: url, encoding: .utf8)
  let script = try #require(NSAppleScript(source: source))
  var error: NSDictionary?
  try #require(script.compileAndReturnError(&error), "Fixed Notes resource must compile")
  _ = try AppleScriptAppleNotesRunner()
  // Execute only pure local handlers, never searchNotes/getNote/checkReadable.
  func pure(_ name: String, _ values: [JSONValue], expectedError: Int? = nil) throws
    -> NSAppleEventDescriptor
  {
    let event = NSAppleEventDescriptor(
      eventClass: AEEventClass(kASAppleScriptSuite), eventID: AEEventID(kASSubroutineEvent),
      targetDescriptor: nil, returnID: AEReturnID(kAutoGenerateReturnID),
      transactionID: AETransactionID(kAnyTransactionID))
    event.setParam(NSAppleEventDescriptor(string: name), forKeyword: AEKeyword(keyASSubroutineName))
    event.setParam(
      try ThingsDescriptors.value(.array(values)), forKeyword: AEKeyword(keyDirectObject))
    error = nil
    let result = script.executeAppleEvent(event, error: &error)
    try #require(
      error?[NSAppleScript.errorNumber] as? Int == expectedError,
      "Pure handler: \(name), error: \(String(describing: error))")
    return result
  }
  #expect(
    try pure(
      "matcheswords",
      [.string("HOME"), .string("server"), .array([.string("home"), .string("SERVER")])]
    ).booleanValue)
  #expect(
    try !pure("matcheswords", [.string("home"), .string("server"), .array([.string("missing")])])
      .booleanValue)
  let body = String(repeating: "x", count: 300) + "Server" + String(repeating: "y", count: 300)
  let snippet = try #require(
    pure("notesnippet", [.string(body), .array([.string("SERVER")])]).stringValue)
  #expect(snippet.count <= 240 && snippet.contains("Server"))
  #expect(
    try pure("boundedtext", [.string(String(repeating: "a", count: 20001)), .number(20000)])
      .stringValue?.count == 20000)
  let words = try pure("querywords", [.string(" one\tTWO\nthree ")])
  #expect(words.numberOfItems == 3)
  _ = try pure("checkprotection", [.bool(true)], expectedError: -27001)
  _ = try pure("checkprotection", [.bool(false)])
  _ = try pure("checkfolder", [.string("recently DELETED")], expectedError: -27001)
  _ = try pure("checkfolder", [.string("Homelab")])
  #expect(try pure("notesnippet", [.string(""), .array([.string("word")])]).stringValue == "")
}
