// switchboard-elevate: a root LaunchDaemon that runs only what the owner's iPhone signed.
//
// Trust model
// - Nothing on this Mac can make it run a command: not Switchboard, not an agent, not the
//   owner's own user account. It runs a request only when the request carries a P-256 ECDSA
//   signature from a key in keys.json. Those keys live in an iPhone's Secure Enclave and sign
//   only after Face ID, over the exact bytes the phone showed the owner.
// - keys.json, recipes.json and this binary are root-owned. Changing any of them is itself a
//   signed request (add-key, remove-key, update-recipes, update-helper).
// - A recipe request is re-rendered from the root-owned recipe and must match the signed
//   steps exactly. A custom request runs the signed argv verbatim, without a shell unless the
//   signed argv itself names one.
// - Every nonce runs at most once; payloads expire and name this machine's install id.
//
// Protocol: one JSON object per line over the Unix socket, one JSON reply per line.
//   {"op":"status"}
//   {"op":"run","payload":"<base64 JSON>","signature":"<base64 DER>"}
//   {"op":"result","job":"<nonce>"}

import CryptoKit
import Darwin
import Foundation

let version = "1.0.0"
#if ELEVATE_TEST
// Test builds only (swiftc -D ELEVATE_TEST): everything under one scratch folder, no root.
let testBase = ProcessInfo.processInfo.environment["ELEVATE_TEST_DIR"] ?? "/tmp/elevate-test"
let supportDir = "\(testBase)/support"
let binaryPath = "\(testBase)/helper"
let socketPath = "\(testBase)/elevate.sock"
let logPath = "\(testBase)/elevate.log"
let fileOwner: [FileAttributeKey: Any] = [:]
#else
let supportDir = "/Library/Application Support/SwitchboardElevate"
let binaryPath = "/Library/PrivilegedHelperTools/com.switchboard.elevate"
let socketPath = "/var/run/com.switchboard.elevate.sock"
let logPath = "/Library/Logs/SwitchboardElevate.log"
let fileOwner: [FileAttributeKey: Any] = [.ownerAccountID: 0]
#endif
let maxRequest = 512 * 1024
let maxOutput = 64 * 1024
let maxJobs = 2

// MARK: - Small helpers

func log(_ line: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    let text = "\(stamp) \(line)\n"
    if let handle = FileHandle(forWritingAtPath: logPath) {
        handle.seekToEndOfFile()
        handle.write(text.data(using: .utf8)!)
        handle.closeFile()
    } else {
        FileManager.default.createFile(
            atPath: logPath, contents: text.data(using: .utf8),
            attributes: fileOwner.merging([.posixPermissions: 0o600]) { a, _ in a })
    }
}

func readJSON(_ path: String) -> Any? {
    guard let data = FileManager.default.contents(atPath: path) else { return nil }
    return try? JSONSerialization.jsonObject(with: data)
}

/// Writes root-owned files atomically: temp file in the same directory, fsync, rename.
func writeAtomically(_ data: Data, to path: String, mode: Int) throws {
    let dir = (path as NSString).deletingLastPathComponent
    let temp = "\(dir)/.\(UUID().uuidString).tmp"
    guard FileManager.default.createFile(
        atPath: temp, contents: data, attributes: fileOwner.merging([.posixPermissions: mode]) { a, _ in a })
    else { throw HelperError("write failed") }
    let fd = open(temp, O_RDONLY)
    if fd >= 0 { fsync(fd); close(fd) }
    guard rename(temp, path) == 0 else {
        unlink(temp)
        throw HelperError("rename failed")
    }
    let dirFD = open(dir, O_RDONLY)
    if dirFD >= 0 { fsync(dirFD); close(dirFD) }
}

func keyFingerprint(_ x963: Data) -> String {
    let hex = SHA256.hash(data: x963).prefix(8).map { String(format: "%02x", $0) }.joined()
    return stride(from: 0, to: hex.count, by: 4).map {
        let start = hex.index(hex.startIndex, offsetBy: $0)
        return String(hex[start..<hex.index(start, offsetBy: 4)])
    }.joined(separator: " ")
}

func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

