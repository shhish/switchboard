"""The shared Slate board: discovery, cards and allowlisted writes."""

from ..schema import obj, string
from .common import array

COLUMNS = ("Ideas", "Up next", "In progress", "Needs you", "Done")

B = {"type": "boolean"}
ID = {"type": "integer", "minimum": 1}
S = string(200)
COLUMN = {"type": "string", "enum": list(COLUMNS)}
CARD_REF = string(240)
NULL_TEXT = {"type": ["string", "null"], "maxLength": 200}
ASK_ID = {**string(20), "pattern": r"^ask_[0-9a-f]{16}$"}
LOCAL_ID = {**string(32), "pattern": r"^[a-z0-9][a-z0-9_-]{0,31}$"}
KIND = {"type": "string", "enum": ["confirm", "choice", "text", "form", "plan"]}
ACTION_TYPE = {
    "type": "string",
    "enum": ["grant_scope", "grant_scopes", "add_ask_media_root", "run_as_root"],
}
ROOT_STEPS = {
    **array({**array({"type": "string", "maxLength": 4096}), "minItems": 1, "maxItems": 64}),
    "minItems": 1,
    "maxItems": 5,
}
ROOT_ARGS = {"type": "object"}
ROOT_KEY = obj(
    {"name": {**string(40), "pattern": r"^[A-Za-z0-9 ._-]+$"}, "x963": string(88)}, ("name", "x963")
)
ROOT_TIMEOUT = {"type": "integer", "minimum": 5, "maximum": 7200}
ACTION = obj(
    {
        "type": ACTION_TYPE,
        "client": string(64),
        "scope": string(100),
        "grants": {
            **array(obj({"client": string(64), "scope": string(100)}, ("client", "scope"))),
            "minItems": 2,
            "maxItems": 8,
        },
        "path": string(1024),
        "recipe": string(41),
        "args": ROOT_ARGS,
        "steps": ROOT_STEPS,
        "timeout": ROOT_TIMEOUT,
        "manage": {"type": "string", "enum": ["add-key", "update-recipes", "update-helper"]},
        "key": ROOT_KEY,
    },
    ("type",),
)
ACTION["description"] = (
    "Confirm asks only. Owner yes applies a scope/picture-root change "
    "or phone-signed root request; "
    "Switchboard generates the summary and forces destructive confirmation."
)
ACTION_RESULT = obj(
    {
        "type": ACTION_TYPE,
        "summary": string(400000),
        "sign_payload": string(524288),
        "kind": {
            "type": "string",
            "enum": ["recipe", "custom", "add-key", "update-recipes", "update-helper"],
        },
        "recipe": string(41),
        "steps": {**ROOT_STEPS, "minItems": 0},
        "job": {**string(32), "pattern": "^[0-9a-f]{32}$"},
        "exit": {"type": "integer"},
        "output": {"type": "string", "maxLength": 4000},
        "changes": obj(
            {k: array(string(100)) for k in ("added", "removed", "changed")},
            ("added", "removed", "changed"),
        ),
        "result": {"type": "string", "enum": ["applied", "already", "failed", "running"]},
        "message": string(2000),
    },
    ("type", "summary"),
)
# Base64 plus the response envelope must fit the registry's 500,000 character cap.
ASK_MEDIA_MAX_BYTES = 360 * 1024
OPTION = obj(
    {"id": LOCAL_ID, "label": string(80), "detail": string(200), "image": string(4096)},
    ("id", "label"),
)
PICTURE = obj({"image": string(4096), "caption": string(120)}, ("image",))
STEP = obj(
    {"id": LOCAL_ID, "label": string(120), "detail": string(200), "optional": B},
    ("id", "label"),
)
EVIDENCE = obj(
    {
        "label": string(100),
        "value": string(40),
        "state": {"type": "string", "enum": ["done", "failed", "pending"]},
    },
    ("label",),
)
FIELD_OPTION = obj({"id": LOCAL_ID, "label": string(80)}, ("id", "label"))
FIELD = obj(
    {
        "id": LOCAL_ID,
        "label": string(80),
        "type": {
            "type": "string",
            "enum": ["text", "multiline", "number", "toggle", "choice", "date"],
        },
        "required": B,
        "placeholder": string(80),
        "options": {**array(FIELD_OPTION), "minItems": 2, "maxItems": 8},
    },
    ("id", "label", "type", "required"),
)
ANSWER = obj(
    {
        "signature": {
            **string(200),
            "description": (
                "Base64 DER signature over action.sign_payload bytes, from the paired phone."
            ),
        },
        "choice": {**array(LOCAL_ID), "maxItems": 8},
        "other": string(500),
        "text": string(4000),
        "note": string(1000),
        "approve": B,
        "steps": {**array(LOCAL_ID), "maxItems": 10},
        # Field IDs and value types are validated against the stored form in the handler.
        "values": {"type": "object"},
    }
)
ASK_PROPERTIES = {
    "id": ASK_ID,
    "card": CARD_REF,
    "from": S,
    "kind": KIND,
    "prompt": string(300),
    "pictures": {
        **array(
            obj(
                {
                    "id": {"type": "string", "enum": ["pic1", "pic2", "pic3", "pic4"]},
                    "caption": {"type": ["string", "null"], "minLength": 1, "maxLength": 120},
                },
                ("id",),
            )
        ),
        "maxItems": 4,
    },
    "detail": {"type": ["string", "null"], "maxLength": 2000},
    "options": {
        **array(
            obj(
                {
                    "id": LOCAL_ID,
                    "label": string(80),
                    "detail": {"type": ["string", "null"], "maxLength": 200},
                    "image": B,
                },
                ("id", "label", "detail", "image"),
            )
        ),
        "maxItems": 8,
    },
    "multiple": B,
    "allow_other": B,
    "recommended": {"type": ["string", "null"], "maxLength": 32},
    "destructive": B,
    "fields": {
        **array(
            obj(
                {
                    **FIELD["properties"],
                    "placeholder": {"type": ["string", "null"], "maxLength": 80},
                    "options": {**array(FIELD_OPTION), "maxItems": 8},
                },
                FIELD["properties"],
            )
        ),
        "maxItems": 8,
    },
    "steps": {
        **array(obj({**STEP["properties"], "detail": NULL_TEXT}, STEP["properties"])),
        "maxItems": 10,
    },
    "evidence": {
        **array(
            obj(
                {**EVIDENCE["properties"], "value": {"type": ["string", "null"], "maxLength": 40}},
                EVIDENCE["properties"],
            )
        ),
        "maxItems": 8,
    },
    "state": {"type": "string", "enum": ["open", "answered", "withdrawn"]},
    "created": string(80),
    "answered": {"type": ["string", "null"], "maxLength": 80},
    "answered_by": NULL_TEXT,
    "answer": {**ANSWER, "type": ["object", "null"]},
    "answer_text": {"type": ["string", "null"], "maxLength": 600},
}
ASK = obj({**ASK_PROPERTIES, "action": ACTION_RESULT}, ASK_PROPERTIES)
ANSWERED_PROPERTIES = {
    "ask_id": ASK_ID,
    "card": CARD_REF,
    "from": S,
    "prompt": string(300),
    "answer_text": {"type": "string", "maxLength": 600},
    "answered": string(80),
    "answered_by": S,
}

