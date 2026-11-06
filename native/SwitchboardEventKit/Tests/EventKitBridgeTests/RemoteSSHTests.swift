import Foundation
import Testing

@testable import EventKitBridge

@Test func remoteRequestsFollowTheAgentGrammar() {
  for good in ["version", "snapshot", "checks", "refresh-updates", "status", "status homebridge",
    "restart adguard", "logs kuma 50", "logs adguard 400", "ps5 status", "ps5 wake", "ps5 standby",
    "dns-stats", "dns-resume", "dns-pause 1", "dns-pause 120"]
  {
    #expect(RemoteSSH.validRequest(good), "\(good)")
  }
  for bad in ["", " version", "version ", "version x", "status a b", "restart", "logs kuma",
    "logs kuma 0", "logs kuma 401", "logs kuma 5x", "id; ls", "Status", "reboot",
    "restart homebridge && id", String(repeating: "a", count: 41), "ps5", "ps5 reboot",
    "ps5 wake now", "ps5 Wake", "dns-pause", "dns-pause 0", "dns-pause 121", "dns-pause 5x",
    "dns-pause -5", "dns-stats now", "dns-resume now"]
  {
    #expect(!RemoteSSH.validRequest(bad), "\(bad)")
  }
}

@Test func remoteHostsArePrivateLiteralsAndUsersAreStrict() {
  for good in ["\(ip(192, 168, 50, 2))", "\(ip(10, 0, 0, 1))", "172.16.0.1", "\(ip(100, 100, 0, 1))"] {
    #expect(RemoteSSH.validAddress(good))
  }
  for bad in ["8.8.8.8", "\(ip(192, 168, 50))", "\(ip(192, 168, "050", 2))", "remotehost.local", "100.128.0.1", "::1"] {
    #expect(!RemoteSSH.validAddress(bad))
  }
  #expect(RemoteSSH.validUser("serviceuser") && RemoteSSH.validUser("_svc-1"))
  #expect(!RemoteSSH.validUser("Root") && !RemoteSSH.validUser("a b") && !RemoteSSH.validUser("-x"))
}

@Test func remoteCredentialsMustLiveInSwitchboardConfig() {
  #expect(!RemoteSSH.validPrivateFile("/etc/ssh/ssh_host_ed25519_key"))
  let home = FileManager.default.homeDirectoryForCurrentUser.path
  #expect(!RemoteSSH.validPrivateFile(home + "/.config/switchboard/../../.ssh/id_ed25519"))
  #expect(!RemoteSSH.validPrivateFile(home + "/.ssh/id_ed25519"))
}

@Test func remoteDeadlinesCoverSlowCommands() {
  #expect(RemoteSSH.deadline(for: "restart homebridge") == 100)
  #expect(RemoteSSH.deadline(for: "refresh-updates") == 190)
  #expect(RemoteSSH.deadline(for: "ps5 wake") == 70)
  #expect(RemoteSSH.deadline(for: "snapshot") == 20)
}
