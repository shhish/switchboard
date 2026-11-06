import Darwin
import Foundation
import Synchronization
import Testing

@testable import EventKitBridge

private struct Vault {
  let home: URL
  let root: URL
  init() throws {
    home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
      .resolvingSymlinksInPath()
    root = home.appendingPathComponent("vault")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  }
  func remove() { try? FileManager.default.removeItem(at: home) }
  func put(_ path: String, _ content: String) throws {
    let file = root.appendingPathComponent(path)
    try FileManager.default.createDirectory(
      at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
    try Data(content.utf8).write(to: file)
  }
  func request(_ method: String = "notes.search", _ args: [String: JSONValue] = [:]) throws
    -> Request
  {
    let defaults: [String: JSONValue] =
      method == "notes.search"
      ? ["root": .string(root.path), "query": .string("alpha beta"), "limit": .number(20)]
      : ["root": .string(root.path), "path": .string("note.md")]
    return try Request(
      data: JSONEncoder().encode(
        JSONValue.object([
          "method": .string(method), "args": .object(defaults.merging(args) { _, new in new }),
        ])))
  }
  func execute(_ request: Request) throws -> [String: JSONValue] {
    try Notes.execute(request, home: home)
  }
}

private func paths(_ result: [String: JSONValue]) -> [String] {
  guard case .array(let results) = result["results"] else { return [] }
  return results.compactMap {
    guard case .object(let row) = $0, case .string(let path) = row["path"] else { return nil }
    return path
  }
}

@Test func notesSearchUsesANDRanksNamesCountsAndNewest() throws {
  let vault = try Vault()
  defer { vault.remove() }
  try vault.put("alpha.md", "# Filename match\nbeta")
  try vault.put("heading.md", "# Alpha Beta\nalpha beta alpha beta")
  try vault.put("many.md", "alpha beta alpha beta alpha beta")
  try vault.put("old.md", "alpha beta")
  try vault.put("new.md", "ALPHA BETA")
  try vault.put("wrong.md", "alpha only")
  try FileManager.default.setAttributes(
    [.modificationDate: Date(timeIntervalSince1970: 1)],
    ofItemAtPath: vault.root.appendingPathComponent("old.md").path)
  let result = try vault.execute(vault.request())
  #expect(paths(result) == ["heading.md", "alpha.md", "many.md", "new.md", "old.md"])
  #expect(result["searched"] == .number(6))
  #expect(result["truncated"] == .bool(false))
  let limited = try vault.execute(vault.request("notes.search", ["limit": .number(1)]))
  #expect(paths(limited) == ["heading.md"])
  #expect(limited["truncated"] == .bool(true))
}

@Test func notesExcludeHiddenOversizeInvalidUTF8AndSymlinksBeforeRead() throws {
  let vault = try Vault()
  defer { vault.remove() }
  for path in [
    "note.md", "personal/secret.md", ".hidden/note.md", ".obsidian/note.md", ".trash/note.md",
  ] {
    try vault.put(path, "alpha beta")
  }
  try vault.put("large.md", String(repeating: "x", count: Notes.maxBytes + 1))
  try vault.put("ignored.txt", "alpha beta")
  try Data([0xff]).write(to: vault.root.appendingPathComponent("invalid.md"))
  try Data("alpha beta".utf8).write(to: vault.home.appendingPathComponent("outside.md"))
  try FileManager.default.createSymbolicLink(
    atPath: vault.root.appendingPathComponent("escape.md").path,
    withDestinationPath: vault.home.appendingPathComponent("outside.md").path)
  try FileManager.default.createSymbolicLink(
    atPath: vault.root.appendingPathComponent("alias").path,
    withDestinationPath: vault.root.appendingPathComponent("personal").path)
  let request = try vault.request("notes.search", ["exclude": .array([.string("personal/")])])
  let result = try vault.execute(request)
  #expect(paths(result) == ["note.md"])
  #expect(result["searched"] == .number(1))
  for path in ["personal/secret.md", "escape.md", "alias/secret.md", ".hidden/note.md"] {
    #expect(throws: BridgeError.self) {
      try vault.execute(
        vault.request(
          "notes.get", ["path": .string(path), "exclude": .array([.string("personal/")])]))
    }
  }
  // Even a forbidden file that would block forever must never be opened.
  let fifo = vault.root.appendingPathComponent("personal/block.md").path
  #expect(mkfifo(fifo, 0o600) == 0)
  #expect(paths(try vault.execute(request)) == ["note.md"])
}