func fullMatch(_ value: String, _ pattern: String) -> Bool {
    guard let regex = try? NSRegularExpression(pattern: "^(?:\(pattern))$") else { return false }
    let range = NSRange(value.startIndex..., in: value)
    return regex.firstMatch(in: value, range: range) != nil
}

struct HelperError: Error {
    let message: String
    init(_ message: String) { self.message = message }
}

// MARK: - Root-owned state

struct Key {
    let name: String
    let x963: Data
    let key: P256.Signing.PublicKey
    var fingerprint: String { keyFingerprint(x963) }
}

struct Arg {
    let name: String
    let label: String
    let pattern: String?
    let choices: [String]?

    /// Patterns may name {home}, the owner's home folder, matched literally.
    func accepts(_ value: String, home: String) -> Bool {
        guard !value.isEmpty, value.count <= 1024, !value.contains("\0"), !value.contains("/../"),
            !value.hasSuffix("/..")
        else { return false }
        if let choices { return choices.contains(value) }
        if let pattern {
            let escaped = NSRegularExpression.escapedPattern(for: home)
            return fullMatch(value, pattern.replacingOccurrences(of: "{home}", with: escaped))
        }
        return false
    }
}

struct Recipe {
    let id: String
    let title: String
    let detail: String
    let args: [Arg]
    let steps: [[String]]
    let timeout: Int
    let destructive: Bool
    let raw: [String: Any]

    /// Fills {arg}, {home} and {owner} tokens in one pass, so a value can never introduce
    /// another token. Every arg must be declared and match its rule.
    func render(_ values: [String: String], home: String, owner: String) throws -> [[String]] {
        guard Set(values.keys) == Set(args.map(\.name)) else { throw HelperError("wrong args") }
        for arg in args where !arg.accepts(values[arg.name]!, home: home) {
            throw HelperError("arg \(arg.name) not allowed")
        }
        // The system's own values always win over anything passed in.
        let fills = ["home": home, "owner": owner].merging(values) { system, _ in system }
        let token = try! NSRegularExpression(pattern: "\\{([a-z][a-z0-9_]{0,30})\\}")
        return try steps.map { argv in
            try argv.map { item in
                var out = ""
                var last = item.startIndex
                for match in token.matches(in: item, range: NSRange(item.startIndex..., in: item)) {
                    let whole = Range(match.range, in: item)!
                    let name = String(item[Range(match.range(at: 1), in: item)!])
                    guard let value = fills[name] else { throw HelperError("recipe names unknown {\(name)}") }
                    out += item[last..<whole.lowerBound] + value
                    last = whole.upperBound
                }
                return out + item[last...]
            }
        }
    }
}

func parseRecipes(_ object: Any?) throws -> [Recipe] {
    guard let root = object as? [String: Any], let list = root["recipes"] as? [[String: Any]] else {
        throw HelperError("recipes.json must hold {\"recipes\": [...]}")
    }
    var seen = Set<String>()
    return try list.map { item in
        guard let id = item["id"] as? String, fullMatch(id, "[a-z][a-z0-9-]{1,40}"),
            !seen.contains(id),
            let title = item["title"] as? String,
            let steps = item["steps"] as? [[String]], !steps.isEmpty, steps.count <= 5,
            steps.allSatisfy({ !$0.isEmpty && $0[0].hasPrefix("/") })
        else { throw HelperError("bad recipe \(item["id"] ?? "?")") }
        seen.insert(id)
        let args = try ((item["args"] as? [[String: Any]]) ?? []).map { arg -> Arg in
            guard let name = arg["name"] as? String, fullMatch(name, "[a-z][a-z0-9_]{0,30}"),
                !["home", "owner"].contains(name)
            else {
                throw HelperError("bad arg in \(id)")
            }
            let pattern = arg["pattern"] as? String
            let choices = arg["choices"] as? [String]
            guard (pattern != nil) != (choices != nil) else { throw HelperError("arg \(name) needs pattern or choices") }
            if let pattern,
                (try? NSRegularExpression(pattern: pattern.replacingOccurrences(of: "{home}", with: "h"))) == nil
            {
                throw HelperError("bad pattern for \(name)")
            }
            return Arg(name: name, label: (arg["label"] as? String) ?? name, pattern: pattern, choices: choices)
        }
        let timeout = min(max((item["timeout"] as? Int) ?? 300, 5), 7200)
        return Recipe(
            id: id, title: title, detail: (item["detail"] as? String) ?? "", args: args,
            steps: steps, timeout: timeout, destructive: (item["destructive"] as? Bool) ?? false,
            raw: item)
    }
}

