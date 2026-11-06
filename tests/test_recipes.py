"""Recipe tools use an authenticated, ephemeral fake HTTP backend."""

import asyncio
import copy
import json
from pathlib import Path

import pytest
import yaml
from aiohttp import web

from switchboard.config import Config

from .conftest import change_policy, running_app


@pytest.fixture
async def recipes(core):
    token = core.config.config_dir / "recipe.token"
    token.write_text("fake-recipe-token")
    token.chmod(0o600)
    recipe = {
        "id": 1,
        "name": "Tomato pasta",
        "description": "x" * 400,
        "cuisine": "Italian",
        "prepTimeMinutes": 10,
        "cookTimeMinutes": 20,
        "servings": 2,
        "difficulty": "easy",
        "isFavorite": True,
        "tags": ["dinner"],
        "ingredients": [
            {
                "name": "Tomato",
                "quantity": 2,
                "unit": "cups",
                "notes": "chopped",
                "isOptional": False,
                "category": "Produce",
                "ingredientId": 20,
                "secret": "PRIVATE",
            }
        ],
        "instructions": [
            {
                "stepNumber": 2,
                "instruction": "Cook the pasta.",
                "durationMinutes": 10,
                "imageUrl": "PRIVATE",
            },
            {"stepNumber": 1, "instruction": "Chop tomatoes.", "durationMinutes": None},
        ],
        "sourceUrl": "PRIVATE",
        "imageUrl": "PRIVATE",
        "secret": "PRIVATE",
    }
    state = {
        "/api/recipes": [recipe],
        "/api/recipes/1": recipe,
        "/api/health": {"status": "healthy"},
    }
    calls = []

    async def handle(request):
        assert request.method == "GET"
        assert request.headers.get("Authorization") == "Bearer fake-recipe-token"
        calls.append((request.path, dict(request.query)))
        data = state.get(request.path, 404)
        if isinstance(data, int):
            return web.Response(status=data, text="PRIVATE")
        return web.json_response(data)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with running_app(app) as url:
        core.config.backends["recipes"].update(enabled=True, url=url, token_file=str(token))
        yield core, state, calls


async def test_recipe_search_filters_compact_rows_and_get(recipes):
    core, _, calls = recipes
    result = await core.call(
        "dot",
        "recipes_search",
        {
            "query": " pasta ",
            "ingredient": "tomato",
            "tag": "dinner",
            "cuisine": "Italian",
            "max_total_minutes": 30,
        },
    )
    assert result["source"] == "recipes" and result["limit"] == 10
    assert calls == [
        (
            "/api/recipes",
            {
                "query": "pasta",
                "ingredient": "tomato",
                "tag": "dinner",
                "cuisine": "Italian",
                "limit": "10",
            },
        )
    ]
    assert result["recipes"] == [
        {
            "id": 1,
            "name": "Tomato pasta",
            "description": "x" * 300,
            "cuisine": "Italian",
            "total_minutes": 30,
            "servings": 2,
            "difficulty": "easy",
            "favorite": True,
            "tags": ["dinner"],
        }
    ]
    assert "PRIVATE" not in json.dumps(result)
    result = await core.call("dot", "recipes_get", {"id": 1})
    assert result["prep_minutes"] == 10 and result["cook_minutes"] == 20
    assert result["ingredients"] == [
        {
            "name": "Tomato",
            "quantity": 2,
            "unit": "cups",
            "notes": "chopped",
            "optional": False,
            "category": "Produce",
        }
    ]
    assert result["steps"] == [
        {"number": 1, "instruction": "Chop tomatoes.", "duration_minutes": None},
        {"number": 2, "instruction": "Cook the pasta.", "duration_minutes": 10},
    ]
    assert "PRIVATE" not in json.dumps(result)


