import EventKit
import Foundation

// The store and every fetched mutable EventKit object stay in this actor. Fetch
// callbacks convert reminders to Sendable value snapshots before crossing back.
public actor NativeBackend: EventKitBackend {
  private let store = EKEventStore()
  private let things = ThingsBackend()
  private var reminderFetches: [UUID: Any] = [:]
  public init() {}

  private func authorization(_ entity: EKEntityType) -> String {
    switch EKEventStore.authorizationStatus(for: entity) {
    case .notDetermined: return "not_determined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .fullAccess: return "full_access"
    case .writeOnly: return "write_only"
    @unknown default: return "unknown"
    }
  }

  private func require(_ entity: EKEntityType) throws {
    guard authorization(entity) == "full_access" else { throw BridgeError("unauthorized") }
  }

  private func status() -> [String: JSONValue] {
    ["events": .string(authorization(.event)), "reminders": .string(authorization(.reminder))]
  }

  private func calendars(_ entity: EKEntityType, ids: [String]?) throws -> [EKCalendar] {
    let all = store.calendars(for: entity)
    guard let ids else { return all }
    let selected = all.filter { ids.contains($0.calendarIdentifier) }
    guard Set(selected.map(\.calendarIdentifier)) == Set(ids) else {
      throw BridgeError("not_found")
    }
    return selected
  }

  private func writableCalendar(_ entity: EKEntityType, id: String) throws -> EKCalendar {
    guard let calendar = try calendars(entity, ids: [id]).first else {
      throw BridgeError("not_found")
    }
    guard calendar.allowsContentModifications else { throw BridgeError("resource_denied") }
    return calendar
  }

  private nonisolated static func iso(_ date: Date) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.timeZone = .current
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: date)
  }

  private func eventJSON(_ event: EKEvent) -> JSONValue {
    .object([
      "id": .string(event.eventIdentifier ?? event.calendarItemIdentifier),
      "calendar_id": .string(event.calendar.calendarIdentifier),
      "title": .string(event.title ?? ""), "start": .string(Self.iso(event.startDate)),
      "end": .string(Self.iso(event.endDate)), "all_day": .bool(event.isAllDay),
      "location": event.location.map(JSONValue.string) ?? .null,
    ])
  }

  private struct ReminderRow: Sendable {
    let id: String
    let due: Date?
    let completed: Bool
    let json: JSONValue
  }

  private nonisolated static func reminderRow(_ reminder: EKReminder) -> ReminderRow {
    let due = reminder.dueDateComponents.flatMap { components -> Date? in
      var calendar = Calendar.current
      calendar.timeZone = components.timeZone ?? .current
      return calendar.date(from: components)
    }
    return ReminderRow(
      id: reminder.calendarItemIdentifier, due: due, completed: reminder.isCompleted,
      json: .object([
        "id": .string(reminder.calendarItemIdentifier),
        "list_id": .string(reminder.calendar.calendarIdentifier),
        "title": .string(reminder.title ?? ""), "due": due.map { .string(iso($0)) } ?? .null,
        "completed": .bool(reminder.isCompleted), "priority": .number(Double(reminder.priority)),
      ]))
  }

  private func fetch(_ calendars: [EKCalendar]) async throws -> [ReminderRow] {
    let predicate = store.predicateForReminders(in: calendars)
    let id = UUID()
    let gate = CompletionGate<[ReminderRow]>()
    reminderFetches[id] = store.fetchReminders(matching: predicate) { reminders in
      if let reminders {
        gate.finish(.success(reminders.map(Self.reminderRow)))
      } else {
        gate.finish(.failure(BridgeError("backend_error")))
      }
    }
    defer { cancelFetch(id) }
    return try await withTaskCancellationHandler {
      try await gate.value()
    } onCancel: {
      gate.finish(.failure(CancellationError()))
      Task { await self.cancelFetch(id) }
    }
  }

  private func cancelFetch(_ id: UUID) {
    if let token = reminderFetches.removeValue(forKey: id) {
      store.cancelFetchRequest(token)
    }
  }

  public func execute(_ request: Request) async throws -> [String: JSONValue] {
    try Task.checkCancellation()
    if request.method.rawValue.hasPrefix("things.") {
      return try await things.execute(request)
    }
    var result: [String: JSONValue]
    let limit = request.number("limit") ?? 200
    switch request.method {
    case .netProbe, .netAccess:
      return try await NetProbe.execute(request)
    case .remoteSSH:
      return try await RemoteSSH.execute(request)
    case .status: result = status()
    case .requestAccess:
      // Never reached from discovery/doctor/domain calls. A same-UID local
      // owner CLI explicitly requests both prompts while a person is present.
      if authorization(.event) == "not_determined" {
        let gate = CompletionGate<Bool>()
        store.requestFullAccessToEvents { granted, error in
          if let error { gate.finish(.failure(error)) } else { gate.finish(.success(granted)) }
        }
        _ = try await gate.value()
      }
      try Task.checkCancellation()
      if authorization(.reminder) == "not_determined" {
        let gate = CompletionGate<Bool>()
        store.requestFullAccessToReminders { granted, error in
          if let error { gate.finish(.failure(error)) } else { gate.finish(.success(granted)) }
        }
        _ = try await gate.value()
      }
      result = status()
    case .calendars, .lists:
      let entity: EKEntityType = request.method == .calendars ? .event : .reminder
      try require(entity)
      let all = try calendars(entity, ids: nil).sorted {
        $0.calendarIdentifier < $1.calendarIdentifier
      }
      let defaultID =
        entity == .event
        ? store.defaultCalendarForNewEvents?.calendarIdentifier
        : store.defaultCalendarForNewReminders()?.calendarIdentifier
      let items = all.prefix(200).map { calendar in
        JSONValue.object([
          "id": .string(calendar.calendarIdentifier), "title": .string(calendar.title),
          "writable": .bool(calendar.allowsContentModifications),
          "default": .bool(calendar.calendarIdentifier == defaultID),
        ])
      }
      result = [
        request.method == .calendars ? "calendars" : "lists": .array(items),
        "truncated": .bool(all.count > 200),
      ]
    case .events:
      try require(.event)
      let start = try request.date("start")!
      let end = try request.date("end")!
      let selected = try calendars(.event, ids: request.strings("calendar_ids"))
      let events =
        selected.isEmpty
        ? []
        : store.events(
          matching: store.predicateForEvents(withStart: start, end: end, calendars: selected)
        ).sorted {
          $0.startDate == $1.startDate
            ? $0.calendarItemIdentifier < $1.calendarItemIdentifier : $0.startDate < $1.startDate
        }
      result = [
        "events": .array(events.prefix(limit).map(eventJSON)),
        "truncated": .bool(events.count > limit),
      ]
    case .create:
      try require(.event)
      let calendar = try writableCalendar(.event, id: request.text("calendar_id")!)
      let event = EKEvent(eventStore: store)
      event.calendar = calendar
      event.title = request.text("title")!
      event.startDate = try request.date("start")!
      event.endDate = try request.date("end")!
      event.isAllDay = request.flag("all_day")
      event.timeZone = .current
      event.location = request.text("location")
      event.notes = request.text("notes")
      if case .array(let alarms) = request.args["alarms_minutes"] {
        event.alarms = alarms.compactMap {
          if case .number(let minutes) = $0 { return EKAlarm(relativeOffset: -minutes * 60) }
          return nil
        }
      }
      try store.save(event, span: .thisEvent, commit: true)
      result = ["event": eventJSON(event)]
    case .reminders:
      try require(.reminder)
      let selected = try calendars(.reminder, ids: request.strings("list_ids"))
      let before = try request.date("due_before")
      let all = selected.isEmpty ? [] : try await fetch(selected)
      let rows = all.filter { row in
        (request.flag("include_completed") || !row.completed)
          && (before == nil || (row.due != nil && row.due! < before!))
      }.sorted { a, b in
        let first = a.due ?? .distantFuture
        let second = b.due ?? .distantFuture
        return first == second ? a.id < b.id : first < second
      }
      result = [
        "reminders": .array(rows.prefix(limit).map(\.json)), "truncated": .bool(rows.count > limit),
      ]
    case .updateEvent:
      try require(.event)
      guard let event = store.event(withIdentifier: request.text("event_id")!) else {
        throw BridgeError("not_found")
      }
      // No await between checking the current calendar/participants and saving.
      try ItemEdits.event(event, request: request)
      try store.save(event, span: ItemEdits.eventSpan, commit: true)
      result = ["event": eventJSON(event)]
    case .updateReminder:
      try require(.reminder)
      guard
        let reminder = store.calendarItem(withIdentifier: request.text("reminder_id")!)
          as? EKReminder
      else {
        throw BridgeError("not_found")
      }
      try ItemEdits.reminder(reminder, request: request)
      try store.save(reminder, commit: true)
      result = ["reminder": Self.reminderRow(reminder).json]
    case .add:
      try require(.reminder)
      let calendar = try writableCalendar(.reminder, id: request.text("list_id")!)
      let reminder = EKReminder(eventStore: store)
      reminder.calendar = calendar
      reminder.title = request.text("title")!
      reminder.notes = request.text("notes")
      reminder.priority = request.number("priority") ?? 0
      if let due = try request.date("due") {
        var components = Calendar.current.dateComponents(
          [.year, .month, .day, .hour, .minute, .second], from: due)
        components.timeZone = .current
        reminder.dueDateComponents = components
      }
      try store.save(reminder, commit: true)
      result = ["reminder": Self.reminderRow(reminder).json]
    case .complete:
      try require(.reminder)
      guard
        let reminder = store.calendarItem(withIdentifier: request.text("reminder_id")!)
          as? EKReminder
      else { throw BridgeError("not_found") }
      // No await between the current-list authorization check and the save.
      // Switchboard always supplies its policy-derived list IDs. The helper's
      // local owner protocol also supports reminder_id alone, like its other
      // same-UID operations; that socket is not a per-client policy boundary.
      if let allowed = request.strings("list_ids"),
        !allowed.contains(reminder.calendar.calendarIdentifier)
      {
        throw BridgeError("resource_denied")
      }
      guard reminder.calendar.allowsContentModifications else {
        throw BridgeError("resource_denied")
      }
      if !reminder.isCompleted {
        reminder.isCompleted = true
        try store.save(reminder, commit: true)
      }
      result = ["reminder": Self.reminderRow(reminder).json]
    case .thingsStatus, .thingsAccess, .thingsLists, .thingsSearch,
      .thingsProjects, .thingsAreas, .thingsAdd, .thingsComplete, .thingsUpdate:
      throw BridgeError("invalid_arguments")
    }
    result["timezone"] = .string(TimeZone.current.identifier)
    result["timezone_note"] = .string(
      "Naive inputs use the system timezone: \(TimeZone.current.identifier)")
    return result
  }
}
