import EventKit
import Foundation

// Deterministic edit checks are shared by the real backend and offline tests.
enum ItemEdits {
  static let eventSpan: EKSpan = .thisEvent

  static func requireEvent(
    calendarID: String, writable: Bool, allowed: [String],
    hasAttendees: Bool, otherOrganizer: Bool
  ) throws {
    guard allowed.contains(calendarID), writable, !hasAttendees, !otherOrganizer else {
      throw BridgeError("resource_denied")
    }
  }

  static func event(_ event: EKEvent, request: Request) throws {
    try requireEvent(
      calendarID: event.calendar.calendarIdentifier,
      writable: event.calendar.allowsContentModifications,
      allowed: request.strings("calendar_ids")!, hasAttendees: event.hasAttendees,
      otherOrganizer: event.organizer.map { !$0.isCurrentUser } ?? false)
    // Validate the merged interval before changing even one property.
    let (start, end) = try eventDates(request, start: event.startDate, end: event.endDate)
    event.startDate = start
    event.endDate = end
    if let title = request.text("title") { event.title = title }
    if let location = request.text("location") { event.location = location }
    if let notes = request.text("notes") { event.notes = notes }
    if request.args["all_day"] != nil { event.isAllDay = request.flag("all_day") }
  }

  static func eventDates(_ request: Request, start: Date, end: Date) throws -> (Date, Date) {
    let start = try request.date("start") ?? start
    let end = try request.date("end") ?? end
    try Request.validateInterval(start: start, end: end)
    return (start, end)
  }

  static func requireReminder(listID: String, writable: Bool, allowed: [String]) throws {
    guard allowed.contains(listID), writable else { throw BridgeError("resource_denied") }
  }

  static func reminder(_ reminder: EKReminder, request: Request) throws {
    try requireReminder(
      listID: reminder.calendar.calendarIdentifier,
      writable: reminder.calendar.allowsContentModifications,
      allowed: request.strings("list_ids")!)
    if let title = request.text("title") { reminder.title = title }
    if let notes = request.text("notes") { reminder.notes = notes }
    if let priority = request.number("priority") { reminder.priority = priority }
    if request.args["due"] == .null {
      reminder.dueDateComponents = nil
    } else if let value = request.text("due") {
      reminder.dueDateComponents = try dueComponents(value)
    }
  }

  static func dueJSON(_ components: DateComponents?) -> JSONValue {
    guard let components else { return .null }
    if components.hour == nil, components.minute == nil, components.second == nil,
      let year = components.year, let month = components.month, let day = components.day
    {
      return .string(String(format: "%04d-%02d-%02d", year, month, day))
    }
    var calendar = Calendar.current
    calendar.timeZone = components.timeZone ?? .current
    guard let date = calendar.date(from: components) else { return .null }
    let formatter = ISO8601DateFormatter()
    formatter.timeZone = .current
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return .string(formatter.string(from: date))
  }

  static func dueComponents(_ value: String) throws -> DateComponents {
    let due = try Request.parseDue(value)
    let fields: Set<Calendar.Component> =
      value.count == 10
      ? [.year, .month, .day] : [.year, .month, .day, .hour, .minute, .second]
    var components = Calendar.current.dateComponents(fields, from: due)
    components.timeZone = .current
    return components
  }
}
