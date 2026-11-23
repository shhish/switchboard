import Foundation

extension Request {
  func validateAppleNotes() throws {
    switch method {
    case .appleNotesStatus, .appleNotesAccess:
      guard args.isEmpty else { throw BridgeError("invalid_arguments") }
    case .appleNotesSearch:
      guard Set(args.keys).isSubset(of: ["query", "limit"]),
        let query = text("query"), query.count <= 500,
        !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      else { throw BridgeError("invalid_arguments") }
      if args["limit"] != nil {
        guard let limit = number("limit"), (1...20).contains(limit) else {
          throw BridgeError("invalid_arguments")
        }
      }
    case .appleNotesGet:
      guard Set(args.keys) == ["id"], let id = text("id"),
        id.hasPrefix("x-coredata://"), id.count <= 1000,
        !id.contains("\u{0}")
      else { throw BridgeError("invalid_arguments") }
    default: throw BridgeError("invalid_arguments")
    }
  }
}
