import Darwin
import Dispatch
import Foundation
import Synchronization

/// Owns the peer until I/O completes or the client disconnects. The cancel handler
/// closes the FD after source callbacks have stopped, and releases admission once.
public final class NativeConnection: Sendable {
  private let source: DispatchSourceRead
  private let task = Mutex<Task<Void, Never>?>(nil)
  private let finished = Mutex(false)

  public init(
    peer: Int32, data: Data, backend: any EventKitBackend, deadline: Double? = nil,
    release: @escaping @Sendable () -> Void
  ) {
    let queue = DispatchQueue(label: "switchboard.eventkit.peer")
    source = DispatchSource.makeReadSource(fileDescriptor: peer, queue: queue)
    source.setCancelHandler {
      close(peer)
      release()
    }
    source.setEventHandler { [self] in
      var byte: UInt8 = 0
      let count = recv(peer, &byte, 1, MSG_PEEK | MSG_DONTWAIT)
      // One request per connection: EOF or additional input ends execution.
      if count >= 0 || (errno != EAGAIN && errno != EINTR) { finish() }
    }
    let worker = Task { [self] in
      let response = await Router.respond(data, backend: backend, deadline: deadline) + Data([10])
      queue.async { [self] in
        guard !finished.withLock({ $0 }) else { return }
        response.withUnsafeBytes { buffer in
          var offset = 0
          while offset < buffer.count {
            let sent = send(
              peer, buffer.baseAddress!.advanced(by: offset), buffer.count - offset, 0)
            if sent < 0 && errno == EINTR { continue }
            if sent <= 0 { break }
            offset += sent
          }
        }
        finish()
      }
    }
    task.withLock { $0 = worker }
    source.resume()
  }

  private func finish() {
    let first = finished.withLock { value in
      if value { return false }
      value = true
      return true
    }
    guard first else { return }
    task.withLock { value in
      value?.cancel()
      value = nil
    }
    source.setEventHandler(handler: nil)
    source.cancel()
  }
}
