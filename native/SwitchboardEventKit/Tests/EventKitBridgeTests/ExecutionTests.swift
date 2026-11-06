import Darwin
import Foundation
import Synchronization
import Testing

@testable import EventKitBridge

/// Intentionally ignores cancellation and retains its continuation until the test ends.
private actor StuckBackend: EventKitBackend {
  private var pending: [CheckedContinuation<[String: JSONValue], any Error>] = []
  private var cancellations = 0

  func execute(_ request: Request) async throws -> [String: JSONValue] {
    try await withTaskCancellationHandler {
      try await withCheckedThrowingContinuation { pending.append($0) }
    } onCancel: {
      Task { await self.cancelled() }
    }
  }
  private func cancelled() { cancellations += 1 }
  func counts() -> (Int, Int) { (pending.count, cancellations) }
  func finishLate() {
    for continuation in pending { continuation.resume(returning: [:]) }
    pending = []
  }
}

private func waitFor(_ condition: @escaping @Sendable () async -> Bool) async throws {
  let deadline = ContinuousClock.now.advanced(by: .seconds(2))
  while !(await condition()) {
    guard ContinuousClock.now < deadline else {
      Issue.record("Condition did not complete before deadline")
      return
    }
    try await Task.sleep(for: .milliseconds(5))
  }
}

@Test func executionDeadlineDoesNotAwaitHungBackendAndLateCompletionIsSafe() async throws {
  let backend = StuckBackend()
  let data = Data(#"{"method":"reminders.list","args":{}}"#.utf8)
  let started = ContinuousClock.now
  let response = await Router.respond(data, backend: backend, deadline: 0.03)
  #expect(started.duration(to: .now) < .seconds(1))
  let result = try JSONDecoder().decode([String: JSONValue].self, from: response)
  #expect(result["ok"] == .bool(false))
  guard case .object(let error) = result["error"] else {
    Issue.record("Missing error")
    return
  }
  #expect(error["code"] == .string("timeout"))
  try await waitFor { await backend.counts().1 == 1 }
  await backend.finishLate()
}

@Test func accessDeadlinesAreLongerAndAllOtherRequestsAreBounded() throws {
  for method in [
    "request_access", "things.request_access", "net.request_access", "status", "reminders.list",
  ] {
    let args = method == "net.request_access" ? #"{"url":"http://172.20.0.2/"}"# : "{}"
    let request = try Request(data: Data("{\"method\":\"\(method)\",\"args\":\(args)}".utf8))
    #expect(
      ExecutionDeadline.seconds(for: request) == (method.contains("request_access") ? 180 : 10))
  }
}

@Test func disconnectAndDeadlineReleaseEachAdmissionExactlyOnce() async throws {
  let backend = StuckBackend()
  let data = Data(#"{"method":"reminders.list","args":{}}"#.utf8)
  let releases = Mutex(0)
  // More than the server's 16 admission slots: a hung backend must not exhaust them.
  for index in 0..<20 {
    var sockets: [Int32] = [-1, -1]
    #expect(socketpair(AF_UNIX, SOCK_STREAM, 0, &sockets) == 0)
    let client = sockets[0]
    var noSignal: Int32 = 1
    _ = setsockopt(
      sockets[1], SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
    _ = NativeConnection(
      peer: sockets[1], data: data, backend: backend, deadline: index % 2 == 0 ? 0.03 : 10
    ) {
      releases.withLock { $0 += 1 }
    }
    try await waitFor { await backend.counts().0 == index + 1 }
    if index % 2 == 1 { close(client) }
    try await waitFor { releases.withLock { $0 == index + 1 } }
    if index % 2 == 0 { close(client) }
  }
  try await waitFor { await backend.counts().1 == 20 }
  await backend.finishLate()
  try await Task.sleep(for: .milliseconds(30))
  #expect(releases.withLock { $0 } == 20)
}

@Test func completionGuardHandlesCancelBeforeInstallAndRepeatedCallbacks() async throws {
  let gate = CompletionGate<Int>()
  #expect(gate.finish(.failure(CancellationError())))
  #expect(!gate.finish(.success(42)))
  do {
    _ = try await gate.value()
    Issue.record("Expected cancellation")
  } catch is CancellationError {}
}
