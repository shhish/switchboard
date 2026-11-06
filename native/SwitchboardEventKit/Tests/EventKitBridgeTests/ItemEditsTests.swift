import EventKit
import Foundation
import Testing

@testable import EventKitBridge

@Test func calendarEditsRefuseInvitationsAndDisallowedCurrentCalendars() throws {
  for (calendar, writable, attendees, otherOrganizer) in [
    ("cal1", false, false, false), ("other", true, false, false),
    ("cal1", true, true, false), ("cal1", true, false, true),
    ("cal1", true, true, true),
  ] {
    #expect(throws: BridgeError.self) {
      try ItemEdits.requireEvent(
        calendarID: calendar, writable: writable, allowed: ["cal1"],
        hasAttendees: attendees, otherOrganizer: otherOrganizer)
    }
  }
  try ItemEdits.requireEvent(
    calendarID: "cal1", writable: true, allowed: ["cal1"],
    hasAttendees: false, otherOrganizer: false)
  // NativeBackend saves with this exact span, including for recurring events.
  #expect(ItemEdits.eventSpan == .thisEvent)
}

@Test func partialEventEditsValidateMergedDates() throws {
  let start = try Request.parseDate("2026-11-01T18:00:00Z")
  let end = try Request.parseDate("2026-11-01T19:00:00Z")
  func request(_ changes: [String: JSONValue]) throws -> Request {
    try Request(
      data: JSONEncoder().encode(
        JSONValue.object([
          "method": .string("events.update"),
          "args": .object(
            changes.merging([
              "event_id": .string("e1"), "calendar_ids": .array([.string("cal1")]),
            ]) { _, new in new }),
        ])))
  }
  let unchanged = try ItemEdits.eventDates(
    request(["title": .string("Changed")]), start: start, end: end)
  #expect(unchanged.0 == start && unchanged.1 == end)
  let laterEnd = try ItemEdits.eventDates(
    request(["end": .string("2026-11-01T20:00:00Z")]), start: start, end: end)
  #expect(laterEnd.0 == start)
  #expect(laterEnd.1 == end.addingTimeInterval(3600))
  for changes: [String: JSONValue] in [
    ["start": .string("2026-11-01T19:00:00Z")],
    ["end": .string("2026-11-01T17:00:00Z")],
    ["end": .string("2028-11-01T20:00:00Z")],
  ] {
    #expect(throws: BridgeError.self) {
      try ItemEdits.eventDates(request(changes), start: start, end: end)
    }
  }
}

@Test func reminderDueDaysKeepNoTimeAndDatetimesPreserveTheirInstant() throws {
  let day = try ItemEdits.dueComponents("2026-11-01")
  #expect(day.year == 2026 && day.month == 11 && day.day == 1)
  #expect(day.hour == nil && day.minute == nil && day.second == nil)
  #expect(day.timeZone == .current)
  let value = "2026-11-01T12:34:56-05:00"
  let timed = try ItemEdits.dueComponents(value)
  #expect(Calendar.current.date(from: timed) == (try Request.parseDate(value)))
  for invalid in ["2026-02-30", "tomorrow", "2026-1-01"] {
    #expect(throws: BridgeError.self) { try ItemEdits.dueComponents(invalid) }
  }
}

@Test func reminderEditsRequireWritableCurrentList() throws {
  try ItemEdits.requireReminder(listID: "list1", writable: true, allowed: ["list1"])
  for (id, writable, allowed) in [
    ("other", true, ["list1"]), ("list1", false, ["list1"]), ("list1", true, []),
  ] {
    #expect(throws: BridgeError.self) {
      try ItemEdits.requireReminder(listID: id, writable: writable, allowed: allowed)
    }
  }
}

@Test func remindersCreateAndUpdateAcceptDaysAndSerializeDaysWithoutTime() throws {
  for method in ["reminders.add", "reminders.update"] {
    let base: [String: JSONValue] =
      method == "reminders.add"
      ? ["list_id": .string("list1"), "title": .string("Task")]
      : ["reminder_id": .string("r1"), "list_ids": .array([.string("list1")])]
    for value in ["2026-11-20", "2026-11-20T12:34:56-05:00"] {
      let request = try Request(
        data: JSONEncoder().encode(
          JSONValue.object([
            "method": .string(method),
            "args": .object(base.merging(["due": .string(value)]) { _, new in new }),
          ])))
      let components = try ItemEdits.dueComponents(request.text("due")!)
      let result = ItemEdits.dueJSON(components)
      if value.count == 10 {
        #expect(result == .string(value))
      } else {
        guard case .string(let serialized) = result else {
          Issue.record("missing datetime")
          continue
        }
        #expect(try Request.parseDate(serialized) == Request.parseDate(value))
      }
    }
    for value in ["2026-02-30", "2026-1-01", "tomorrow"] {
      #expect(throws: BridgeError.self) {
        try Request(
          data: JSONEncoder().encode(
            JSONValue.object([
              "method": .string(method),
              "args": .object(base.merging(["due": .string(value)]) { _, new in new }),
            ])))
      }
    }
  }
  #expect(ItemEdits.dueJSON(nil) == .null)
}