func parseKeys(_ object: Any?) -> [Key] {
    guard let list = (object as? [String: Any])?["keys"] as? [[String: Any]] else { return [] }
    return list.compactMap { item in
        guard let name = item["name"] as? String, let b64 = item["x963"] as? String,
            let data = Data(base64Encoded: b64),
            let key = try? P256.Signing.PublicKey(x963Representation: data)
        else { return nil }
        return Key(name: name, x963: data, key: key)
    }
}

final class State {
    let queue = DispatchQueue(label: "state")
    var ownerUID: uid_t = 0
    var home = ""
    var owner = ""
    var machine = ""
    var keys: [Key] = []
    var recipes: [Recipe] = []
    var used: [String: Int] = [:]  // nonce -> expiry
    var jobs: [String: [String: Any]] = [:]
    var running = 0

    func load() throws {
        guard let config = readJSON("\(supportDir)/config.json") as? [String: Any],
            let uid = config["owner_uid"] as? Int, let machine = config["machine"] as? String
        else { throw HelperError("config.json missing") }
        ownerUID = uid_t(uid)
        self.machine = machine
        guard let account = getpwuid(ownerUID) else { throw HelperError("owner account missing") }
        home = String(cString: account.pointee.pw_dir)
        owner = String(cString: account.pointee.pw_name)
        keys = parseKeys(readJSON("\(supportDir)/keys.json"))
        recipes = try parseRecipes(readJSON("\(supportDir)/recipes.json"))
        let now = Int(Date().timeIntervalSince1970)
        if let text = try? String(contentsOfFile: "\(supportDir)/used-nonces", encoding: .utf8) {
            for line in text.split(separator: "\n") {
                let parts = line.split(separator: " ")
                if parts.count == 2, let expiry = Int(parts[1]), expiry > now {
                    used[String(parts[0])] = expiry
                }
            }
        }
        try saveNonces()
        // Results outlive their nonces by 30 days at most.
        let results = "\(supportDir)/results"
        for name in (try? FileManager.default.contentsOfDirectory(atPath: results)) ?? [] {
            let path = "\(results)/\(name)"
            if let date = (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date,
                date < Date().addingTimeInterval(-30 * 86400)
            {
                unlink(path)
            }
        }
        chmod(logPath, 0o600)
    }

    func saveNonces() throws {
        let text = used.map { "\($0.key) \($0.value)" }.sorted().joined(separator: "\n")
        try writeAtomically(Data((text + "\n").utf8), to: "\(supportDir)/used-nonces", mode: 0o600)
    }

    func saveKeys() throws {
        let list = keys.map { ["name": $0.name, "x963": $0.x963.base64EncodedString()] }
        let data = try JSONSerialization.data(
            withJSONObject: ["keys": list], options: [.prettyPrinted, .sortedKeys])
        try writeAtomically(data, to: "\(supportDir)/keys.json", mode: 0o644)
    }

    func status() -> [String: Any] {
        [
            "ok": true, "version": version, "machine": machine, "home": home, "owner": owner,
            "keys": keys.map { ["name": $0.name, "fingerprint": $0.fingerprint] },
            "recipes": recipes.map { $0.raw },
        ]
    }
}

let state = State()

// MARK: - Verifying a request

/// Returns the signed payload, after signature, machine, expiry and replay checks.
/// The nonce is spent here, before anything runs, so a crash can never allow a second run.
func verify(_ request: [String: Any]) throws -> (payload: [String: Any], signer: Key) {
    guard let payloadB64 = request["payload"] as? String, payloadB64.count <= maxRequest,
        let signatureB64 = request["signature"] as? String,
        let payloadData = Data(base64Encoded: payloadB64),
        let signatureData = Data(base64Encoded: signatureB64),
        let signature = try? P256.Signing.ECDSASignature(derRepresentation: signatureData)
    else { throw HelperError("malformed request") }
    guard let signer = state.keys.first(where: { $0.key.isValidSignature(signature, for: payloadData) })
    else { throw HelperError("signature not from a paired iPhone") }
    guard let payload = try? JSONSerialization.jsonObject(with: payloadData) as? [String: Any],
        payload["v"] as? Int == 1,
        payload["machine"] as? String == state.machine,
        let nonce = payload["nonce"] as? String, fullMatch(nonce, "[0-9a-f]{32}"),
        let expires = payload["expires"] as? Int
    else { throw HelperError("payload not for this Mac") }
    let now = Int(Date().timeIntervalSince1970)
    guard expires > now, expires - now <= 30 * 86400 else { throw HelperError("payload expired") }
    guard state.used[nonce] == nil else { throw HelperError("payload already used") }
    state.used[nonce] = expires
    try state.saveNonces()
    return (payload, signer)
}

/// Validates the steps a payload may run. Returns them exactly as signed.
func stepsFor(_ payload: [String: Any]) throws -> (steps: [[String]], timeout: Int, label: String) {
    guard let steps = payload["steps"] as? [[String]], (1...5).contains(steps.count),
        steps.allSatisfy({ (1...64).contains($0.count) && $0[0].hasPrefix("/") }),
        steps.joined().allSatisfy({ $0.utf8.count <= 4096 && !$0.contains("\0") })
    else { throw HelperError("bad steps") }
    switch payload["kind"] as? String {
    case "recipe":
        guard let id = payload["recipe"] as? String,
            let recipe = state.recipes.first(where: { $0.id == id })
        else { throw HelperError("unknown recipe") }
        let values = (payload["args"] as? [String: String]) ?? [:]
        let rendered = try recipe.render(values, home: state.home, owner: state.owner)
        guard rendered == steps else { throw HelperError("steps differ from recipe \(id)") }
        return (steps, recipe.timeout, "recipe \(id)")
    case "custom":
        let timeout = min(max((payload["timeout"] as? Int) ?? 600, 5), 7200)
        return (steps, timeout, "custom")
    default:
        throw HelperError("unknown kind")
    }
}

// MARK: - Running

func run(job: String, steps: [[String]], timeout: Int) {
    var output = Data()
    var exitCode: Int32 = 0
    let started = Date()
    let deadline = started.addingTimeInterval(TimeInterval(timeout))
    let lock = NSLock()
    func append(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        let room = maxOutput * 4 - output.count
        if room > 0 { output.append(data.prefix(room)) }
    }
    for argv in steps {
        append(Data(("$ " + argv.joined(separator: " ") + "\n").utf8))
        let process = Process()
        process.executableURL = URL(fileURLWithPath: argv[0])
        process.arguments = Array(argv.dropFirst())
        process.environment = [
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/var/root", "LANG": "en_US.UTF-8",
        ]
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        process.standardInput = FileHandle.nullDevice
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        let reader = DispatchGroup()
        reader.enter()
        DispatchQueue.global().async {
            while true {
                let chunk = pipe.fileHandleForReading.availableData
                if chunk.isEmpty { break }
                append(chunk)
            }
            reader.leave()
        }
        do {
            try process.run()
        } catch {
            append(Data("could not start \(argv[0])\n".utf8))
            exitCode = 127
            try? pipe.fileHandleForWriting.close()
            reader.wait()
            break
        }
        try? pipe.fileHandleForWriting.close()
        let remaining = max(deadline.timeIntervalSinceNow, 1)
        let killer = DispatchWorkItem {
            guard process.isRunning else { return }
            append(Data("timed out after \(timeout) s\n".utf8))
            process.terminate()
            DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
                if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + remaining, execute: killer)
        process.waitUntilExit()
        killer.cancel()
        reader.wait()
        exitCode = process.terminationStatus
        if exitCode != 0 { break }
    }
    lock.lock()
    let tail = output.suffix(maxOutput)
    lock.unlock()
    let record: [String: Any] = [
        "state": exitCode == 0 ? "done" : "failed", "exit": Int(exitCode),
        "output": String(decoding: tail, as: UTF8.self),
        "started": ISO8601DateFormatter().string(from: started),
        "finished": ISO8601DateFormatter().string(from: Date()),
    ]
    state.queue.sync {
        state.jobs[job] = record
        // Keep the newest results in memory; older ones are read back from disk.
        if state.jobs.count > 50, let oldest = state.jobs.keys.first(where: { $0 != job }) {
            state.jobs.removeValue(forKey: oldest)
        }
        state.running -= 1
        if let data = try? JSONSerialization.data(withJSONObject: record) {
            try? writeAtomically(data, to: "\(supportDir)/results/\(job).json", mode: 0o600)
        }
    }
    log("job \(job) exit \(exitCode)")
}

/// Handles signed changes to the helper itself. Runs synchronously; small and bounded.
func manage(_ payload: [String: Any], job: String) throws -> [String: Any] {
    let kind = payload["kind"] as? String ?? ""
    switch kind {
    case "update-helper", "update-recipes":
        guard let file = payload["file"] as? [String: String], let path = file["path"],
            path.hasPrefix("/"), let expected = file["sha256"], fullMatch(expected, "[0-9a-f]{64}"),
            let data = FileManager.default.contents(atPath: path),
            data.count <= (kind == "update-helper" ? 30_000_000 : 1_000_000)
        else { throw HelperError("file missing or too large") }
        // Hash the bytes we will install, not the path, so the file can't change in between.
        guard sha256Hex(data) == expected else { throw HelperError("sha256 mismatch") }
        if kind == "update-recipes" {
            let recipes = try parseRecipes(try JSONSerialization.jsonObject(with: data))
            try writeAtomically(data, to: "\(supportDir)/recipes.json", mode: 0o644)
            state.recipes = recipes
        } else {
            let magic = data.prefix(4).map { $0 }
            let machO: Set<[UInt8]> = [[0xcf, 0xfa, 0xed, 0xfe], [0xca, 0xfe, 0xba, 0xbe]]
            guard machO.contains(magic) else { throw HelperError("not a Mach-O binary") }
            try writeAtomically(data, to: binaryPath, mode: 0o755)
            // launchd (KeepAlive) starts the new binary.
            DispatchQueue.global().asyncAfter(deadline: .now() + 1) { exit(0) }
        }
    case "add-key":
        guard let key = payload["key"] as? [String: String], let name = key["name"],
            (1...40).contains(name.count), let b64 = key["x963"], let data = Data(base64Encoded: b64),
            let publicKey = try? P256.Signing.PublicKey(x963Representation: data)
        else { throw HelperError("bad key") }
        if !state.keys.contains(where: { $0.x963 == data }) {
            guard state.keys.count < 10 else { throw HelperError("at most 10 paired iPhones") }
            state.keys.append(Key(name: name, x963: data, key: publicKey))
            try state.saveKeys()
        }
    case "remove-key":
        guard let print = payload["fingerprint"] as? String else { throw HelperError("bad key") }
        let kept = state.keys.filter { $0.fingerprint != print }
        guard !kept.isEmpty else { throw HelperError("can't remove the last key") }
        state.keys = kept
        try state.saveKeys()
    default:
        throw HelperError("unknown kind")
    }
    let record: [String: Any] = [
        "state": "done", "exit": 0, "output": "\(kind) applied\n",
        "started": ISO8601DateFormatter().string(from: Date()),
        "finished": ISO8601DateFormatter().string(from: Date()),
    ]
    state.jobs[job] = record
    if let data = try? JSONSerialization.data(withJSONObject: record) {
        try? writeAtomically(data, to: "\(supportDir)/results/\(job).json", mode: 0o600)
    }
    return ["ok": true, "job": job, "state": "done"]
}

func handle(_ request: [String: Any]) -> [String: Any] {
    switch request["op"] as? String {
    case "status":
        return state.queue.sync { state.status() }
    case "result":
        guard let job = request["job"] as? String, fullMatch(job, "[0-9a-f]{32}") else {
            return ["ok": false, "error": "bad job"]
        }
        return state.queue.sync {
            if let record = state.jobs[job] { return record.merging(["ok": true]) { a, _ in a } }
            if let saved = readJSON("\(supportDir)/results/\(job).json") as? [String: Any] {
                return saved.merging(["ok": true]) { a, _ in a }
            }
            return ["ok": true, "state": state.used[job] == nil ? "unknown" : "lost"]
        }
    case "run":
        return state.queue.sync { () -> [String: Any] in
            // Check capacity before spending the nonce, so a busy reply never wastes an approval.
            guard state.running < maxJobs else { return ["ok": false, "error": "busy, try again"] }
            do {
                let (payload, signer) = try verify(request)
                let job = payload["nonce"] as! String
                let kind = payload["kind"] as? String ?? ""
                let who = "ask \(payload["ask"] ?? "-") asker \(payload["asker"] ?? "-") key \(signer.fingerprint)"
                if !["recipe", "custom"].contains(kind) {
                    log("manage \(kind) \(who)")
                    return try manage(payload, job: job)
                }
                let (steps, timeout, label) = try stepsFor(payload)
                state.running += 1
                state.jobs[job] = ["state": "running"]
                log("run \(label) \(who) steps \(steps)")
                DispatchQueue.global().async { run(job: job, steps: steps, timeout: timeout) }
                return ["ok": true, "job": job, "state": "running"]
            } catch let error as HelperError {
                log("refused: \(error.message)")
                return ["ok": false, "error": error.message]
            } catch {
                log("refused: internal error")
                return ["ok": false, "error": "internal error"]
            }
        }
    default:
        return ["ok": false, "error": "unknown op"]
    }
}

// MARK: - Socket server

func serve(_ client: Int32) {
    defer { close(client) }
    var uid: uid_t = 0
    var gid: gid_t = 0
    guard getpeereid(client, &uid, &gid) == 0, uid == state.ownerUID || uid == 0 else { return }
    var timeout = timeval(tv_sec: 10, tv_usec: 0)
    setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
    setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
    var buffer = Data()
    var chunk = [UInt8](repeating: 0, count: 16384)
    while buffer.count <= maxRequest, !buffer.contains(0x0a) {
        let count = read(client, &chunk, chunk.count)
        if count <= 0 { break }
        buffer.append(contentsOf: chunk[0..<count])
    }
    var reply: [String: Any] = ["ok": false, "error": "bad request"]
    if let line = buffer.split(separator: 0x0a).first, buffer.count <= maxRequest,
        let request = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any]
    {
        reply = handle(request)
    }
    var data = (try? JSONSerialization.data(withJSONObject: reply, options: [.sortedKeys])) ?? Data()
    data.append(0x0a)
    data.withUnsafeBytes { raw in
        var offset = 0
        while offset < raw.count {
            let sent = write(client, raw.baseAddress! + offset, raw.count - offset)
            if sent <= 0 { break }
            offset += sent
        }
    }
}

