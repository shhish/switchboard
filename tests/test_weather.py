"""NWS contracts use an explicit Python-only fake origin, never the public API."""

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml
from aiohttp import web

from switchboard import __version__
from switchboard.config import Config, validate_url
from switchboard.errors import CapabilityError
from switchboard.nws import BASE, MAX_BODY, NWS, validate_settings

from .conftest import change_policy, running_app


@pytest.fixture
async def nws(core, config):
    config.weather = validate_settings(
        {"enabled": True, "name": "Test location", "zip": "00000", "lat": 40.0, "lon": -100.0}
    )
    change_policy(config, lambda p: p["clients"]["dot"]["scopes"].append("weather:read"))
    calls, state = [], {}
    today = datetime.now(ZoneInfo("America/New_York")).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    periods = [
        {
            "name": "Today" if i == 0 else "Tonight" if i == 1 else f"Period {i}",
            "startTime": (today + timedelta(hours=12 * i)).isoformat(),
            "isDaytime": i % 2 == 0,
            "temperature": 70 if i % 2 == 0 else 50,
            "temperatureUnit": "F",
            "probabilityOfPrecipitation": {"value": 25, "unitCode": "wmoUnit:percent"},
            "shortForecast": "Sunny",
            "windSpeed": "5 to 10 mph",
            "windDirection": "NW",
            "detailedForecast": "PRIVATE",
        }
        for i in range(14)
    ]
    state["/forecast"] = {"properties": {"periods": periods}}
    state["/hourly"] = {
        "properties": {
            "periods": [
                {**periods[0], "startTime": (today + timedelta(hours=i)).isoformat()}
                for i in range(24)
            ]
        }
    }
    state["/stations"] = {
        "features": [
            {"properties": {"stationIdentifier": "KFIRST", "name": "Nearest"}},
            {"properties": {"stationIdentifier": "KSECOND", "name": "Farther"}},
        ]
    }
    state["/stations/KFIRST/observations/latest"] = {
        "properties": {
            "temperature": {"value": 20, "unitCode": "wmoUnit:degC"},
            "heatIndex": {"value": None, "unitCode": "wmoUnit:degC"},
            "windChill": {"value": 18, "unitCode": "wmoUnit:degC"},
            "relativeHumidity": {"value": 42, "unitCode": "wmoUnit:percent"},
            "windSpeed": {"value": 16.09344, "unitCode": "wmoUnit:km_h-1"},
            "windDirection": {"value": 315, "unitCode": "wmoUnit:degree_(angle)"},
            "timestamp": today.isoformat(),
            "textDescription": "Clear",
            "private": "PRIVATE",
        }
    }
    state["/alerts/active"] = {
        "features": [
            {
                "properties": {
                    "event": "Wind Advisory",
                    "severity": "Moderate",
                    "urgency": "Expected",
                    "headline": "Untrusted headline " + "x" * 500,
                    "effective": today.isoformat(),
                    "expires": (today + timedelta(hours=8)).isoformat(),
                    "description": "PRIVATE LONG DESCRIPTION",
                    "instruction": "PRIVATE INSTRUCTION",
                }
            }
        ]
    }

    async def handle(request):
        assert request.method == "GET"
        assert request.headers["User-Agent"] == f"Switchboard/{__version__} (personal home server)"
        assert request.headers["Accept"] == "application/geo+json"
        assert not any(k in request.headers for k in ("Authorization", "Cookie", "X-Api-Key"))
        calls.append((request.path, dict(request.query)))
        value = state.get(request.path, 404)
        if isinstance(value, web.StreamResponse):
            return value
        if isinstance(value, int):
            return web.json_response({"private": "PRIVATE"}, status=value)
        if isinstance(value, str):
            raise web.HTTPFound(value)
        if value == b"chunked":
            response = web.StreamResponse()
            await response.prepare(request)
            for _ in range(33):
                await response.write(b"x" * 65536)
            await response.write_eof()
            return response
        return web.json_response(value)

    app = web.Application()
    app.router.add_get("/{path:.*}", handle)
    async with running_app(app) as url:
        state["/points/40.0,-100.0"] = {
            "properties": {
                "forecast": url + "/forecast",
                "forecastHourly": url + "/hourly",
                "observationStations": url + "/stations",
                "timeZone": "America/New_York",
            }
        }
        await core.backends.weather.close()
        client = NWS(config, _test_base_url=url)
        await client.start()
        core.backends.weather = client
        yield client, state, calls


