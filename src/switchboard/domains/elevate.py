"""Phone-approved root requests over the signed native helper's fixed socket API."""

import json
from pathlib import Path

from .. import elevate
from ..schema import obj, string
from .board import ASK, ASK_ID, CARD_REF, ROOT_ARGS, ROOT_KEY, ROOT_STEPS, ROOT_TIMEOUT
from .common import array

REASON = string(300)


async def root_card(core, client, card=None):
    if card is not None:
        return card
    result = await core.board.add(
        client, {"title": "Root commands on the Mac", "column": "Up next"}
    )
    return result["card"]


async def request(core, client, args):
    reason = args["reason"]
    if not reason.strip():
        elevate.invalid("Give a plain reason for the root request")
    action = {
        "type": "run_as_root",
        **{k: v for k, v in args.items() if k not in {"card", "reason"}},
    }
    # Validate before creating a fallback card, so invalid requests have no board side effects.
    await validate(core, action)
    card = await root_card(core, client, args.get("card"))
    result = await core.board.ask(
        client, {"card": card, "kind": "confirm", "prompt": reason, "action": action}
    )
    return {"status": "ok", "ask": result["ask"]}


async def validate(core, action):
    try:
        status = await core.elevate.status()
    except elevate.NotInstalled:
        elevate.invalid("Root helper not installed")
    elevate.validate_root(action, status)


async def update(core, client, args):
    return await request(
        core,
        client,
        {
            "card": args.get("card"),
            "reason": args["reason"],
            "manage": "update-" + args["what"],
            "path": args["path"],
        },
    )


async def pair(core, client, args):
    public = elevate.key(args)
    code = elevate.fingerprint(public)
    try:
        status = await core.elevate.status()
    except elevate.NotInstalled:
        status = None
    if status is None or not status["keys"]:
        elevate.write_pending(public)
        path = Path.home() / ".local/share/switchboard/current/scripts/elevate-install.sh"
        # The Mac shows its own password box naming this code; typing the password once
        # installs the helper. The command stays as a fallback for a Mac without a screen.
        opened = elevate.open_setup(code)
        return {
            "status": "ok",
            "mode": "install",
            "fingerprint": code,
            "password_box": opened,
            "command": "sudo " + str(path),
        }
    result = await request(
        core,
        client,
        {
            "manage": "add-key",
            "key": public,
            "reason": f"Pair {public['name']} for root commands on this Mac?",
        },
    )
    return {"status": "ok", "mode": "approve", "fingerprint": code, "ask_id": result["ask"]["id"]}


async def status(core, client, args):
    try:
        data = await core.elevate.status()
    except elevate.NotInstalled:
        data = None
    pending = None
    try:
        value = json.loads(elevate.read_update(str(elevate.pending_path()), 4096))
        public = elevate.key({k: value[k] for k in ("name", "x963")})
        if type(value["created"]) is int:
            pending = {
                "name": public["name"],
                "fingerprint": elevate.fingerprint(public),
                "created": value["created"],
            }
    except (elevate.CapabilityError, ValueError, KeyError, TypeError):
        pass
    # The install script keeps the public pending file; hide an already installed key.
    if (
        data
        and pending
        and any(k.get("fingerprint") == pending["fingerprint"] for k in data["keys"])
    ):
        pending = None
    result = {
        "status": "ok",
        "installed": data is not None,
        "version": data["version"] if data else None,
        "keys": [{k: row[k] for k in ("name", "fingerprint")} for row in data["keys"][:100]]
        if data
        else [],
        "recipes": [
            {
                "id": r["id"],
                "title": r["title"],
                "detail": r.get("detail", ""),
                "args": [
                    {k: a[k] for k in ("name", "label", "pattern", "choices") if k in a}
                    for a in r.get("args", [])
                ],
                "destructive": r.get("destructive", False),
            }
            for r in data["recipes"][:100]
        ]
        if data
        else [],
        "pending": pending,
        "truncated": bool(data and (len(data["keys"]) > 100 or len(data["recipes"]) > 100)),
    }
    # Bound by bytes as well as count; omit whole recipes rather than cutting fields.
    while len(json.dumps(result)) > 100000 and result["recipes"]:
        result["recipes"].pop()
        result["truncated"] = True
    return result