SUMMARY = {
    "card": CARD_REF,
    "repo": string(100),
    "number": ID,
    "title": {"type": "string", "maxLength": 200},
    "column": COLUMN,
    "state": {"type": "string", "enum": ["open", "closed"]},
    "updated": {"type": "string", "maxLength": 80},
    "url": {"type": "string", "maxLength": 600},
    "labels": {**array({"type": "string", "maxLength": 80}), "maxItems": 20},
    "claimed_by": NULL_TEXT,
    "last_note": NULL_TEXT,
    "last_actor": NULL_TEXT,
    "asks_open": {"type": "integer", "minimum": 0},
}
CARD = obj({**SUMMARY, "open_asks": {**array(ASK), "maxItems": 3}}, (*SUMMARY, "open_asks"))


def register(registry):
    def declare(
        name,
        title,
        description,
        inputs,
        fields,
        *,
        required=(),
        write=False,
        scope=None,
        timeout_s=180,
    ):
        async def handler(core, client, args):
            return await getattr(core.board, name.removeprefix("board_"))(client, args)

        registry.declare(
            name,
            title,
            description,
            obj(inputs, required),
            scope=scope or ("board:write" if write else "board:read"),
            tier="write" if write else "read",
            idempotent=not write,
            timeout_s=timeout_s,
            service="gitea",
            max_result_chars=500_000
            if name
            in {"board_list", "board_card", "board_answer", "board_ask_wait", "board_ask_media"}
            else 150_000,
            output_schema=obj(
                {"status": S, **fields}, ("status", *(key for key in fields if key != "truncated"))
            ),
            audit_hash_fields=frozenset(
                {
                    "title",
                    "body",
                    "note",
                    "prompt",
                    "detail",
                    "options",
                    "pictures",
                    "fields",
                    "steps",
                    "evidence",
                    "answer",
                    "action",
                    "yes_label",
                    "no_label",
                    "recommended",
                }
            ),
            memory=None,
            memory_optout="Board cards and actor receipts are workflow state, not shared facts",
        )(handler)

    declare(
        "board_list",
        "Slate board",
        "Slate board: cards grouped in board order. Use when choosing or checking shared work. "
        "Requires board:read. Returns sanitized untrusted summaries, claims, open asks and "
        "answers from the last 72 hours; "
        "bounded five-column paging, limit 1-100 (60 default). Done is hidden unless requested, "
        "capped at 15; closed issues count as Done. Partial views are marked.",
        {
            "project": {
                **string(201),
                "description": "Filter by repository name or configured-owner/name",
            },
            "column": COLUMN,
            "include_done": {**B, "description": "Include Done; defaults false"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        {
            "board": obj(
                {"title": S, "url": {"type": "string", "maxLength": 600}}, ("title", "url")
            ),
            "columns": {
                **array(
                    obj(
                        {
                            "title": COLUMN,
                            "count": {"type": "integer", "minimum": 0},
                            "cards": {
                                **array(CARD),
                                "maxItems": 100,
                            },
                        },
                        ("title", "count", "cards"),
                    )
                ),
                "maxItems": 5,
            },
            "answered": {
                **array(obj({**ANSWERED_PROPERTIES, "action": ACTION_RESULT}, ANSWERED_PROPERTIES)),
                "maxItems": 10,
            },
            "needs_you": {"type": "integer", "minimum": 0},
            "truncated": B,
        },
    )
    declare(
        "board_card",
        "Slate card detail",
        "Slate card detail and discussion. Use when reading shared work before acting. "
        "Requires board:read and attached repo#number "
        "(owner/repo#number or general #number accepted). "
        "Returns sanitized untrusted summary, latest 10 asks, body up to 6000 and "
        "latest 30 comments "
        "up to 1500 characters each; absent membership is not_found.",
        {"card": CARD_REF},
        {
            **SUMMARY,
            "asks": {**array(ASK), "maxItems": 10},
            "body": {"type": "string", "maxLength": 6000},
            "created": {"type": "string", "maxLength": 80},
            "comments": {
                **array(
                    obj(
                        {
                            "author": {"type": "string", "maxLength": 100},
                            "created": {"type": "string", "maxLength": 80},
                            "body": {"type": "string", "maxLength": 1500},
                        },
                        ("author", "created", "body"),
                    )
                ),
                "maxItems": 30,
            },
            "truncated": B,
        },
        required=("card",),
    )
    declare(
        "board_add",
        "Add Slate card",
        "Slate card creation. Use when capturing an idea or follow-up. Requires board:write, "
        "an allowlisted repo and title up to 200 characters; body up to 8000. Repo defaults to the "
        "general repo and column to Ideas; Done is forbidden. Returns sanitized untrusted summary "
        "and duplicate flag; matching open repo/title creates nothing. Otherwise non-idempotent.",
        {
            "title": string(200),
            "repo": string(201),
            "body": {"type": "string", "maxLength": 8000},
            "column": {"type": "string", "enum": list(COLUMNS[:-1])},
        },
        {**SUMMARY, "duplicate": B},
        required=("title",),
        write=True,
    )
    declare(
        "board_move",
        "Move Slate card",
        "Slate card movement and claiming. Use when starting, finishing or blocking work. "
        "Requires board:write, attached card and allowlisted repo; Needs you requires an exact "
        "physical owner action in note (up to 2000 characters). "
        "In progress claims for the caller; Done closes, "
        "others reopen. Notes post as attributed comments. Returns sanitized untrusted summary.",
        {
            "card": CARD_REF,
            "column": COLUMN,
            "note": {
                **string(2000),
                "description": "Required for Needs you: exactly what the owner must do",
            },
        },
        SUMMARY,
        required=("card", "column"),
        write=True,
    )
    declare(
        "board_comment",
        "Comment on Slate card",
        "Slate card progress comments. Use when recording progress or context. Requires "
        "board:write, an attached card, allowlisted repo and body up to 4000 characters. "
        "Posts with the authenticated caller's attribution and records a local last note/actor. "
        "Returns comment ID and sanitized untrusted card summary. Non-idempotent.",
        {"card": CARD_REF, "body": string(4000)},
        {"comment_id": ID, **SUMMARY},
        required=("card", "body"),
        write=True,
    )
    declare(
        "board_ask",
        "Ask the owner on Slate",
        "Slate structured questions. Use when the owner must decide or supply values. "
        "Requires board:write, allowlisted open card; five open asks max. Returns untrusted "
        "confirm/choice/text/form/plan ask; 8 evidence lines, 8 option/4 ask pictures "
        "(PNG/JPEG, 360 KiB, configured roots). Confirm action forces destructive. "
        "Moves to Needs you. No secrets. Non-idempotent.",
        {
            "card": CARD_REF,
            "kind": KIND,
            "action": ACTION,
            "prompt": string(300),
            "pictures": {**array(PICTURE), "maxItems": 4},
            "detail": string(2000),
            "options": {**array(OPTION), "minItems": 2, "maxItems": 8},
            "multiple": B,
            "allow_other": B,
            "recommended": LOCAL_ID,
            "yes_label": string(40),
            "no_label": string(40),
            "destructive": B,
            "fields": {**array(FIELD), "minItems": 1, "maxItems": 8},
            "steps": {**array(STEP), "minItems": 1, "maxItems": 10},
            "evidence": {**array(EVIDENCE), "maxItems": 8},
        },
        {**SUMMARY, "ask": ASK},
        required=("card", "kind", "prompt"),
        write=True,
    )
    declare(
        "board_ask_media",
        "Slate ask picture",
        "Slate ask pictures. Use when viewing an option picture or an ask's own picture. "
        "Requires board:read, ask_id and option_id (option ID or pic1-pic4). "
        "Returns untrusted PNG or JPEG as base64, media type and byte count, "
        "up to 360 KiB; missing asks, options or pictures are not_found. History pictures "
        "are pruned after 30 days closed.",
        {
            "ask_id": ASK_ID,
            "option_id": {
                **LOCAL_ID,
                "description": "Option ID or pic1-pic4 for the ask's own pictures.",
            },
        },
        {
            "ask_id": ASK_ID,
            "option_id": LOCAL_ID,
            "media_type": {"type": "string", "enum": ["image/png", "image/jpeg"]},
            "bytes": {"type": "integer", "minimum": 1, "maximum": ASK_MEDIA_MAX_BYTES},
            "data_base64": string(4 * ((ASK_MEDIA_MAX_BYTES + 2) // 3)),
        },
        required=("ask_id", "option_id"),
    )
    declare(
        "board_ask_wait",
        "Wait for a Slate answer",
        "Slate ask waiting. Use when work can continue after an answer or root job. Requires "
        "board:read and ask_id; timeout_s 0-280, default 120. Returns sanitized untrusted ask, "
        "waited_s and timed_out; polls every two seconds, refreshing running root jobs from the "
        "helper. Answered root asks keep waiting while running. Unknown IDs are not_found; "
        "withdrawal ends waiting.",
        {"ask_id": ASK_ID, "timeout_s": {"type": "integer", "minimum": 0, "maximum": 280}},
        {"ask": ASK, "waited_s": {"type": "number", "minimum": 0}, "timed_out": B},
        required=("ask_id",),
        timeout_s=300,
    )
    declare(
        "board_ask_withdraw",
        "Withdraw a Slate ask",
        "Slate ask withdrawal. Use when a question no longer matters. Requires board:write, "
        "allowlisted repo and an open ask created by this client. Returns sanitized untrusted "
        "summary and withdrawn ask; comments and restores the prior column only after the "
        "last open ask, while the card remains open in Needs you. Non-idempotent.",
        {"ask_id": ASK_ID},
        {**SUMMARY, "ask": ASK},
        required=("ask_id",),
        write=True,
    )
    declare(
        "board_answer",
        "Answer a Slate ask",
        "Slate structured answers. Use when the owner's surface answers an ask. Requires "
        "board:answer, allowlisted repo and another client's open ask (own add-key asks need "
        "elevate:pair). Returns untrusted summary, answered ask and action result. Yes applies "
        "scope/media-root actions; root actions require a paired phone signature. Comments and "
        "restores its column after the last open ask. Non-idempotent.",
        {"ask_id": ASK_ID, "answer": ANSWER},
        {**SUMMARY, "ask": ASK},
        required=("ask_id", "answer"),
        write=True,
        scope="board:answer",
    )
    declare(
        "board_import_things",
        "Import Things board captures",
        "Things capture into Slate Ideas. Use when importing phone ideas now. "
        "Requires board:write, tasks:write, configured project and Git/Things allowlists. "
        "Returns up to 20 sanitized untrusted imported titles, completed/skipped counts and "
        "error codes. Creates a missing project without an area; mappings precede completion. "
        "Unset or unavailable Things is quiet; bounded reads may truncate.",
        {},
        {
            "imported": {
                **array(
                    obj(
                        {
                            "things_id": string(1000),
                            "card": CARD_REF,
                            "title": {"type": "string", "maxLength": 200},
                        },
                        ("things_id", "card", "title"),
                    )
                ),
                "maxItems": 20,
            },
            "completed": {"type": "integer", "minimum": 0, "maximum": 20},
            "skipped": {"type": "integer", "minimum": 0, "maximum": 800},
            "errors": {
                **array(
                    obj(
                        {
                            "things_id": {"type": ["string", "null"], "maxLength": 1000},
                            "code": string(80),
                        },
                        ("things_id", "code"),
                    )
                ),
                "maxItems": 20,
            },
            "truncated": B,
        },
        write=True,
    )
