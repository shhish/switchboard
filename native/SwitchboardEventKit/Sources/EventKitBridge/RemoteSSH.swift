import Darwin
import Foundation
import Synchronization

/// Runs Switchboard's one fixed SSH command to a configured private host (the Pi agent).
/// It lives in the helper because macOS Local Network privacy blocks ssh started by the
/// launchd Python service; this app holds the Local Network grant. Never a general
/// command runner: every argument is revalidated here and the argv shape is fixed.
public enum RemoteSSH {
  static let commands: Set<String> = [
    "version", "snapshot", "checks", "status", "logs", "restart", "refresh-updates", "ps5",
    "dns-stats", "dns-pause", "dns-resume",
  ]

  static func token(_ value: Substring) -> Bool {
    (1...40).contains(value.count)
      && value.allSatisfy { ("a"..."z").contains($0) || ("0"..."9").contains($0) || $0 == "_" || $0 == "-" }
  }

  public static func validRequest(_ request: String) -> Bool {
    let parts = request.split(separator: " ", omittingEmptySubsequences: false)
    guard (1...3).contains(parts.count), parts.allSatisfy(token), commands.contains(String(parts[0]))
    else { return false }
    let args = parts.dropFirst()
    switch parts[0] {
    case "version", "snapshot", "checks", "refresh-updates", "dns-stats", "dns-resume":
      return args.isEmpty
    case "dns-pause":
      guard args.count == 1, let minutes = Int(args.first!), (1...120).contains(minutes) else { return false }
      return args.first!.allSatisfy { $0.isASCII && $0.isNumber }
    case "status": return args.count <= 1
    case "restart": return args.count == 1
    case "ps5": return args.count == 1 && ["status", "wake", "standby"].contains(String(args.first!))
    case "logs":
      guard args.count == 2, let n = Int(args.last!), (1...400).contains(n) else { return false }
      return true
    default: return false
    }
  }

  static func validAddress(_ value: String) -> Bool {
    let octets = value.split(separator: ".", omittingEmptySubsequences: false)
    let bytes = octets.compactMap { UInt8($0) }
    guard octets.count == 4, bytes.count == 4,
      zip(octets, bytes).allSatisfy({ String($0.1) == $0.0 })
    else { return false }
    return bytes[0] == 10 || (bytes[0] == 172 && (16...31).contains(bytes[1]))
      || (bytes[0] == 192 && bytes[1] == 168) || (bytes[0] == 100 && (64...127).contains(bytes[1]))
  }

  static func validUser(_ value: String) -> Bool {
    guard let first = value.first, first == "_" || ("a"..."z").contains(first), value.count <= 32
    else { return false }
    return value.allSatisfy { ("a"..."z").contains($0) || ("0"..."9").contains($0) || $0 == "_" || $0 == "-" }
  }

  /// Credentials must be owned 0600 regular files inside Switchboard's config directory.
  static func validPrivateFile(_ path: String) -> Bool {
    let root = FileManager.default.homeDirectoryForCurrentUser.path + "/.config/switchboard/"
    guard path.hasPrefix(root), !path.contains(".."), !path.contains("\0") else { return false }
    var info = stat()
    guard lstat(path, &info) == 0 else { return false }
    return (info.st_mode & S_IFMT) == S_IFREG && (info.st_mode & 0o777) == 0o600
      && info.st_uid == getuid()
  }

  public static func deadline(for request: String) -> Double {
    switch request.split(separator: " ").first {
    case "restart": return 100
    case "refresh-updates": return 190
    case "ps5": return 70
    default: return 20
    }
  }

  public static func validate(_ request: Request) throws {
    guard Set(request.args.keys) == ["host", "user", "request", "key_file", "known_hosts_file"],
      let host = request.text("host"), validAddress(host),
      let user = request.text("user"), validUser(user),
      let line = request.text("request"), validRequest(line),
      let key = request.text("key_file"), validPrivateFile(key),
      let known = request.text("known_hosts_file"), validPrivateFile(known)
    else { throw BridgeError("invalid_arguments") }
  }

  public static func execute(_ request: Request) async throws -> [String: JSONValue] {
    try validate(request)
    let line = request.text("request")!
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
    process.arguments = [
      "-F", "/dev/null", "-i", request.text("key_file")!,
      "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
      "-o", "UserKnownHostsFile=" + request.text("known_hosts_file")!,
      "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
      "-o", "LogLevel=ERROR",
      request.text("user")! + "@" + request.text("host")!, line,
    ]
    process.environment = ["PATH": "/usr/bin:/bin"]
    process.standardInput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice  // remote and ssh stderr never leave the helper
    let pipe = Pipe()
    process.standardOutput = pipe
    let limit = 1024 * 1024
    let output = Mutex(Data())
    let overflow = Atomic<Bool>(false)

    return try await withTaskCancellationHandler {
      try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<[String: JSONValue], any Error>) in
        let reader = Thread {
          let handle = pipe.fileHandleForReading
          while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            let full = output.withLock { data -> Bool in
              data.append(chunk.prefix(max(0, limit - data.count)))
              return data.count >= limit
            }
            if full {
              overflow.store(true, ordering: .relaxed)
              process.terminate()
              break
            }
          }
          process.waitUntilExit()
          if overflow.load(ordering: .relaxed) {
            continuation.resume(throwing: BridgeError("other"))
            return
          }
          let data = output.withLock { $0 }
          continuation.resume(returning: [
            "exit_code": .number(Double(process.terminationStatus)),
            "stdout": .string(String(decoding: data, as: UTF8.self)),
          ])
        }
        do {
          try process.run()
          reader.start()
        } catch {
          continuation.resume(throwing: BridgeError("other"))
        }
      }
    } onCancel: {
      // The execution deadline cancels this task; never leave ssh running behind it.
      if process.isRunning { process.terminate() }
    }
  }
}
