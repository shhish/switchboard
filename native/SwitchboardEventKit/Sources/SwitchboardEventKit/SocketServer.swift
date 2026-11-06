import Darwin
import Dispatch
import EventKitBridge
import Foundation
import Synchronization

final class SocketServer: Sendable {
  let backend: any EventKitBackend
  private let active = Mutex(0)

  init(backend: any EventKitBackend) { self.backend = backend }

  func start() throws {
    let directory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(
      "Library/Application Support/switchboard")
    let path = directory.appendingPathComponent("eventkit.sock").path
    umask(0o077)
    try FileManager.default.createDirectory(
      at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    var info = stat()
    guard lstat(directory.path, &info) == 0, info.st_uid == getuid(),
      info.st_mode & S_IFMT == S_IFDIR,
      info.st_mode & 0o777 == 0o700
    else { throw BridgeError("backend_error") }
    // A lifetime lock prevents a second helper from unlinking a live socket.
    let lockPath = directory.appendingPathComponent("eventkit.lock").path
    let lockFD = open(lockPath, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
    guard lockFD >= 0 else { throw BridgeError("backend_error") }
    guard fstat(lockFD, &info) == 0, info.st_uid == getuid(), info.st_mode & S_IFMT == S_IFREG,
      info.st_mode & 0o777 == 0o600, flock(lockFD, LOCK_EX | LOCK_NB) == 0
    else {
      close(lockFD)
      throw BridgeError("backend_error")
    }
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8) + [UInt8(0)]
    guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else {
      close(lockFD)
      throw BridgeError("backend_error")
    }
    withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: bytes) }
    address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    if lstat(path, &info) == 0 {
      guard info.st_uid == getuid(), info.st_mode & S_IFMT == S_IFSOCK else {
        close(lockFD)
        throw BridgeError("backend_error")
      }
      guard unlink(path) == 0 else {
        close(lockFD)
        throw BridgeError("backend_error")
      }
    }
    let listener = socket(AF_UNIX, SOCK_STREAM, 0)
    guard listener >= 0 else {
      close(lockFD)
      throw BridgeError("backend_error")
    }
    let bound = withUnsafePointer(to: &address) { pointer in
      pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        Darwin.bind(listener, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
      }
    }
    guard bound == 0, chmod(path, 0o600) == 0, listen(listener, 16) == 0 else {
      close(listener)
      close(lockFD)
      throw BridgeError("backend_error")
    }
    DispatchQueue(label: "switchboard.eventkit.accept").async { [self] in
      defer {
        close(listener)
        close(lockFD)
        unlink(path)
      }
      while true {
        let peer = accept(listener, nil, nil)
        if peer < 0 {
          if errno == EINTR { continue }
          break
        }
        var uid: uid_t = 0
        var gid: gid_t = 0
        guard getpeereid(peer, &uid, &gid) == 0, uid == getuid() else {
          close(peer)
          continue
        }
        let admitted = active.withLock { count in
          if count >= 16 { return false }
          count += 1
          return true
        }
        guard admitted else {
          close(peer)
          continue
        }
        var timeout = timeval(tv_sec: 10, tv_usec: 0)
        _ = setsockopt(
          peer, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        _ = setsockopt(
          peer, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var noSignal: Int32 = 1
        _ = setsockopt(
          peer, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        DispatchQueue.global().async { [self] in
          // One bounded NDJSON request per connection. Blocking socket I/O
          // stays on Dispatch workers, outside Swift's cooperative pool.
          guard let data = Self.readLine(peer) else {
            close(peer)
            active.withLock { $0 -= 1 }
            return
          }
          _ = NativeConnection(peer: peer, data: data, backend: backend) { [self] in
            active.withLock { $0 -= 1 }
          }
        }
      }
    }
  }

  private static func readLine(_ fd: Int32) -> Data? {
    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 4096)
    let deadline = Date().addingTimeInterval(10)
    while data.count <= 65536 && Date() < deadline {
      let count = recv(fd, &buffer, buffer.count, 0)
      if count < 0 && errno == EINTR { continue }
      guard count > 0 else { return nil }
      data.append(contentsOf: buffer.prefix(count))
      if let newline = data.firstIndex(of: 10) {
        guard newline <= 65536, newline == data.count - 1 else { return nil }
        return data.prefix(newline)
      }
    }
    return nil
  }
}