async def test_now_and_transport(core, nws):
    client, _, calls = nws
    result = await core.call("dot", "weather_now", {})
    assert result["status"] == "ok"
    assert result["temp_c"] == 20 and result["temp_f"] == 68
    assert result["feels_like_f"] == 64.4
    assert result["humidity_percent"] == 42
    assert result["wind_mph"] == 10 and result["wind_direction"] == "NW"
    assert result["station"] == {"id": "KFIRST", "name": "Nearest"}
    assert result["today_high_f"] == 70 and result["today_low_f"] == 50
    assert result["location"] == {"name": "Test location", "zip": "00000"}
    assert result["source"] == "nws" and result["as_of"] and result["observed_at"]
    assert result["stale"] is False and result["age_s"] == 0
    assert "PRIVATE" not in json.dumps(result)
    assert len(calls) == 4
    assert client.session.trust_env is False and client.session.timeout.total == 8
    assert (await core.call("dot", "weather_now", {})) == result
    assert len(calls) == 4
    expected_ttls = {
        "/points/": 86400,
        "/forecast": 900,
        "/stations": 86400,
        "/observations/latest": 600,
    }
    for url, cached in client.cache.items():
        ttl = next(
            ttl
            for part, ttl in expected_ttls.items()
            if part in url and not (part == "/stations" and "/observations" in url)
        )
        assert ttl - 5 < cached.expires - time.monotonic() <= ttl


async def test_forecast_hourly_alerts_defaults_limits_and_privacy(core, nws):
    client, _, calls = nws
    result = await core.call("dot", "weather_forecast", {})
    assert len(result["periods"]) == 6
    assert len((await core.call("dot", "weather_forecast", {"days": 7}))["periods"]) == 14
    assert len((await core.call("dot", "weather_hourly", {}))["hours"]) == 12
    result = await core.call("dot", "weather_hourly", {"hours": 24})
    assert len(result["hours"]) == 24
    assert set(result["hours"][0]) == {"hour", "temp_f", "precip_percent", "short_forecast"}
    result = await core.call("dot", "weather_alerts", {})
    alert = result["alerts"][0]
    assert len(alert["headline"]) == 300
    assert set(alert) == {"event", "severity", "urgency", "headline", "effective", "expires"}
    assert "PRIVATE" not in json.dumps(result)
    assert calls[-1][1] == {"point": "40.0,-100.0"}
    assert len(calls) == 4  # Points shared; forecast/hours/alerts have independent caches.
    assert (
        295
        < client.cache[client.base + "/alerts/active?point=40.0,-100.0"].expires - time.monotonic()
        <= 300
    )


async def test_stale_and_recovery_preserve_successful_value(core, nws):
    client, state, calls = nws
    first = await core.call("dot", "weather_forecast", {})
    cached = client.cache[client.base + "/forecast"]
    cached.expires = 0
    cached.fetched -= 1000
    state["/forecast"] = 503
    result = await core.call("dot", "weather_forecast", {})
    assert result["stale"] is True and result["age_s"] >= 1000
    assert result["periods"] == first["periods"]
    assert "PRIVATE" not in json.dumps(result)
    assert client.cache[client.base + "/forecast"] is cached
    state["/forecast"] = {"properties": {"periods": []}}
    assert (await core.call("dot", "weather_forecast", {}))["stale"] is True
    client.cache.clear()
    result = await core.call("dot", "weather_forecast", {})
    assert result["stale"] is True  # Last composed result survives a mapping failure too.
    state["/forecast"] = {
        "properties": {
            "periods": [
                {
                    "startTime": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                    "isDaytime": True,
                    "temperature": 10,
                    "temperatureUnit": "C",
                }
            ]
        }
    }
    result = await core.call("dot", "weather_forecast", {})
    assert result["stale"] is False and result["periods"][0]["temp_f"] == 50
    assert calls


