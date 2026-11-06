import Darwin
import Foundation
import Synchronization

/// All vault I/O stays in the signed helper. Directory descriptors and O_NOFOLLOW
/// keep traversal inside the opened root, even if a path is replaced during a call.
public enum Notes {
  /// O_NONBLOCK only guards open() against FIFOs. Once the descriptor is known to be a
  /// regular file, reads must block: iCloud "dataless" placeholders return EAGAIN to
  /// non-blocking reads instead of downloading the file's contents.
  /// Background processes don't download iCloud "dataless" placeholders by default;
  /// reads then fail instead of fetching. Opt this process in once, before vault I/O.
  static let materializeDatalessFiles: Void = {
    _ = setiopolicy_np(
      IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_PROCESS,
      IOPOL_MATERIALIZE_DATALESS_FILES_ON)
  }()

  static func blockingReads(_ fd: Int32) {
    let flags = fcntl(fd, F_GETFL)
    if flags >= 0 { _ = fcntl(fd, F_SETFL, flags & ~O_NONBLOCK) }
  }

  static let maxBytes = 1024 * 1024
  private static let workers = Mutex(0)

  static func parts(_ value: String, folder: Bool = false) -> [String]? {
    var value = value
    if folder && value.hasSuffix("/") { value.removeLast() }
    let parts = value.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
    guard !value.isEmpty, value.utf8.count <= 4096, !value.contains("\0"),
      !value.contains("\\"), parts.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }),
      folder || value.hasSuffix(".md")
    else { return nil }
    return parts
  }

  static func validate(_ request: Request) throws {
    let search = request.method == .notesSearch
    let fields: Set<String> =
      search ? ["root", "query", "limit", "exclude"] : ["root", "path", "exclude"]
    guard Set(request.args.keys).isSubset(of: fields),
      let root = request.text("root"), root.hasPrefix("/"),
      parts(String(root.dropFirst()), folder: true) != nil
    else { throw BridgeError("invalid_arguments") }
    if search {
      guard let query = request.text("query"), query.unicodeScalars.count <= 500,
        !query.split(whereSeparator: \.isWhitespace).isEmpty
      else { throw BridgeError("invalid_arguments") }
      if request.args["limit"] != nil {
        guard let limit = request.number("limit"), (1...20).contains(limit) else {
          throw BridgeError("invalid_arguments")
        }
      }
    } else {
      guard let path = request.text("path"), parts(path) != nil else {
        throw BridgeError("invalid_arguments")
      }
    }
    if let exclude = request.args["exclude"] {
      guard case .array(let values) = exclude, values.count <= 100,
        let prefixes = request.strings("exclude"), prefixes.count == values.count,
        prefixes.allSatisfy({ parts($0, folder: true) != nil })
      else { throw BridgeError("invalid_arguments") }
    }
  }

  static func openRoot(_ root: String, home: URL, check: () throws -> Void) throws -> Int32 {
    let base = home.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    let prefix = "/" + base + "/"
    guard root.hasPrefix(prefix),
      let components = parts(String(root.dropFirst(prefix.count)), folder: true)
    else { throw BridgeError("invalid_arguments") }
    var fd = open(home.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else { throw BridgeError("resource_denied") }
    for component in components {
      do { try check() } catch {
        close(fd)
        throw error
      }
      let next = openat(fd, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
      close(fd)
      guard next >= 0 else { throw BridgeError("resource_denied") }
      fd = next
    }
    return fd
  }

  static func excluded(_ path: String, prefixes: [String]) -> Bool {
    prefixes.contains { prefix in
      let folder = prefix.hasSuffix("/") ? String(prefix.dropLast()) : prefix
      return path.lowercased() == folder.lowercased()
        || path.lowercased().hasPrefix(folder.lowercased() + "/")
    }
  }

  static func clipped(_ text: String, _ count: Int) -> String {
    String(text.unicodeScalars.prefix(count))
  }

  static func title(_ content: String, path: String) -> String {
    let heading = content.split(whereSeparator: \.isNewline).first { $0.hasPrefix("# ") }
    let value = heading.map { String($0.dropFirst(2)).trimmingCharacters(in: .whitespaces) }
    return clipped(
      value.flatMap { $0.isEmpty ? nil : $0 }
        ?? URL(fileURLWithPath: path).deletingPathExtension().lastPathComponent, 200)
  }

  static func modified(_ info: stat) -> String {
    let date = Date(
      timeIntervalSince1970: Double(info.st_mtimespec.tv_sec)
        + Double(info.st_mtimespec.tv_nsec) / 1e9)
    return ISO8601DateFormatter().string(from: date)
  }

  static func read(_ fd: Int32, max: Int, check: () throws -> Void) throws -> Data {
    let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
    var data = Data()
    while data.count < max {
      try check()
      let chunk = try handle.read(upToCount: min(65536, max - data.count)) ?? Data()
      if chunk.isEmpty { break }
      data.append(chunk)
    }
    try check()
    return data
  }

  static func matchCount(_ text: String, word: String, check: () throws -> Void) throws -> Int {
    var count = 0
    var start = text.startIndex
    while let range = text.range(of: word, options: .caseInsensitive, range: start..<text.endIndex)
    {
      count += 1
      if count % 256 == 0 { try check() }
      start = range.upperBound
    }
    return count
  }

  static func snippet(_ content: String, words: [String]) -> String {
    let range = words.compactMap { content.range(of: $0, options: .caseInsensitive) }
      .min { $0.lowerBound < $1.lowerBound }
    let index = range?.lowerBound ?? content.startIndex
    let start =
      content.index(index, offsetBy: -60, limitedBy: content.startIndex) ?? content.startIndex
    return clipped(
      String(content[start...]).split(whereSeparator: \.isWhitespace).joined(separator: " "), 240)
  }

  private struct Hit {
    let path: String
    let title: String
    let snippet: String
    let info: stat
    let named: Bool
    let count: Int

    var json: JSONValue {
      .object([
        "path": .string(path), "title": .string(title), "snippet": .string(snippet),
        "modified": .string(Notes.modified(info)),
      ])
    }
  }

  public static func execute(_ request: Request) async throws -> [String: JSONValue] {
    _ = materializeDatalessFiles
    return try await run { check in
      try execute(request, home: FileManager.default.homeDirectoryForCurrentUser, check: check)
    }
  }

  static func run(
    _ operation: @escaping @Sendable (@Sendable () throws -> Void) throws -> [String: JSONValue]
  ) async throws -> [String: JSONValue] {
    // A filesystem read may wait for TCC/iCloud without cooperating with task
    // cancellation. Keep it off Swift's cooperative executor so the deadline
    // always runs, and cap workers still waiting after their caller times out.
    try Task.checkCancellation()
    let admitted = workers.withLock { count in
      guard count < 4 else { return false }
      count += 1
      return true
    }
    guard admitted else { throw BridgeError("timeout") }
    let cancelled = Atomic<Bool>(false)
    let gate = CompletionGate<[String: JSONValue]>()
    return try await withTaskCancellationHandler {
      Thread {
        defer { workers.withLock { $0 -= 1 } }
        do {
          let result = try operation {
            if cancelled.load(ordering: .relaxed) { throw CancellationError() }
          }
          gate.finish(.success(result))
        } catch { gate.finish(.failure(error)) }
      }.start()
      return try await gate.value()
    } onCancel: {
      cancelled.store(true, ordering: .relaxed)
      gate.finish(.failure(CancellationError()))
    }
  }

  // The injectable home/check are used only by offline disposable-vault tests.
  static func execute(
    _ request: Request, home: URL,
    check: @Sendable () throws -> Void = { try Task.checkCancellation() }
  ) throws -> [String: JSONValue] {
    try validate(request)
    try check()
    let root = try openRoot(request.text("root")!, home: home, check: check)
    defer { close(root) }
    let prefixes = request.strings("exclude") ?? []
    if request.method == .notesGet {
      return try get(request.text("path")!, root: root, prefixes: prefixes, check: check)
    }
    let words = Array(
      Set(
        request.text("query")!.lowercased().split(whereSeparator: \.isWhitespace).map(String.init)))
    let limit = request.number("limit") ?? 10
    var visited = 0
    var searched = 0
    var truncated = false
    var hits: [Hit] = []
    func walk(_ fd: Int32, prefix: String, depth: Int) throws {
      try check()
      guard let directory = fdopendir(dup(fd)) else { throw BridgeError("resource_denied") }
      defer { closedir(directory) }
      while let entry = readdir(directory) {
        try check()
        let name = withUnsafePointer(to: &entry.pointee.d_name) {
          $0.withMemoryRebound(to: CChar.self, capacity: Int(entry.pointee.d_namlen) + 1) {
            String(cString: $0)
          }
        }
        if name == "." || name == ".." { continue }
        if visited >= 5000 {
          truncated = true
          break
        }
        visited += 1
        let path = prefix + name
        if excluded(path, prefixes: prefixes) { continue }
        var info = stat()
        guard fstatat(fd, name, &info, AT_SYMLINK_NOFOLLOW) == 0 else { continue }
        if (info.st_mode & S_IFMT) == S_IFDIR {
          if name.hasPrefix(".") { continue }
          if depth >= 12 {
            truncated = true
            continue
          }
          let child = openat(fd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
          guard child >= 0 else {
            truncated = true
            continue
          }
          do { try walk(child, prefix: path + "/", depth: depth + 1) } catch {
            close(child)
            throw error
          }
          close(child)
        } else if (info.st_mode & S_IFMT) == S_IFREG, name.hasSuffix(".md"),
          info.st_size <= maxBytes
        {
          let file = openat(fd, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
          guard file >= 0 else {
            truncated = true
            continue
          }
          defer { close(file) }
          guard fstat(file, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
            info.st_size <= maxBytes
          else { continue }
          Notes.blockingReads(file)
          let data: Data
          do { data = try read(file, max: maxBytes + 1, check: check) } catch is CancellationError {
            throw CancellationError()
          } catch {
            truncated = true
            continue
          }
          guard data.count <= maxBytes, let content = String(data: data, encoding: .utf8) else {
            continue
          }
          searched += 1
          let title = title(content, path: path)
          let text = name + "\n" + content
          guard words.allSatisfy({ text.range(of: $0, options: .caseInsensitive) != nil }) else {
            continue
          }
          let named = words.contains {
            name.range(of: $0, options: .caseInsensitive) != nil
              || title.range(of: $0, options: .caseInsensitive) != nil
          }
          let count = try words.reduce(0) { $0 + (try matchCount(text, word: $1, check: check)) }
          hits.append(
            Hit(
              path: path, title: title, snippet: snippet(content, words: words), info: info,
              named: named, count: count))
          hits.sort {
            if $0.named != $1.named { return $0.named }
            if $0.count != $1.count { return $0.count > $1.count }
            if $0.info.st_mtimespec.tv_sec != $1.info.st_mtimespec.tv_sec {
              return $0.info.st_mtimespec.tv_sec > $1.info.st_mtimespec.tv_sec
            }
            if $0.info.st_mtimespec.tv_nsec != $1.info.st_mtimespec.tv_nsec {
              return $0.info.st_mtimespec.tv_nsec > $1.info.st_mtimespec.tv_nsec
            }
            return $0.path < $1.path
          }
          if hits.count > limit {
            hits.removeLast()
            truncated = true
          }
        }
      }
    }
    try walk(root, prefix: "", depth: 0)
    return [
      "results": .array(hits.map(\.json)), "searched": .number(Double(searched)),
      "truncated": .bool(truncated),
    ]
  }

  private static func get(
    _ path: String, root: Int32, prefixes: [String], check: () throws -> Void
  ) throws -> [String: JSONValue] {
    let components = parts(path)!
    guard !excluded(path, prefixes: prefixes),
      !components.dropLast().contains(where: { $0.hasPrefix(".") })
    else {
      throw BridgeError("resource_denied")
    }
    var parent = dup(root)
    defer { close(parent) }
    for component in components.dropLast() {
      try check()
      let child = openat(parent, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
      guard child >= 0 else { throw BridgeError(errno == ENOENT ? "not_found" : "resource_denied") }
      close(parent)
      parent = child
    }
    let fd = openat(parent, components.last!, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
    guard fd >= 0 else { throw BridgeError(errno == ENOENT ? "not_found" : "resource_denied") }
    defer { close(fd) }
    var info = stat()
    guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG else {
      throw BridgeError("resource_denied")
    }
    Notes.blockingReads(fd)
    // Stream UTF-8, retaining only the response prefix and a bounded heading.
    // Continue until the first heading is known, even if it follows the prefix.
    let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
    var pending = Data()
    var prefix = ""
    var prefixCount = 0
    var heading: String?
    var line = ""
    var lineCount = 0
    var truncated = false
    while true {
      try check()
      let chunk = try handle.read(upToCount: 65536) ?? Data()
      if chunk.isEmpty {
        guard pending.isEmpty else { throw BridgeError("backend_error") }
        if heading == nil && line.hasPrefix("# ") { heading = line }
        break
      }
      pending.append(chunk)
      var end = pending.count
      var decoded = String(data: pending, encoding: .utf8)
      while decoded == nil && pending.count - end < 3 && end > 0 {
        end -= 1
        decoded = String(data: pending.prefix(end), encoding: .utf8)
      }
      guard let decoded else { throw BridgeError("backend_error") }
      pending = Data(pending.dropFirst(end))
      for scalar in decoded.unicodeScalars {
        if prefixCount < 20000 {
          prefix.unicodeScalars.append(scalar)
          prefixCount += 1
        } else {
          truncated = true
        }
        if heading == nil {
          if scalar == "\n" || scalar == "\r" {
            if line.hasPrefix("# ") { heading = line }
            line = ""
            lineCount = 0
          } else if lineCount < 202 {
            line.unicodeScalars.append(scalar)
            lineCount += 1
          }
        }
      }
      if truncated && heading != nil { break }
    }
    try check()
    return [
      "path": .string(path), "title": .string(title(heading ?? "", path: path)),
      "content": .string(prefix), "modified": .string(modified(info)),
      "truncated": .bool(truncated),
    ]
  }
}
