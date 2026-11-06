"""Read-only Recipe API with compact, explicitly selected fields."""

from ..errors import CapabilityError
from ..schema import obj, string
from .common import array, output, pick, sanitize_text

TEXT = {"type": ["string", "null"]}
MINUTES = {"type": ["integer", "null"], "minimum": 0}
ROW = obj(
    {
        "id": {"type": "integer", "minimum": 1},
        "name": string(200),
        "description": {**TEXT, "maxLength": 300},
        "cuisine": TEXT,
        "total_minutes": MINUTES,
        "servings": {"type": "integer", "minimum": 1},
        "difficulty": string(50),
        "favorite": {"type": "boolean"},
        "tags": array(string(100)),
    },
    (
        "id",
        "name",
        "description",
        "cuisine",
        "total_minutes",
        "servings",
        "difficulty",
        "favorite",
        "tags",
    ),
)
INGREDIENT = obj(
    {
        "name": string(200),
        "quantity": {"type": "number"},
        "unit": string(50),
        "notes": string(1000),
        "category": string(100),
        "optional": {"type": "boolean"},
    },
    ("name",),
)
STEP = obj(
    {
        "number": {"type": "integer", "minimum": 1},
        "instruction": string(4000),
        "duration_minutes": MINUTES,
    },
    ("number", "instruction", "duration_minutes"),
)


def invalid():
    raise CapabilityError("backend_invalid", "Invalid recipes response")


def minutes(value):
    if value is not None and (type(value) is not int or value < 0):
        invalid()
    return value


def recipe_row(value):
    if not isinstance(value, dict):
        invalid()
    if (
        type(value.get("id")) is not int
        or value["id"] < 1
        or not isinstance(value.get("name"), str)
        or not value["name"].strip()
        or type(value.get("servings")) is not int
        or value["servings"] < 1
        or not isinstance(value.get("difficulty"), str)
        or type(value.get("isFavorite")) is not bool
    ):
        invalid()
    for key in ("description", "cuisine"):
        if value.get(key) is not None and not isinstance(value[key], str):
            invalid()
    tags = value.get("tags")
    if not isinstance(tags, list) or any(
        not isinstance(tag, str) or not tag.strip() for tag in tags
    ):
        invalid()
    prep, cook = minutes(value.get("prepTimeMinutes")), minutes(value.get("cookTimeMinutes"))
    return {
        "id": value["id"],
        "name": sanitize_text(value["name"], 200),
        "description": sanitize_text(value["description"], 300)
        if value.get("description") is not None
        else None,
        "cuisine": sanitize_text(value["cuisine"], 50)
        if value.get("cuisine") is not None
        else None,
        "total_minutes": prep + cook if prep is not None and cook is not None else None,
        "servings": value["servings"],
        "difficulty": sanitize_text(value["difficulty"], 50),
        "favorite": value["isFavorite"],
        "tags": [sanitize_text(tag, 100) for tag in tags[:20]],
    }


def register(registry):
    @registry.declare(
        "recipes_search",
        "Search recipes",
        "Use when finding recipes by text, ingredient, tag, cuisine or total cooking time. "
        "Requires an enabled Recipe backend and notes read access. Returns up to 20 compact "
        "recipes; total-time filtering applies to the bounded API result page.",
        obj(
            {
                "query": string(200),
                "ingredient": string(200),
                "tag": string(32),
                "cuisine": string(50),
                "max_total_minutes": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            }
        ),
        scope="notes:read",
        service="recipes",
        output_schema=output(recipes=array(ROW)),
    )
    async def search(core, client, args):
        params = {
            k: args[k].strip() for k in ("query", "ingredient", "tag", "cuisine") if k in args
        }
        if any(not value for value in params.values()):
            raise CapabilityError("invalid_arguments", "Recipe filters must contain text")
        limit = args.get("limit", 10)
        params["limit"] = str(limit)
        values = await core.backends.get("recipes", "/api/recipes", params=params)
        if not isinstance(values, list):
            invalid()
        recipes = [recipe_row(row) for row in values[:limit]]
        if "max_total_minutes" in args:
            recipes = [
                row
                for row in recipes
                if row["total_minutes"] is not None
                and row["total_minutes"] <= args["max_total_minutes"]
            ]
        return {
            "status": "ok",
            "recipes": recipes,
            "limit": limit,
            "truncated": len(values) >= limit,
        }

    @registry.declare(
        "recipes_get",
        "Read a recipe",
        "Use when reading a chosen recipe. Requires its recipe ID, an enabled Recipe "
        "backend and notes read access. Returns servings, times, ingredients and ordered steps.",
        obj({"id": {"type": "integer", "minimum": 1}}, ("id",)),
        scope="notes:read",
        service="recipes",
        output_schema=output(
            **ROW["properties"],
            prep_minutes=MINUTES,
            cook_minutes=MINUTES,
            ingredients=array(INGREDIENT),
            steps=array(STEP),
        ),
    )
    async def get(core, client, args):
        data = await core.backends.get("recipes", f"/api/recipes/{args['id']}")
        result = recipe_row(data)
        if result["id"] != args["id"]:
            invalid()
        ingredients, steps = data.get("ingredients"), data.get("instructions")
        if not isinstance(ingredients, list) or not isinstance(steps, list):
            invalid()
        compact_ingredients, compact_steps = [], []
        for row in ingredients[:100]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("name"), str)
                or not row["name"].strip()
            ):
                invalid()
            item = pick(row, ("name", "quantity", "unit", "notes", "category"))
            item["name"] = sanitize_text(row["name"], 200)
            if type(row.get("isOptional")) is bool:
                item["optional"] = row["isOptional"]
            compact_ingredients.append(item)
        for row in steps[:100]:
            if (
                not isinstance(row, dict)
                or type(row.get("stepNumber")) is not int
                or row["stepNumber"] < 1
                or not isinstance(row.get("instruction"), str)
                or not row["instruction"].strip()
            ):
                invalid()
            compact_steps.append(
                {
                    "number": row["stepNumber"],
                    "instruction": sanitize_text(row["instruction"], 4000),
                    "duration_minutes": minutes(row.get("durationMinutes")),
                }
            )
        return {
            "status": "ok",
            **result,
            "prep_minutes": minutes(data.get("prepTimeMinutes")),
            "cook_minutes": minutes(data.get("cookTimeMinutes")),
            "ingredients": compact_ingredients,
            "steps": sorted(compact_steps, key=lambda x: x["number"]),
            "truncated": len(ingredients) > 100 or len(steps) > 100,
        }