@Test func notesGetUTF8TitleContentTruncationAndSnippetBounds() throws {
  let vault = try Vault()
  defer { vault.remove() }
  let content = "intro\n# A heading\n" + String(repeating: "🌱 alpha\n", count: 4000)
  try vault.put("note.md", content)
  let note = try vault.execute(vault.request("notes.get"))
  #expect(note["title"] == .string("A heading"))
  #expect(note["content"] == .string(String(content.unicodeScalars.prefix(20000))))
  #expect(note["truncated"] == .bool(true))
  #expect(note["path"] == .string("note.md"))
  guard case .string(let modified) = note["modified"] else {
    Issue.record("missing time")
    return
  }
  #expect(ISO8601DateFormatter().date(from: modified) != nil)
  let search = try vault.execute(vault.request("notes.search", ["query": .string("ALPHA")]))
  guard case .array(let rows) = search["results"], case .object(let row) = rows.first,
    case .string(let snippet) = row["snippet"]
  else {
    Issue.record("missing snippet")
    return
  }
  #expect(snippet.unicodeScalars.count <= 240 && !snippet.contains("\n"))
  #expect(snippet.contains("alpha"))
  try vault.put("note.md", "plain text")
  let plain = try vault.execute(vault.request("notes.get"))
  #expect(plain["title"] == .string("note"))
  #expect(plain["truncated"] == .bool(false))
  // A heading after the returned content prefix still supplies the title.
  try vault.put("note.md", String(repeating: "a", count: 90000) + "\n# Late heading\n")
  #expect(try vault.execute(vault.request("notes.get"))["title"] == .string("Late heading"))
}

@Test func notesRejectInvalidPathsRootsFieldsAndLimits() throws {
  let vault = try Vault()
  defer { vault.remove() }
  for path in ["../x.md", "/x.md", "a/../x.md", "x.txt", "a//x.md", "./x.md", "x.md\0", "a\\x.md"] {
    #expect(throws: BridgeError.self) { try vault.request("notes.get", ["path": .string(path)]) }
  }
  for extra: [String: JSONValue] in [
    ["query": .string(" ")], ["limit": .number(0)], ["limit": .number(21)],
    ["limit": .bool(true)], ["limit": .number(1.5)], ["extra": .string("x")],
    ["exclude": .array([.string("../")])], ["exclude": .array([.bool(true)])],
    ["root": .string("relative")], ["root": .string(vault.root.path + "/../vault")],
  ] {
    #expect(throws: BridgeError.self) { try vault.request("notes.search", extra) }
  }
  for root in [vault.home.path, "/tmp", vault.home.path + "-sibling/vault"] {
    #expect(throws: BridgeError.self) {
      try vault.execute(vault.request("notes.search", ["root": .string(root)]))
    }
  }
  let alias = vault.home.appendingPathComponent("alias")
  try FileManager.default.createSymbolicLink(
    atPath: alias.path, withDestinationPath: vault.root.path)
  #expect(throws: BridgeError.self) {
    try vault.execute(vault.request("notes.search", ["root": .string(alias.path)]))
  }
  #expect(throws: BridgeError.self) { try vault.execute(vault.request("notes.get")) }
}

@Test func notesBoundDepthAndEntryCountAndUse20SecondDeadline() throws {
  let vault = try Vault()
  defer { vault.remove() }
  let deep = Array(repeating: "folder", count: 12).joined(separator: "/")
  try vault.put(deep + "/yes.md", "alpha beta")
  try vault.put(deep + "/deeper/no.md", "alpha beta")
  let result = try vault.execute(vault.request())
  #expect(paths(result) == [deep + "/yes.md"])
  #expect(result["truncated"] == .bool(true))
  for index in 0..<5001 { try vault.put("file\(index).md", "alpha beta") }
  let bounded = try vault.execute(vault.request())
  #expect(bounded["truncated"] == .bool(true))
  guard case .number(let searched) = bounded["searched"] else {
    Issue.record("Missing searched count")
    return
  }
  #expect(searched > 4900 && searched <= 5000)
  for method in ["notes.search", "notes.get"] {
    #expect(ExecutionDeadline.seconds(for: try vault.request(method)) == 20)
  }
}

@Test func notesCancelledWorkDoesNotContinueScanning() async throws {
  let vault = try Vault()
  defer { vault.remove() }
  let request = try vault.request()
  let task = Task {
    while !Task.isCancelled { await Task.yield() }
    return try Notes.execute(request, home: vault.home)
  }
  task.cancel()
  do {
    _ = try await task.value
    Issue.record("expected cancellation")
  } catch is CancellationError {}
}

@Test func notesDeadlineReturnsWhileFilesystemWorkerIsStillBlocked() async throws {
  let release = DispatchSemaphore(value: 0)
  let stopped = Mutex(false)
  let start = ContinuousClock.now
  defer { release.signal() }
  do {
    _ = try await ExecutionDeadline.run(seconds: 0.03) {
      try await Notes.run { check in
        release.wait()
        defer { stopped.withLock { $0 = true } }
        try check()
        Issue.record("Cancelled worker continued past a blocked read")
        return [:]
      }
    }
    Issue.record("Expected native deadline")
  } catch let error as BridgeError {
    #expect(error.code == "timeout")
  }
  #expect(start.duration(to: .now) < .seconds(1))
  release.signal()
  let until = ContinuousClock.now.advanced(by: .seconds(1))
  while !stopped.withLock({ $0 }) && ContinuousClock.now < until {
    try await Task.sleep(for: .milliseconds(5))
  }
  #expect(stopped.withLock { $0 })
}
