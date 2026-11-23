"""Typed Homebrew and Cloudflare operations using existing ops permissions."""

from .. import hostcmd
from ..schema import obj, string
from .common import array, output

NAME = {**string(128), "pattern": "^" + hostcmd.BREW_NAME + "$"}
LISTED_NAME = {**string(128), "pattern": "^" + hostcmd.BREW_LISTED_NAME + "$"}
HOSTNAME = string(253)
PACKAGE = obj(
    {
        "name": LISTED_NAME,
        "installed": {**array(string(100)), "maxItems": 8},
        "current": string(100),
        "major": {"type": "boolean"},
        "held": {"type": "boolean"},
        "pinned": {"type": "boolean"},
    },
    ("name", "installed", "current", "major", "held", "pinned"),
)
INVENTORY = output(
    formulae={**array(PACKAGE), "maxItems": 100}, casks={**array(PACKAGE), "maxItems": 100}
)
PROBE = obj(
    {
        "status": {"type": ["integer", "null"]},
        "latency_ms": {"type": ["number", "null"], "minimum": 0},
        "error": {"type": ["string", "null"], "enum": [None, "unreachable", "unsupported"]},
    },
    ("status", "latency_ms", "error"),
)
ROUTE_STATES = ["ok", "origin_down", "edge_error", "unknown"]
ROUTE = obj(
    {
        "hostname": HOSTNAME,
        "path": string(200),
        "state": {"type": "string", "enum": ROUTE_STATES},
        "origin": PROBE,
        "public": PROBE,
    },
    ("hostname", "path", "state", "origin", "public"),
)
JOB = output(job_id=string(64))
CHANGE = output(
    hostname=HOSTNAME,
    backup_path=string(2048),
    dns_left_in_place={"type": "boolean"},
    note=string(200),
    route=ROUTE,
)


def register(registry):
    common = {
        "service": "host",
        "memory": None,
        "memory_optout": "Bounded host maintenance and route observations",
    }

    @registry.declare(
        "ops_brew_outdated",
        "Outdated Homebrew packages",
        "Homebrew outdated formulae and casks. Use when checking Mac packages. Requires ops:read. "
        "Returns up to 100 of each with installed/current versions, major, held "
        "and pinned flags; cached ten minutes.",
        scope="ops:read",
        timeout_s=75,
        max_result_chars=300000,
        output_schema=INVENTORY,
        **common,
    )
    async def outdated(core, client, args):
        return await core.brew.outdated()

    @registry.declare(
        "ops_brew_refresh",
        "Refresh Homebrew metadata",
        "Homebrew metadata refresh without installs. Use when checking new "
        "updates. Requires ops:restart. "
        "Returns a job; poll jobs_get for the bounded outdated inventory after "
        "brew update (300s timeout).",
        scope="ops:restart",
        tier="write",
        idempotent=False,
        output_schema=JOB,
        **common,
    )
    async def refresh(core, client, args):
        async def operation(job_id):
            return await core.brew.refresh()

        return core.jobs.submit("ops_brew_refresh", client, operation)

    def upgrade_handler(allow_held):
        async def upgrade(core, client, args):
            # Fail held/major gates before accepting a job, then recheck at execution.
            async with core.brew.lock:
                await core.brew.target(args["name"], allow_held)

            async def operation(job_id):
                return await core.brew.upgrade(args["name"], allow_held)

            return core.jobs.submit(
                "ops_brew_upgrade_held" if allow_held else "ops_brew_upgrade", client, operation
            )

        return upgrade

    for allow_held in (False, True):
        registry.declare(
            "ops_brew_upgrade_held" if allow_held else "ops_brew_upgrade",
            "Approve one held or major Homebrew upgrade"
            if allow_held
            else "Upgrade one Homebrew package",
            "Homebrew single-package upgrade. Use when upgrading one named "
            "formula or cask. Requires ops:restart"
            + (
                " and owner approval for this package; permits held and major upgrades. "
                if allow_held
                else "; held and major upgrades are refused. "
            )
            + "Returns a job with before/after versions inferred from "
            "successful upgrade and fresh outdated inventory; "
            "900s command timeout, no bulk upgrade or cleanup commands.",
            obj({"name": NAME}, ("name",)),
            scope="ops:restart",
            tier="approval" if allow_held else "write",
            idempotent=False,
            timeout_s=75,
            output_schema=JOB,
            **common,
        )(upgrade_handler(allow_held))

    @registry.declare(
        "ops_tunnel_status",
        "Cloudflare tunnel routes",
        "Cloudflare tunnel connector and ingress health. Use when diagnosing "
        "public routes. Requires ops:read and tunnel config. "
        "Returns tunnel ID, launchd state and up to 100 routes with local "
        "5s/public 8s probes, status, latency and counts; eight "
        "concurrent GETs, no redirects or bodies.",
        scope="ops:read",
        timeout_s=200,
        max_result_chars=100000,
        output_schema=output(
            tunnel_id=string(36),
            credentials_file={"type": ["string", "null"], "maxLength": 1024},
            connector_state={"type": "string", "enum": ["running", "stopped", "unknown"]},
            routes={**array(ROUTE), "maxItems": 100},
            counts=obj(
                {state: {"type": "integer", "minimum": 0} for state in ROUTE_STATES}, ROUTE_STATES
            ),
        ),
        **common,
    )
    async def status(core, client, args):
        return await core.tunnel.status()

    def preflight(add):
        def check(core, client, args):
            core.tunnel.preflight(args["hostname"], args["port"] if add else None)

        return check

    def change_handler(add):
        async def change(core, client, args):
            return await core.tunnel.change(client, args["hostname"], args["port"] if add else None)

        return change

    for add in (True, False):
        registry.declare(
            "ops_tunnel_route_add" if add else "ops_tunnel_route_remove",
            "Add Cloudflare tunnel route" if add else "Remove Cloudflare tunnel route",
            "Cloudflare tunnel route "
            + ("addition" if add else "removal")
            + ". Use when changing one hostname under the configured zone. "
            "Requires ops:restart and owner approval. "
            + (
                "Port 1024–65535 maps to loopback HTTP. Backs up, validates, "
                "creates DNS, restarts. "
                if add
                else "Backs up, validates and restarts; DNS is left for the owner to delete. "
            )
            + "Returns backup and "
            + (
                "route probes; config rolls back only before DNS starts."
                if add
                else "DNS retention note; failures restore config."
            ),
            obj(
                {
                    "hostname": HOSTNAME,
                    **(
                        {"port": {"type": "integer", "minimum": 1024, "maximum": 65535}}
                        if add
                        else {}
                    ),
                },
                ("hostname", "port") if add else ("hostname",),
            ),
            scope="ops:restart",
            tier="approval",
            idempotent=False,
            timeout_s=120,
            preflight=preflight(add),
            output_schema=CHANGE,
            max_result_chars=10000,
            **common,
        )(change_handler(add))
