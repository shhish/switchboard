"""Reload and enforce policy at every call, failing closed on invalid policy."""

import re
from pathlib import Path

import yaml

from .auth import CLIENTS
from .errors import CapabilityError


class Policy:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            data = yaml.safe_load(self.path.read_text())
            if not isinstance(data, dict) or set(data) - {"clients", "allowlists"}:
                raise ValueError
            clients = data.get("clients", {})
            if not isinstance(clients, dict) or set(clients) - set(CLIENTS):
                raise ValueError
            for rule in clients.values():
                if not isinstance(rule, dict) or set(rule) - {
                    "scopes",
                    "trust_client_confirmation",
                }:
                    raise ValueError
                scopes = rule.get("scopes", [])
                if (
                    not isinstance(scopes, list)
                    or not all(
                        isinstance(s, str) and re.fullmatch(r"[a-z]+:[a-z_]+", s) for s in scopes
                    )
                    or type(rule.get("trust_client_confirmation", False)) is not bool
                ):
                    raise ValueError
            allowlists = data.get("allowlists", {})
            if not isinstance(allowlists, dict):
                raise ValueError
            for domain, lists in allowlists.items():
                if not isinstance(domain, str) or not isinstance(lists, dict):
                    raise ValueError
                if domain == "git":
                    from .gitea import SLUG

                    entries = lists.get("write_repos", [])
                    if (
                        set(lists) - {"write_repos"}
                        or not isinstance(entries, list)
                        or not all(
                            isinstance(v, str) and re.fullmatch(SLUG + r"/(?:" + SLUG + r"|\*)", v)
                            for v in entries
                        )
                    ):
                        raise ValueError
                if domain == "tasks" and set(lists) - {
                    "allow_all",
                    "projects",
                    "areas",
                    "deny_projects",
                    "deny_areas",
                }:
                    raise ValueError
                if domain == "home" and set(lists) - {
                    "allow_all_safe",
                    "accessories",
                    "scenes",
                    "deny_accessories",
                    "deny_scenes",
                    "secure_accessories",
                    "secure_scenes",
                }:
                    raise ValueError
                for kind, entries in lists.items():
                    if (domain, kind) in {
                        ("calendar", "default_calendar"),
                        ("reminders", "default_list"),
                    }:
                        if not isinstance(entries, str):
                            raise ValueError
                        continue
                    if (domain, kind) in (
                        ("home", "allow_all_safe"),
                        ("media", "allow_all"),
                        ("plex", "allow_all"),
                        ("tasks", "allow_all"),
                    ):
                        if type(entries) is not bool:
                            raise ValueError
                        continue
                    if not isinstance(entries, list) or not all(
                        isinstance(v, str) for v in entries
                    ):
                        raise ValueError
            return data
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            raise CapabilityError(
                "policy_invalid", "Policy missing or invalid; run sb doctor"
            ) from None

    def client(self, name):
        rule = self.load().get("clients", {}).get(name)
        if rule is None:
            raise CapabilityError("unknown_client", "Client is not authorized by policy")
        return rule

    def require(self, client, capability):
        rule = self.require_scope(client, capability.scope)
        if capability.name == "approvals_confirm" and not rule.get(
            "trust_client_confirmation", False
        ):
            raise CapabilityError("confirmation_denied", "Client confirmation is not trusted")
        return rule

    def require_scope(self, client, scope):
        rule = self.client(client)
        if scope not in rule.get("scopes", []):
            raise CapabilityError("scope_denied", "Required scope is not granted")
        return rule

    def allowed(self, client, capability):
        try:
            self.require(client, capability)
            return True
        except CapabilityError:
            return False

    def require_allowed(self, domain, kind, identifier):
        """Writes must call this with a stable resource ID before mutation.

        Media requests and Plex watch state are low-risk and reversible, so
        `media.allow_all` / `plex.allow_all` may admit any ID; `deny_<kind>` still wins.
        """
        lists = self.load().get("allowlists", {}).get(domain, {})
        if identifier in lists.get("deny_" + kind, []):
            raise CapabilityError("resource_denied", "Resource is denied by policy")
        blanket = domain in ("media", "plex") and lists.get("allow_all", False)
        if not blanket and identifier not in self.allowed_ids(domain, kind, rules=lists):
            raise CapabilityError("resource_denied", "Resource is not allowlisted")

    def require_git_repo(self, repo):
        entries = self.load().get("allowlists", {}).get("git", {}).get("write_repos", [])
        if repo not in entries and repo.split("/", 1)[0] + "/*" not in entries:
            raise CapabilityError("resource_denied", "Git repository is not allowlisted for writes")

    def allowed_ids(self, domain, kind, *, rules=None):
        """Effective IDs from one snapshot; explicit denials always win."""
        lists = rules if rules is not None else self.load().get("allowlists", {}).get(domain, {})
        entries = lists.get(kind, [])
        default = {
            ("calendar", "calendars"): "default_calendar",
            ("reminders", "lists"): "default_list",
        }
        fallback = lists.get(default.get((domain, kind)), "")
        positive = list(entries) if entries else ([fallback] if fallback else [])
        denied = set(lists.get("deny_" + kind, []))
        return [identifier for identifier in positive if identifier not in denied]

    def require_home(self, kind, identifier, *, secure=False, opaque=False):
        """Deny wins; secure resources always require explicit owner opt-in.

        An opaque resource (a scene whose actions HomeRelay does not expose) could
        unlock a door, so allow_all_safe never covers it: it must be listed by ID.
        """
        lists = self.load().get("allowlists", {}).get("home", {})
        if identifier in lists.get("deny_" + kind, []):
            raise CapabilityError("resource_denied", "Home resource is denied by policy")
        if secure:
            allowed = identifier in lists.get("secure_" + kind, [])
        else:
            if identifier in lists.get("secure_" + kind, []):
                tool = "home_set_secure" if kind == "accessories" else "home_run_scene_secure"
                raise CapabilityError("secure_required", f"Use {tool} for this resource")
            blanket = lists.get("allow_all_safe", False) and not opaque
            allowed = blanket or identifier in lists.get(kind, [])
        if not allowed:
            raise CapabilityError("resource_denied", "Home resource is not allowlisted")

    def require_compress_path(self, value):
        """Resolve components and symlinks; media.allow_all never bypasses these roots."""
        if not isinstance(value, str) or not 1 <= len(value) <= 1024 or "\x00" in value:
            raise CapabilityError("invalid_arguments", "Invalid absolute library path")
        path = Path(value)
        if not path.is_absolute():
            raise CapabilityError("invalid_arguments", "Invalid absolute library path")
        roots = self.allowed_ids("media", "compress_roots")
        try:
            resolved = path.resolve()
            allowed = any(
                "\x00" not in root
                and Path(root).is_absolute()
                and resolved.is_relative_to(Path(root).resolve())
                for root in roots
            )
        except (OSError, ValueError, RuntimeError):
            allowed = False
        if not allowed:
            raise CapabilityError("resource_denied", "Library path is not allowlisted")

    def task_rules(self):
        """One policy snapshot for the helper's current-destination write check.

        Absence fails closed; Inbox is allowed only with an explicit tasks rule.
        Denied areas also deny tasks in their projects. Allowed projects OR areas
        admit a task when allow_all is false; deny always takes precedence.
        """
        rules = self.load().get("allowlists", {}).get("tasks")
        if rules is None or "allow_all" not in rules:
            raise CapabilityError("resource_denied", "Things writes need an explicit tasks policy")
        return {
            "allow_all": rules["allow_all"],
            **{
                key: list(rules.get(key, []))
                for key in ("projects", "areas", "deny_projects", "deny_areas")
            },
        }

    def require_task(self, rules, project_id=None, area_id=None):
        if project_id in rules["deny_projects"] or area_id in rules["deny_areas"]:
            raise CapabilityError("resource_denied", "Things destination denied by policy")
        if not (
            rules["allow_all"]
            or not (project_id or area_id)
            or project_id in rules["projects"]
            or area_id in rules["areas"]
        ):
            raise CapabilityError("resource_denied", "Things destination is not allowlisted")