func main() {
    #if !ELEVATE_TEST
    guard getuid() == 0 else {
        FileHandle.standardError.write("switchboard-elevate must run as root (launchd)\n".data(using: .utf8)!)
        exit(1)
    }
    #endif
    umask(0o022)
    do {
        try FileManager.default.createDirectory(
            atPath: "\(supportDir)/results", withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        try state.load()
    } catch {
        log("startup failed: \((error as? HelperError)?.message ?? "\(error)")")
        sleep(30)  // keep launchd from spinning
        exit(1)
    }
    unlink(socketPath)
    umask(0o077)  // the socket is never reachable by other accounts, even before chmod
    let server = socket(AF_UNIX, SOCK_STREAM, 0)
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    withUnsafeMutableBytes(of: &address.sun_path) { raw in
        let bytes = Array(socketPath.utf8)
        for (i, byte) in bytes.enumerated() where i < raw.count - 1 { raw[i] = byte }
    }
    let size = socklen_t(MemoryLayout<sockaddr_un>.size)
    let bound = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(server, $0, size) }
    }
    guard bound == 0, listen(server, 16) == 0 else {
        log("socket failed errno \(errno)")
        exit(1)
    }
    chown(socketPath, state.ownerUID, 0)
    chmod(socketPath, 0o600)
    umask(0o022)
    log("started \(version), \(state.keys.count) key(s), \(state.recipes.count) recipe(s)")
    while true {
        let client = accept(server, nil, nil)
        if client < 0 { continue }
        DispatchQueue.global().async { serve(client) }
    }
}

main()
