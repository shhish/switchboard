import Foundation
import Synchronization

/// One completion wins, including cancellation before a continuation is installed.
/// Unstructured backend work is cancelled without waiting for an uncooperative backend.
public final class CompletionGate<Value: Sendable>: Sendable {
  private struct State {
    var result: Result<Value, any Error>?
    var continuation: CheckedContinuation<Value, any Error>?
    var tasks: [Task<Void, Never>] = []
  }
  private let state = Mutex(State())
  public init() {}

  public func add(_ task: Task<Void, Never>) {
    let finished = state.withLock { state in
      if state.result != nil { return true }
      state.tasks.append(task)
      return false
    }
    if finished { task.cancel() }
  }

  @discardableResult
  public func finish(_ result: Result<Value, any Error>) -> Bool {
    let completion = state.withLock {
      state -> (Bool, CheckedContinuation<Value, any Error>?, [Task<Void, Never>]) in
      guard state.result == nil else { return (false, nil, []) }
      state.result = result
      let continuation = state.continuation
      let tasks = state.tasks
      state.continuation = nil
      state.tasks = []
      return (true, continuation, tasks)
    }
    for task in completion.2 { task.cancel() }
    completion.1?.resume(with: result)
    return completion.0
  }

  public func value() async throws -> Value {
    try await withTaskCancellationHandler {
      try await withCheckedThrowingContinuation { continuation in
        let result = state.withLock { state -> Result<Value, any Error>? in
          if let result = state.result { return result }
          state.continuation = continuation
          return nil
        }
        if let result { continuation.resume(with: result) }
      }
    } onCancel: {
      self.finish(.failure(CancellationError()))
    }
  }
}

public enum ExecutionDeadline {
  public static func seconds(for request: Request) -> Double {
    if request.method == .remoteSSH {
      return RemoteSSH.deadline(for: request.text("request") ?? "") + 5
    }
    return [.requestAccess, .thingsAccess, .netAccess].contains(request.method) ? 180 : 10
  }

  public static func run<Value: Sendable>(
    seconds: Double, operation: @escaping @Sendable () async throws -> Value
  ) async throws -> Value {
    try Task.checkCancellation()
    let gate = CompletionGate<Value>()
    gate.add(
      Task {
        do {
          try Task.checkCancellation()
          gate.finish(.success(try await operation()))
        } catch { gate.finish(.failure(error)) }
      })
    gate.add(
      Task {
        do { try await Task.sleep(for: .seconds(seconds)) } catch { return }
        gate.finish(.failure(BridgeError("timeout")))
      })
    return try await gate.value()
  }
}