def register(registry):
    key_info = obj({"name": string(40), "fingerprint": string(19)}, ("name", "fingerprint"))
    arg = obj(
        {
            "name": string(31),
            "label": string(200),
            "pattern": string(10000),
            "choices": array(string(1024)),
        },
        ("name",),
    )
    recipe = obj(
        {
            "id": string(41),
            "title": string(10000),
            "detail": {"type": "string", "maxLength": 100000},
            "args": array(arg),
            "destructive": {"type": "boolean"},
        },
        ("id", "title", "detail", "args", "destructive"),
    )
    definitions = [
        (
            "elevate_status",
            "Root helper status",
            (
                "Root helper and phone pairing. Use when checking available root recipes "
                "or pairing "
                "progress. Requires elevate:read. Returns installation, version, up to "
                "100 keys and "
                "recipes, pending public-key fingerprint and truncation flag; bounded to 100000 "
                "characters."
            ),
            {},
            (),
            "elevate:read",
            False,
            status,
            {
                "installed": {"type": "boolean"},
                "version": {"type": ["string", "null"]},
                "keys": array(key_info),
                "recipes": array(recipe),
                "pending": {
                    **obj(
                        {**key_info["properties"], "created": {"type": "integer"}},
                        ("name", "fingerprint", "created"),
                    ),
                    "type": ["object", "null"],
                },
                "truncated": {"type": "boolean"},
            },
        ),
        (
            "elevate_request",
            "Request root command",
            (
                "Root command approval on this Mac. Use when a task needs root; prefer a listed "
                "recipe. Requires elevate:request, installed helper and allowlisted card; supply "
                "recipe/args or 1-5 argv steps, reason and optional timeout 5-7200s. Returns a "
                "destructive confirm ask with exact bytes for the paired phone to sign. Wait with "
                "board_ask_wait; only the helper executes."
            ),
            {
                "card": CARD_REF,
                "recipe": string(41),
                "args": ROOT_ARGS,
                "steps": ROOT_STEPS,
                "timeout": ROOT_TIMEOUT,
                "reason": REASON,
            },
            ("reason",),
            "elevate:request",
            True,
            request,
            {"ask": ASK},
        ),
        (
            "elevate_update",
            "Approve root helper update",
            (
                "Root helper or recipe updates. Use when proposing a new recipe or helper binary. "
                "Requires elevate:request, installed helper, absolute local file path, reason and "
                "optional allowlisted card. Returns a phone-signable confirm ask binding "
                "file SHA256; "
                "recipe updates list added, removed and changed IDs. Only the helper "
                "installs approved "
                "bytes."
            ),
            {
                "card": CARD_REF,
                "what": {"type": "string", "enum": ["recipes", "helper"]},
                "path": string(4096),
                "reason": REASON,
            },
            ("what", "path", "reason"),
            "elevate:request",
            True,
            update,
            {"ask": ASK},
        ),
        (
            "elevate_pair",
            "Pair a phone for root approval",
            (
                "Root approval phone pairing. Use when the Server app registers its Secure Enclave "
                "public key. Requires elevate:pair; name 1-40 safe characters and base64 "
                "X9.63 public "
                "key (65 bytes, 0x04 prefix). Returns fingerprint plus one-time install command if "
                "uninstalled/keyless, or ask_id for approval by an already paired phone."
            ),
            ROOT_KEY["properties"],
            ("name", "x963"),
            "elevate:pair",
            True,
            pair,
            {
                "mode": {"type": "string", "enum": ["install", "approve"]},
                "fingerprint": string(19),
                "password_box": {"type": "boolean"},
                "command": string(4096),
                "ask_id": ASK_ID,
            },
        ),
    ]
    for name, title, description, inputs, required, scope, write, handler, outputs in definitions:
        registry.declare(
            name,
            title,
            description,
            obj(inputs, required),
            scope=scope,
            tier="write" if write else "read",
            idempotent=not write,
            timeout_s=180,
            service="switchboard",
            max_result_chars=500000 if "ask" in outputs else 110000,
            output_schema=obj({"status": string(20), **outputs}, ("status",)),
            audit_hash_fields=frozenset({"reason", "steps", "args", "path", "x963", "name"}),
            memory=None,
            memory_optout="Signed root requests and public pairing state are workflow receipts",
        )(handler)
