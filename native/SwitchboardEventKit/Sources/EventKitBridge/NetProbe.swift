import Darwin
import Foundation
import Network
import Synchronization

/// This helper is never a general HTTP client. Revalidate at the native boundary.
public enum NetProbe {
  public static func validate(_ value: String) throws -> URL {
    guard !value.contains(where: { $0.isWhitespace || $0.asciiValue.map { $0 < 32 } == true }),
      !value.contains("\\"), !value.contains("?"), !value.contains("#"),
      let parts = URLComponents(string: value),
      parts.scheme?.lowercased() == "http" || parts.scheme?.lowercased() == "https",
      let host = parts.host, parts.percentEncodedHost == host,
      parts.user == nil, parts.password == nil,
      parts.percentEncodedPath.count <= 200,
      parts.port == nil || (1...65535).contains(parts.port!),
      let url = parts.url
    else { throw BridgeError("invalid_arguments") }
    let octets = host.split(separator: ".", omittingEmptySubsequences: false)
    guard octets.count == 4 else { throw BridgeError("invalid_arguments") }
    let bytes = octets.compactMap { UInt8($0) }
    guard bytes.count == 4,
      zip(octets, bytes).allSatisfy({ String($0.1) == $0.0 }),
      bytes[0] == 10 || (bytes[0] == 172 && (16...31).contains(bytes[1]))
        || (bytes[0] == 192 && bytes[1] == 168)
    else { throw BridgeError("invalid_arguments") }
    return url
  }

  public static func execute(_ request: Request) async throws -> [String: JSONValue] {
    let url = try validate(request.text("url") ?? "")
    let configuration = URLSessionConfiguration.ephemeral
    configuration.httpShouldSetCookies = false
    configuration.httpCookieStorage = nil
    configuration.urlCache = nil
    configuration.urlCredentialStorage = nil
    configuration.connectionProxyDictionary = [:]
    configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
    configuration.timeoutIntervalForRequest = 3
    configuration.timeoutIntervalForResource = 3
    configuration.waitsForConnectivity = false
    let session = URLSession(configuration: configuration, delegate: ProbeDelegate(), delegateQueue: nil)
    defer { session.invalidateAndCancel() }
    let started = ContinuousClock.now
    do {
      var get = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3)
      get.httpMethod = "GET"
      // Stop at response headers. No response body is accumulated, decoded or returned.
      let (_, response) = try await session.bytes(for: get)
      guard let http = response as? HTTPURLResponse else { return ["error": .string("other")] }
      let elapsed = started.duration(to: .now).components
      let ms = Double(elapsed.seconds) * 1000 + Double(elapsed.attoseconds) / 1e15
      return ["http_status": .number(Double(http.statusCode)), "latency_ms": .number(max(0, ms))]
    } catch {
      let ns = error as NSError
      let underlying = ns.userInfo[NSUnderlyingErrorKey] as? NSError
      let code = (error as? URLError)?.code
      // URLSession does not expose NWPath's privacy reason. Treat its offline/policy
      // errors conservatively as unknown; never claim a device outage from these.
      var denied = code == .notConnectedToInternet || (underlying?.domain == NSPOSIXErrorDomain
        && [Int(EACCES), Int(EPERM)].contains(underlying?.code ?? 0))
      if !denied && [.cannotConnectToHost, .networkConnectionLost, .timedOut].contains(code) {
        // A URLSession error alone cannot distinguish a dead host from macOS
        // privacy denial. Inspect the public path reason on a bounded TCP check;
        // this sends no second HTTP request or application data.
        denied = await localNetworkDenied(url)
      }
      var kind = "other"
      switch code {
      case .timedOut: kind = "timeout"
      case .cannotConnectToHost:
        kind = underlying?.domain == NSPOSIXErrorDomain && underlying?.code == Int(EHOSTUNREACH)
          ? "unreachable" : "refused"
      case .cannotFindHost, .dnsLookupFailed, .notConnectedToInternet, .networkConnectionLost:
        kind = "unreachable"
      case .secureConnectionFailed, .serverCertificateHasBadDate, .serverCertificateUntrusted,
        .serverCertificateHasUnknownRoot, .serverCertificateNotYetValid,
        .clientCertificateRejected, .clientCertificateRequired:
        kind = "tls"
      default: break
      }
      var result: [String: JSONValue] = ["error": .string(kind)]
      if denied { result["local_network_denied"] = .bool(true) }
      return result
    }
  }

  private static func localNetworkDenied(_ url: URL) async -> Bool {
    guard let host = url.host,
      let port = NWEndpoint.Port(rawValue: UInt16(url.port ?? (url.scheme == "https" ? 443 : 80)))
    else { return false }
    let connection = NWConnection(host: NWEndpoint.Host(host), port: port, using: .tcp)
    let queue = DispatchQueue(label: "switchboard.eventkit.net-privacy")
    let pending = Mutex<CheckedContinuation<Bool, Never>?>(nil)
    let finish: @Sendable (Bool) -> Void = { denied in
      let continuation = pending.withLock { value in
        let current = value
        value = nil
        return current
      }
      connection.cancel()
      continuation?.resume(returning: denied)
    }
    return await withTaskCancellationHandler {
      await withCheckedContinuation { continuation in
        pending.withLock { $0 = continuation }
        connection.stateUpdateHandler = { state in
          switch state {
          case .waiting:
            finish(connection.currentPath?.unsatisfiedReason == .localNetworkDenied)
          case .ready, .failed, .cancelled: finish(false)
          default: break
          }
        }
        connection.start(queue: queue)
        queue.asyncAfter(deadline: .now() + .milliseconds(150)) { finish(false) }
      }
    } onCancel: {
      connection.cancel()
    }
  }

}

private final class ProbeDelegate: NSObject, URLSessionTaskDelegate {
  func urlSession(
    _ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void
  ) {
    completionHandler(nil)
  }

  func urlSession(
    _ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge,
    completionHandler: @escaping @Sendable (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
  ) {
    // Default TLS trust evaluation, but never supply HTTP/proxy/client credentials.
    completionHandler(
      challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust
        ? .performDefaultHandling : .cancelAuthenticationChallenge, nil)
  }
}
