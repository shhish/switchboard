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
KIND = {"type": "string", "enum": ["confirm", "choice", "text", "form"]}
OPTION = obj({"id": LOCAL_ID, "label": string(80), "detail": string(200)}, ("id", "label"))
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
        "choice": {**array(LOCAL_ID), "maxItems": 8},
        "other": string(500),
        "text": string(4000),
        "note": string(1000),
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
    "detail": {"type": ["string", "null"], "maxLength": 2000},
    "options": {
        **array(
            obj(
                {
                    "id": LOCAL_ID,
                    "label": string(80),
                    "detail": {"type": ["string", "null"], "maxLength": 200},
                },
                ("id", "label", "detail"),
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
    "state": {"type": "string", "enum": ["open", "answered", "withdrawn"]},
    "created": string(80),
    "answered": {"type": ["string", "null"], "maxLength": 80},
    "answered_by": NULL_TEXT,
    "answer": {**ANSWER, "type": ["object", "null"]},
    "answer_text": {"type": ["string", "null"], "maxLength": 600},
}
ASK = obj(ASK_PROPERTIES, ASK_PROPERTIES)
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
            if name in {"board_list", "board_card", "board_answer", "board_ask_wait"}
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
                    "fields",
                    "answer",
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
            "answered": {**array(obj(ANSWERED_PROPERTIES, ANSWERED_PROPERTIES)), "maxItems": 10},
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
        "Requires board:write, allowlisted repo and open card; up to five open asks. "
        "Returns sanitized untrusted summary and confirm, choice, text or form ask; "
        "moves to Needs you and posts context. Never ask for secrets. Non-idempotent.",
        {
            "card": CARD_REF,
            "kind": KIND,
            "prompt": string(300),
            "detail": string(2000),
            "options": {**array(OPTION), "minItems": 2, "maxItems": 8},
            "multiple": B,
            "allow_other": B,
            "recommended": LOCAL_ID,
            "yes_label": string(40),
            "no_label": string(40),
            "destructive": B,
            "fields": {**array(FIELD), "minItems": 1, "maxItems": 8},
        },
        {**SUMMARY, "ask": ASK},
        required=("card", "kind", "prompt"),
        write=True,
    )
    declare(
        "board_ask_wait",
        "Wait for a Slate answer",
        "Slate ask waiting. Use when work can continue after an answer. Requires board:read "
        "and ask_id; timeout_s 0-280, default 120. Returns sanitized untrusted ask, waited_s "
        "and timed_out; polls local state every two seconds without Gitea calls. Unknown IDs "
        "are not_found; withdrawal also ends waiting.",
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
        "Slate structured answers. Use when the owner's own surface answers an agent. "
        "Requires board:answer, allowlisted repo and another client's open ask. Returns sanitized "
        "untrusted summary and answered ask; comments and restores the prior column after the "
        "last open ask if still open in Needs you. Grants no permission. Non-idempotent.",
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