async def test_recipe_limit_time_filter_unknown_times_and_truncation(recipes):
    core, state, _ = recipes
    first = state["/api/recipes"][0]
    state["/api/recipes"] = [
        first,
        {**first, "id": 2, "prepTimeMinutes": None},
        {**first, "id": 3, "cookTimeMinutes": 40},
    ]
    result = await core.call("dot", "recipes_search", {"max_total_minutes": 30, "limit": 3})
    assert [row["id"] for row in result["recipes"]] == [1]
    assert result["truncated"]
    result = await core.call("dot", "recipes_search", {"limit": 1})
    assert len(result["recipes"]) == 1 and result["truncated"]


async def test_recipe_health_503_and_disabled_defaults(recipes, config):
    core, state, _ = recipes
    assert (await core.backends.probe("recipes"))["status"] == "ok"
    state["/api/health"] = 503
    check = await core.backends.probe("recipes")
    assert check["status"] == "down" and "503" in check["reason"]
    assert "PRIVATE" not in json.dumps(check)
    core.config.backends["recipes"]["enabled"] = False
    assert (await core.backends.probe("recipes"))["status"] == "disabled"
    assert (await core.call("dot", "recipes_search", {}))["outcome"] == "unavailable"
    loaded = Config.load()
    assert loaded.backends["recipes"] == {
        "url": "http://127.0.0.1:3000",
        "enabled": False,
        "token_file": None,
    }
    (config.config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"backends": {"recipes": {"enabled": True, "token_file": "/private/recipe.token"}}}
        )
    )
    assert Config.load().backends["recipes"]["token_file"] == "/private/recipe.token"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("recipes_get", {}),
        ("recipes_get", {"id": "../secret"}),
        ("recipes_get", {"id": True}),
        ("recipes_get", {"id": 0}),
        ("recipes_search", {"limit": 21}),
        ("recipes_search", {"limit": 0}),
        ("recipes_search", {"query": " "}),
        ("recipes_search", {"max_total_minutes": -1}),
        ("recipes_search", {"url": "PRIVATE"}),
    ],
)
async def test_recipes_invalid_before_io(recipes, tool, args):
    core, _, calls = recipes
    assert (await core.call("dot", tool, args))["error"]["code"] == "invalid_arguments"
    assert not calls


@pytest.mark.parametrize("value", [None, {}, [None], [{"id": 1}], "PRIVATE"])
async def test_recipe_bad_list(recipes, value):
    core, state, _ = recipes
    state["/api/recipes"] = value
    result = await core.call("dot", "recipes_search", {})
    assert result["error"]["code"] == "backend_invalid"
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ingredients", None),
        ("ingredients", [None]),
        ("instructions", [None]),
        ("prepTimeMinutes", "PRIVATE"),
        ("id", 2),
    ],
)
async def test_recipe_bad_detail(recipes, field, value):
    core, state, _ = recipes
    data = copy.deepcopy(state["/api/recipes/1"])
    data[field] = value
    state["/api/recipes/1"] = data
    result = await core.call("dot", "recipes_get", {"id": 1})
    assert result["error"]["code"] == "backend_invalid"
    assert "PRIVATE" not in json.dumps(result)


async def test_recipe_denial_hidden_policy_and_example(recipes):
    core, _, calls = recipes
    for name in ("recipes_search", "recipes_get"):
        assert name in {tool["name"] for tool in core.tools("dot")}
        assert name not in {tool["name"] for tool in core.tools("agy")}
        assert (await core.call("agy", name, {}))["error"]["code"] == "scope_denied"
        assert core.audit.tail()[-1]["decision"] == "denied"
    change_policy(core.config, lambda p: p["clients"]["dot"].update(scopes=[]))
    assert (await core.call("dot", "recipes_search", {}))["error"]["code"] == "scope_denied"
    core.policy.path.unlink()
    assert (await core.call("owner", "recipes_get", {"id": 1}))["error"]["code"] == "policy_invalid"
    assert not calls
    policy = yaml.safe_load(await asyncio.to_thread(Path("config/policy.example.yaml").read_text))
    assert {name for name, row in policy["clients"].items() if "notes:read" in row["scopes"]} == {
        "owner",
        "dot",
        "orb",
        "openclaw",
        "claude",
        "codex",
        "agy",
    }
