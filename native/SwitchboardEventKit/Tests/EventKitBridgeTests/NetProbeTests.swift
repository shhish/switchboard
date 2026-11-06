import Foundation
import Testing

@testable import EventKitBridge

@Test func lanProbeValidation() throws {
  for method in ["net.probe", "net.request_access"] {
    for host in ["\(ip(10, 0, 0, 1))", "172.16.0.1", "172.31.255.255", "\(ip(192, 168, 50, 2))"] {
      for scheme in ["http", "https"] {
        let args = ["url": "\(scheme)://\(host):8080/health"]
        let data = try JSONSerialization.data(withJSONObject: ["method": method, "args": args])
        #expect(throws: Never.self) { try Request(data: data) }
      }
    }
  }
}

@Test func invalidLanProbes() throws {
  let invalid = [
    "http://127.0.0.1/", "http://[::1]/", "http://localhost/", "http://8.8.8.8/",
    "http://169.254.1.1/", "http://172.15.0.1/", "http://172.32.0.1/", "http://192.169.0.1/",
    "http://\(ip(10, "01", 1, 1))/", "http://\(ip(10, 1, 1))/", "http://\(ip(10, 1, 1, 256))/", "http://0x0a000001/",
    "http://%31%30.0.0.1/", "http://167772161/", "http://user@\(ip(10, 0, 0, 1))/", "http://@\(ip(10, 0, 0, 1))/",
    "http://\(ip(10, 0, 0, 1))/?", "http://\(ip(10, 0, 0, 1))/#", "http://\(ip(10, 0, 0, 1))/?key=secret",
    "http://\(ip(10, 0, 0, 1))\\@8.8.8.8/", "ftp://\(ip(10, 0, 0, 1))/", "http://\(ip(10, 0, 0, 1)):0/",
    "http://\(ip(10, 0, 0, 1)):65536/", "http://\(ip(10, 0, 0, 1))/\n", "http://\(ip(10, 0, 0, 1))/" + String(repeating: "a", count: 200),
  ]
  for value in invalid {
    #expect(throws: (any Error).self) { try NetProbe.validate(value) }
  }
  for args: [String: Any] in [[:], ["url": true], ["url": "http://\(ip(10, 0, 0, 1))", "headers": [:]]] {
    let data = try JSONSerialization.data(withJSONObject: ["method": "net.probe", "args": args])
    #expect(throws: (any Error).self) { try Request(data: data) }
  }
}
