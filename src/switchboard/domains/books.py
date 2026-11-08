"""LazyLibrarian book discovery, bounded status and serialized format requests."""

import math
import re

from ..errors import CapabilityError
from ..memory_subjects import MemorySubject
from ..schema import obj, string
from .common import sanitize_text

BOOK_ID = {**string(64), "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}(?![\s\S])"}
STATUSES = ("Skipped", "Wanted", "Snatched", "Open", "Have", "Ignored")
ACTIVE = ("Wanted", "Snatched", "Open", "Have")
FORMATS = {"ebook": ("eBook", "ebook_status"), "audiobook": ("AudioBook", "audiobook_status")}
STATUS = {"type": ["string", "null"], "enum": [*STATUSES, None]}
NULL_TEXT = {"type": ["string", "null"], "maxLength": 200}
BOOK_FIELDS = {
    "book_id": BOOK_ID,
    "title": string(200),
    "author": string(200),
    "ebook_status": STATUS,
    "audiobook_status": STATUS,
}


def invalid(detail):
    raise CapabilityError("backend_invalid", f"lazylibrarian: invalid {detail}")


def text(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or (not optional and not value.strip()):
        invalid("book text")
    return sanitize_text(value, 200)


def identifier(value):
    if not isinstance(value, str) or re.search(BOOK_ID["pattern"], value) is None:
        invalid("book ID")
    return value


def book_status(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in STATUSES:
        invalid("book status")
    return value


def book_output(statuses=("ok",), **fields):
    fields = {"status": {"type": "string", "enum": list(statuses)}, **fields}
    return obj(fields, tuple(fields))


def array(items, maximum=None):
    return {"type": "array", "items": items, **({"maxItems": maximum} if maximum else {})}


async def command(core, cmd, **params):
    return await core.backends.get("lazylibrarian", "/api", params={"cmd": cmd, **params})


async def library(core):
    data = await command(core, "getAllBooks")
    if not isinstance(data, list):
        invalid("library response")
    books = {}
    for row in data:
        if not isinstance(row, dict) or not {"Status", "AudioStatus"} <= row.keys():
            invalid("library book")
        book_id = identifier(row.get("BookID"))
        if book_id in books:
            invalid("duplicate book ID")
        added = row.get("BookAdded", "")
        if not isinstance(added, str):
            invalid("book added date")
        books[book_id] = {
            "book_id": book_id,
            "title": text(row.get("BookName")),
            "author": text(row.get("AuthorName")),
            "ebook_status": book_status(row["Status"]),
            "audiobook_status": book_status(row["AudioStatus"]),
            "added": added,
        }
    return books


def public_book(book):
    return {key: book[key] for key in BOOK_FIELDS}


def search_rows(data):
    if not isinstance(data, list):
        invalid("search response")
    results = []
    for row in data:
        # Metadata providers return ~100 loosely-typed rows; skip a malformed row rather
        # than failing the whole search. The response itself must still be a list.
        try:
            if not isinstance(row, dict):
                invalid("search book")
            score = row.get("highest_fuzz")
            if type(score) not in (int, float) or not math.isfinite(score):
                invalid("search score")
            year = row.get("bookpub")
            if year is not None and type(year) not in (str, int):
                invalid("publication year")
            book = {
                "book_id": identifier(row.get("bookid")),
                "title": text(row.get("bookname")),
                "subtitle": text(row.get("booksub", ""), optional=True) or "",
                "author": text(row.get("authorname")),
                "year": (sanitize_text(str(year), 200) or None) if year is not None else None,
                "isbn": text(row.get("bookisbn"), optional=True),
                "source": text(row.get("source")),
            }
        except CapabilityError:
            continue
        results.append((score, book))
    return [book for _, book in sorted(results, key=lambda item: item[0], reverse=True)]


def download_rows(data):
    if (
        not isinstance(data, dict)
        or data.get("Success") is not True
        or not isinstance(data.get("Data"), list)
        or ("Error" in data and not isinstance(data["Error"], dict))
    ):
        invalid("download response")
    downloads = {}
    types = {value[0]: key for key, value in FORMATS.items()}
    for row in data["Data"]:
        if not isinstance(row, dict):
            invalid("download")
        book_id = identifier(row.get("BookID"))
        kind = row.get("AuxInfo")
        progress = row.get("progress")
        if (
            not isinstance(kind, str)
            or kind not in types
            or (progress is not None and (type(progress) is not int or not -1 <= progress <= 100))
            or type(row.get("finished")) is not bool
        ):
            invalid("download")
        downloads.setdefault(book_id, []).append(
            {
                "format": types[kind],
                "progress": None if progress == -1 else progress,
                "finished": row["finished"],
            }
        )
    return downloads


def register(registry):
    @registry.declare(
        "books_search",
        "Search books",
        "Books: search ebooks and audiobooks by title or author. Use when finding a book before "
        "requesting it. Requires a title or author query. Returns ranked book IDs and "
        "ebook/audiobook state; confirm the exact title, author and format before "
        "books_request.",
        obj(
            {"query": string(200), "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
            ("query",),
        ),
        scope="media:read",
        service="lazylibrarian",
        timeout_s=60,
        output_schema=book_output(
            results=array(
                obj(
                    {
                        **BOOK_FIELDS,
                        "subtitle": {"type": "string", "maxLength": 200},
                        "year": NULL_TEXT,
                        "isbn": NULL_TEXT,
                        "source": string(200),
                    },
                    (*BOOK_FIELDS, "subtitle", "year", "isbn", "source"),
                ),
                10,
            )
        ),
        memory=MemorySubject(
            "books",
            arg_keys=("book_id",),
            result_paths=(
                "title",
                "author",
                "results[].title",
                "results[].author",
                "books[].title",
                "books[].author",
            ),
        ),
    )
    async def search(core, client, args):
        query = args["query"].strip()
        if not query:
            raise CapabilityError("invalid_arguments", "query must contain non-whitespace text")
        results = search_rows(await command(core, "findBook", name=query))
        books = await library(core)
        for result in results:
            book = books.get(result["book_id"], {})
            result.update(
                ebook_status=book.get("ebook_status"), audiobook_status=book.get("audiobook_status")
            )
        limit = args.get("limit", 5)
        return {
            "status": "ok",
            "results": results[:limit],
            "limit": limit,
            "truncated": len(results) > limit,
        }

    @registry.declare(
        "books_request",
        "Request book",
        "Books: request ebooks and audiobooks. Use when requesting an ebook, audiobook or both. "
        "Requires a policy-allowed book ID and human-confirmed title, author and format. "
        "Returns queued/already formats; Wanted is never re-queued, but repeating restarts its "
        "search. Poll books_status instead.",
        obj(
            {"book_id": BOOK_ID, "format": {"type": "string", "enum": [*FORMATS, "both"]}},
            ("book_id",),
        ),
        scope="media:request",
        tier="write",
        idempotent=True,
        service="lazylibrarian",
        timeout_s=60,
        output_schema=book_output(
            ("queued", "already_requested"),
            **BOOK_FIELDS,
            queued=array({"type": "string", "enum": list(FORMATS)}, 2),
            already=array({"type": "string", "enum": list(FORMATS)}, 2),
        ),
        memory=MemorySubject(
            "books",
            arg_keys=("book_id",),
            result_paths=(
                "title",
                "author",
                "results[].title",
                "results[].author",
                "books[].title",
                "books[].author",
            ),
        ),
    )
    async def request(core, client, args):
        book_id = args["book_id"]
        async with core.media_request_lock:
            core.policy.require_allowed("media", "books", book_id)

            def require_write():
                # Reload policy after slow reads and before each mutation, as media requests do.
                core.policy.require(client, registry.capabilities["books_request"])
                core.policy.require_allowed("media", "books", book_id)

            books = await library(core)
            added = book_id not in books
            if added:
                require_write()
                await command(core, "addBook", id=book_id, wait="1")
                books = await library(core)
            if book_id not in books:
                raise CapabilityError("not_found", "lazylibrarian: book not found after add")
            book = books[book_id]
            requested = args.get("format", "ebook")
            formats = list(FORMATS) if requested == "both" else [requested]
            queued, already = [], []
            for kind in formats:
                backend_type, field = FORMATS[kind]
                # Snatched/Open/Have need nothing. Wanted (queued earlier, or set by LL's
                # new-book default) is not re-queued, but its search is started again: a
                # repeated request is how a human says "try again", and LL's own backlog
                # search may be hours away.
                if book[field] in ("Snatched", "Open", "Have"):
                    already.append(kind)
                    continue
                if book[field] == "Wanted" and not added:
                    already.append(kind)
                if book[field] != "Wanted":
                    require_write()
                    receipt = await command(core, "queueBook", id=book_id, type=backend_type)
                    if receipt != "OK":
                        invalid("queue receipt")
                    book[field] = "Wanted"
                require_write()
                await command(core, "searchBook", id=book_id, type=backend_type)
                if kind not in already:
                    queued.append(kind)
            return {
                "status": "queued" if queued else "already_requested",
                **public_book(book),
                "queued": queued,
                "already": already,
            }

    @registry.declare(
        "books_status",
        "Book request status",
        "Book requests, availability and download progress. Use when checking book requests or "
        "availability. Requires an optional book ID or bounded limit. Returns ebook/audiobook "
        "state and download progress; Open/Have means available in Calibre/AudioShelf. Without "
        "an ID, lists active books newest first.",
        obj({"book_id": BOOK_ID, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
        scope="media:read",
        service="lazylibrarian",
        timeout_s=60,
        output_schema=book_output(
            books=array(
                obj(
                    {
                        **BOOK_FIELDS,
                        "downloads": array(
                            obj(
                                {
                                    "format": {"type": "string", "enum": list(FORMATS)},
                                    "progress": {
                                        "type": ["integer", "null"],
                                        "minimum": 0,
                                        "maximum": 100,
                                    },
                                    "finished": {"type": "boolean"},
                                },
                                ("format", "progress", "finished"),
                            )
                        ),
                    },
                    (*BOOK_FIELDS, "downloads"),
                ),
                50,
            )
        ),
        memory=MemorySubject(
            "books",
            arg_keys=("book_id",),
            result_paths=(
                "title",
                "author",
                "results[].title",
                "results[].author",
                "books[].title",
                "books[].author",
            ),
        ),
    )
    async def status(core, client, args):
        books = await library(core)
        limit = args.get("limit", 20)
        if "book_id" in args:
            if args["book_id"] not in books:
                raise CapabilityError("not_found", "lazylibrarian: book not found")
            selected = [books[args["book_id"]]]
        else:
            selected = sorted(
                (
                    book
                    for book in books.values()
                    if book["ebook_status"] in ACTIVE or book["audiobook_status"] in ACTIVE
                ),
                key=lambda book: book["added"],
                reverse=True,
            )
        truncated = len(selected) > limit
        selected = selected[:limit]
        downloads = download_rows(await command(core, "getDownloadProgress"))
        return {
            "status": "ok",
            "limit": limit,
            "truncated": truncated,
            "books": [
                {**public_book(book), "downloads": downloads.get(book["book_id"], [])}
                for book in selected
            ],
        }