@pytest.mark.parametrize("mode", ["redirect", "large", "chunked", "invalid", "error", "followup"])
async def test_unavailable_safe_without_cache(core, nws, mode):
    client, state, calls = nws
    path = "/points/40.0,-100.0"
    if mode == "redirect":
        state[path] = "https://example.org/private"
    elif mode == "large":
        state[path] = web.Response(body=b"x" * (MAX_BODY + 1))
    elif mode == "chunked":
        state[path] = b"chunked"
    elif mode == "invalid":
        state[path] = web.Response(body=b"not json PRIVATE")
    elif mode == "error":
        state[path] = 500
    else:
        state[path]["properties"]["forecast"] = "https://example.org/forecast"
    result = await core.call("dot", "weather_forecast", {})
    assert result["error"]["code"] == "weather_unavailable"
    assert "PRIVATE" not in json.dumps(result) and "example.org" not in json.dumps(result)
    assert len(calls) == 1 and client.cache == {}


async def test_same_host_redirects_two_only(core, nws):
    client, state, calls = nws
    state["/alerts/original"] = state["/alerts/active"]
    state["/alerts/active"] = "/alerts/redirect"
    state["/alerts/redirect"] = "/alerts/original"
    assert (await core.call("dot", "weather_alerts", {}))["stale"] is False
    assert len(calls) == 3
    client.cache.clear()
    client.results.clear()
    state["/alerts/original"] = "/alerts/third"
    assert (await core.call("dot", "weather_alerts", {}))["error"]["code"] == "weather_unavailable"
    assert calls[-1][0] == "/alerts/original"  # Third redirect was never followed.


@pytest.mark.parametrize(
    "url",
    [
        "http://api.weather.gov/points/x",
        "https://api.weather.gov.evil.test/x",
        "https://user:password@api.weather.gov/x",
        "https://api.weather.gov:444/x",
        "https://api.weather.gov/x#fragment",
        "http://127.0.0.1:18800/x",
    ],
)
def test_production_allowlist(config, url):
    with pytest.raises(CapabilityError):
        NWS(config).check_url(url)
    assert NWS(config).check_url(BASE + "/points/x")
    with pytest.raises(ValueError):
        validate_url(BASE)  # Local backend rules have not changed.


@pytest.mark.parametrize(
    "tool,args",
    [
        ("weather_now", {"url": BASE}),
        ("weather_forecast", {"days": 8}),
        ("weather_forecast", {"days": True}),
        ("weather_hourly", {"hours": 0}),
        ("weather_hourly", {"hours": 25}),
        ("weather_alerts", {"point": "elsewhere"}),
    ],
)
async def test_schemas_and_hidden_denials(core, nws, tool, args):
    assert (await core.call("dot", tool, args))["error"]["code"] == "invalid_arguments"
    assert (await core.call("agy", tool, {}))["error"]["code"] == "scope_denied"
    assert core.audit.tail()[-1]["decision"] == "denied"
    assert not any(t["name"].startswith("weather_") for t in core.tools("agy"))
    assert nws[2] == []


@pytest.mark.parametrize(
    "weather",
    [
        {"_test_base_url": "http://127.0.0.1:12345"},
        {"lat": True},
        {"lat": float("nan")},
        {"lon": 181},
        {"zip": 00000},
        {"enabled": "true"},
        {"allowed_hosts": ["*.weather.gov"]},
        {"allowed_hosts": "api.weather.gov"},
        {"name": " "},
    ],
)
def test_invalid_config(config, weather):
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump({"weather": weather}))
    with pytest.raises(ValueError):
        Config.load()


