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
}
CARD = obj(SUMMARY, SUMMARY)


def register(registry):
    def declare(name, title, description, inputs, fields, *, required=(), write=False):
        async def handler(core, client, args):
            return await getattr(core.board, name.removeprefix("board_"))(client, args)

        registry.declare(
            name,
            title,
            description,
            obj(inputs, required),
            scope="board:write" if write else "board:read",
            tier="write" if write else "read",
            idempotent=not write,
            timeout_s=180,
            service="gitea",
            max_result_chars=500_000 if name == "board_list" else 150_000,
            output_schema=obj(
                {"status": S, **fields}, ("status", *(key for key in fields if key != "truncated"))
            ),
            audit_hash_fields=frozenset({"title", "body", "note"}),
            memory=None,
            memory_optout="Board cards and actor receipts are workflow state, not shared facts",
        )(handler)

    declare(
        "board_list",
        "Slate board",
        "Slate board: cards grouped in board order. Use when choosing or checking shared work. "
        "Requires board:read. Returns sanitized untrusted summaries, counts, claims and notes; "
        "bounded five-column paging, limit 1-100 (60 default). Done is hidden unless requested, "
        "then capped at 15 newest updates; closed issues count as Done. Partial views are marked.",
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
                            "cards": {**array(CARD), "maxItems": 100},
                        },
                        ("title", "count", "cards"),
                    )
                ),
                "maxItems": 5,
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
        "Returns sanitized untrusted summary, body up to 6000 characters and latest 30 comments "
        "up to 1500 characters each; absent membership is not_found.",
        {"card": CARD_REF},
        {
            **SUMMARY,
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
        "owner ask in note (up to 2000 characters). "
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
        "board_import_things",
        "Import Things board captures",
        "Things capture into Slate Ideas. Use when importing phone ideas now. "
        "Requires board:write, tasks:write, configured project and Git/Things allowlists. "
        "Returns up to 20 sanitized untrusted imported titles, completed/skipped counts and "
        "error codes. Saves mappings before completion; retries reuse cards. Unset or unavailable "
        "Things is a quiet no-op; bounded reads may truncate.",
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
