import Foundation

extension Request {
  func validateThings() throws {
    let policy: Set<String> = ["allow_all", "projects", "areas", "deny_projects", "deny_areas"]
    let fields: Set<String>
    let required: Set<String>
    switch method {
    case .thingsStatus, .thingsAccess, .thingsProjects, .thingsAreas:
      fields = []
      required = []
    case .thingsLists:
      fields = ["list", "limit"]
      required = ["list"]
    case .thingsSearch:
      fields = ["query", "limit"]
      required = ["query"]
    case .thingsAdd:
      fields = policy.union([
        "title", "notes", "when", "deadline", "project_id", "area_id", "tags", "checklist",
      ])
      required = policy.union(["title"])
    case .thingsComplete:
      fields = policy.union(["todo_id"])
      required = policy.union(["todo_id"])
    case .thingsUpdate:
      fields = policy.union([
        "todo_id", "title", "notes", "when", "deadline", "project_id", "area_id", "tags",
      ])
      required = policy.union(["todo_id"])
    default: throw BridgeError("invalid_arguments")
    }
    guard Set(args.keys).isSubset(of: fields), required.isSubset(of: Set(args.keys)) else {
      throw BridgeError("invalid_arguments")
    }
    if method == .thingsUpdate && Set(args.keys).subtracting(required).isEmpty {
      throw BridgeError("invalid_arguments")
    }
    for (key, value) in args {
      switch key {
      case "allow_all":
        guard case .bool = value else { throw BridgeError("invalid_arguments") }
      case "limit":
        guard let n = number(key), (1...(method == .thingsSearch ? 50 : 200)).contains(n) else {
          throw BridgeError("invalid_arguments")
        }
      case "projects", "areas", "deny_projects", "deny_areas", "tags", "checklist":
        let maximum = key == "tags" ? 10 : key == "checklist" ? 20 : 200
        guard case .array(let values) = value, values.count <= maximum,
          let items = strings(key), items.count == values.count,
          items.allSatisfy({
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
              && $0.count <= (key == "tags" || key == "checklist" ? 200 : 1000)
              && (!(key == "tags" || key == "checklist")
                || (!$0.contains("\n") && !$0.contains("\r")))
              && (key != "tags" || !$0.contains(","))
          })
        else { throw BridgeError("invalid_arguments") }
      default:
        if method == .thingsUpdate
          && (key == "deadline" && value == .null || key == "notes" && value == .string(""))
        {
          continue
        }
        let maxLength = key == "title" || key == "query" ? 200 : key == "notes" ? 2000 : 1000
        guard case .string(let text) = value, text.count <= maxLength,
          !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { throw BridgeError("invalid_arguments") }
      }
    }
    if method == .thingsLists,
      !["Inbox", "Today", "Upcoming", "Anytime", "Someday"].contains(text("list")!)
    {
      throw BridgeError("invalid_arguments")
    }
    if text("project_id") != nil && text("area_id") != nil {
      throw BridgeError("invalid_arguments")
    }
    if let when = text("when"),
      !["today", "tonight", "evening", "tomorrow", "anytime", "someday"].contains(when)
    {
      try Self.validateDay(when)
    }
    if method == .thingsUpdate && ["tonight", "evening"].contains(text("when") ?? "") {
      throw BridgeError("invalid_arguments")
    }
    if let deadline = text("deadline") { try Self.validateDay(deadline) }
  }

  static func validateDay(_ value: String) throws {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = .current
    formatter.dateFormat = "yyyy-MM-dd"
    formatter.isLenient = false
    guard value.count == 10, let day = formatter.date(from: value),
      formatter.string(from: day) == value
    else { throw BridgeError("invalid_arguments") }
  }
}