async def test_disabled_weather_does_not_serve_cache(core, nws):
    assert (await core.call("dot", "weather_forecast", {}))["status"] == "ok"
    core.config.weather["enabled"] = False
    assert (await core.call("dot", "weather_forecast", {}))["error"][
        "code"
    ] == "weather_unavailable"


async def test_hourly_observation_and_alerts_independent_stale_caches(core, nws):
    client, state, _ = nws
    originals = {}
    for tool in ("now", "hourly", "alerts"):
        originals[tool] = await core.call("dot", "weather." + tool, {})
        assert originals[tool]["stale"] is False
    affected = ("/hourly", "/stations/KFIRST/observations/latest", "/alerts/active")
    for url, saved in client.cache.items():
        if any(part in url for part in affected):
            saved.expires = 0
            saved.fetched -= 700
    for path in affected:
        state[path] = 503
    for tool in ("now", "hourly", "alerts"):
        result = await core.call("dot", "weather." + tool, {})
        assert result["stale"] is True and result["age_s"] >= 700
        assert result["as_of"] <= originals[tool]["as_of"]
        assert "PRIVATE" not in json.dumps(result)


async def test_points_stale_and_mapping_change_falls_back(core, nws):
    client, state, calls = nws
    result = await core.call("dot", "weather_forecast", {})
    point = client.cache[client.base + "/points/40.0,-100.0"]
    point.expires = 0
    state["/points/40.0,-100.0"] = 503
    assert (await core.call("dot", "weather_forecast", {}))["stale"] is True
    state["/points/40.0,-100.0"] = {
        "properties": {
            "forecast": client.base + "/new-forecast",
            "forecastHourly": client.base + "/hourly",
            "observationStations": client.base + "/stations",
            "timeZone": "America/New_York",
        }
    }
    saved = await core.call("dot", "weather_forecast", {})
    assert saved["stale"] is True and saved["periods"] == result["periods"]
    assert calls[-1][0] == "/new-forecast"


async def test_nullable_observation_and_nonfinite_quantities(core, nws):
    _, state, _ = nws
    props = state["/stations/KFIRST/observations/latest"]["properties"]
    props["temperature"]["value"] = None
    props["windChill"]["value"] = None
    props["windSpeed"]["value"] = float("nan")
    props["relativeHumidity"]["value"] = 10**1000
    result = await core.call("dot", "weather_now", {})
    assert result["status"] == "ok"
    for key in ("temp_f", "temp_c", "feels_like_f", "wind_mph", "humidity_percent"):
        assert result[key] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.weather.gov",
        "http://127.0.0.1:18888/base",
        "http://localhost:18888",
        "http://user@127.0.0.1:18888",
        "http://127.0.0.1:18888?base=1",
    ],
)
def test_test_override_is_limited_to_literal_loopback_origin(config, url):
    with pytest.raises(ValueError):
        NWS(config, _test_base_url=url)


async def test_transport_timeout_uses_cached_value(core, nws, monkeypatch):
    client, _, _ = nws
    first = await core.call("dot", "weather_alerts", {})
    for saved in client.cache.values():
        saved.expires = 0

    def timeout(*args, **kwargs):
        raise TimeoutError("PRIVATE exception text")

    monkeypatch.setattr(client.session, "get", timeout)
    result = await core.call("dot", "weather_alerts", {})
    assert result["stale"] is True and result["alerts"] == first["alerts"]
    assert "PRIVATE" not in json.dumps(result)
    client.cache.clear()
    client.results.clear()
    result = await core.call("dot", "weather_alerts", {})
    assert result["error"]["code"] == "weather_unavailable"
