"""Dedicated credential-free NWS client; local backend URL rules remain unchanged."""

import asyncio
import copy
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp

from . import __version__
from .errors import CapabilityError

BASE = "https://api.weather.gov"
MAX_BODY = 2 * 1024 * 1024
DEFAULT_WEATHER = {
    "enabled": False,
    "name": None,
    "zip": None,
    "lat": None,
    "lon": None,
    "allowed_hosts": ["api.weather.gov"],
}


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - DEFAULT_WEATHER.keys():
        raise ValueError("Invalid weather fields")
    result = {**DEFAULT_WEATHER, **value}
    if type(result["enabled"]) is not bool:
        raise ValueError("Weather enabled must be boolean")
    for key in ("name", "zip", "lat", "lon"):
        field = result[key]
        if field is None and not result["enabled"]:
            continue
        if key == "name":
            valid = isinstance(field, str) and 1 <= len(field.strip()) <= 120
        elif key == "zip":
            valid = isinstance(field, str) and re.fullmatch(r"[0-9]{5}", field)
        else:
            bound = 90 if key == "lat" else 180
            valid = type(field) in (int, float) and math.isfinite(field) and abs(field) <= bound
        if not valid:
            raise ValueError(f"Weather requires a valid {key}; set name/zip/lat/lon when enabling")
    hosts = result["allowed_hosts"]
    if (
        not isinstance(hosts, list)
        or not hosts
        or len(hosts) > 10
        or any(
            not isinstance(host, str) or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host)
            for host in hosts
        )
    ):
        raise ValueError("Invalid weather allowed hosts")
    result["allowed_hosts"] = list(hosts)
    return result


def unavailable():
    return CapabilityError("weather_unavailable", "NWS weather unavailable; try again later")


def text(value):
    from .domains.common import sanitize_text

    return sanitize_text(value) if isinstance(value, str) else None


def number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None


def quantity(value, unit):
    if not isinstance(value, dict) or number(value.get("value")) is None:
        return None
    amount = value["value"]
    source = value.get("unitCode", "").split(":")[-1]
    if unit == "c":
        return amount if source == "degC" else (amount - 32) * 5 / 9 if source == "degF" else None
    if unit == "mph":
        factor = {"km_h-1": 1 / 1.609344, "m_s-1": 2.236936, "mi_h-1": 1, "kn": 1.150779}
        return amount * factor[source] if source in factor else None
    return amount


def fahrenheit(celsius):
    return round(celsius * 9 / 5 + 32, 1) if celsius is not None else None


def properties(data):
    if not isinstance(data, dict) or not isinstance(data.get("properties"), dict):
        raise unavailable()
    return data["properties"]


@dataclass
class Cached:
    value: object
    fetched: float
    expires: float
    stale: bool = False


class NWS:
    def __init__(self, config, *, _test_base_url=None):
        self.config = config
        self.base = BASE
        # Explicit Python-only injection; never accepted from config, tools or env.
        if _test_base_url is not None:
            parsed = urlsplit(_test_base_url)
            if (
                parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or not parsed.port
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("Test NWS base must be an ephemeral loopback origin")
            self.base = _test_base_url
        self.session = None
        self.cache = {}
        self.results = {}
        self.locks = {}

    def check_url(self, url):
        try:
            parsed = urlsplit(url)
            origin = urlsplit(self.base)
            valid = (
                parsed.scheme == origin.scheme
                and parsed.hostname == origin.hostname
                and parsed.port == origin.port
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
                and "\\" not in url
                and (self.base != BASE or parsed.hostname in self.config.weather["allowed_hosts"])
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise unavailable()
        return url

    async def start(self):
        self.session = aiohttp.ClientSession(
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=8),
            headers={
                "User-Agent": f"Switchboard/{__version__} (personal home server)",
                "Accept": "application/geo+json",
            },
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def request(self, url):
        self.check_url(url)
        try:
            # One timeout covers the full redirect chain, not eight seconds per hop.
            async with asyncio.timeout(8):
                for hop in range(3):
                    async with self.session.get(url, allow_redirects=False) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            if hop == 2 or not response.headers.get("Location"):
                                raise unavailable()
                            url = self.check_url(urljoin(url, response.headers["Location"]))
                            continue
                        if response.status != 200:
                            raise unavailable()
                        if response.content_length and response.content_length > MAX_BODY:
                            raise unavailable()
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > MAX_BODY:
                                raise unavailable()
                        return json.loads(body)
        except CapabilityError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError, RecursionError):
            raise unavailable() from None
        raise unavailable()

    async def cached(self, url, ttl, parse):
        self.check_url(url)
        async with self.locks.setdefault(url, asyncio.Lock()):
            saved = self.cache.get(url)
            if saved and time.monotonic() < saved.expires:
                return saved
            try:
                value = parse(await self.request(url))
            except (CapabilityError, KeyError, TypeError, ValueError, AttributeError):
                if saved:
                    return Cached(saved.value, saved.fetched, saved.expires, True)
                raise unavailable() from None
            saved = Cached(value, time.time(), time.monotonic() + ttl)
            self.cache[url] = saved
            return saved

    def points(self, data):
        props = properties(data)
        result = {
            key: self.check_url(props[key])
            for key in ("forecast", "forecastHourly", "observationStations")
        }
        ZoneInfo(props["timeZone"])
        result["timeZone"] = props["timeZone"]
        return result

    @staticmethod
    def periods(data):
        periods = properties(data).get("periods")
        if not isinstance(periods, list) or not periods:
            raise unavailable()
        result = []
        for row in periods:
            start = datetime.fromisoformat(row["startTime"])
            if start.tzinfo is None or type(row["isDaytime"]) is not bool:
                raise unavailable()
            temp = number(row["temperature"])
            if temp is None or row["temperatureUnit"] not in ("F", "C"):
                raise unavailable()
            result.append(
                {
                    "hour": start.isoformat(),
                    "name": text(row.get("name")),
                    "is_daytime": row["isDaytime"],
                    "temp_f": temp if row["temperatureUnit"] == "F" else fahrenheit(temp),
                    "precip_percent": quantity(row.get("probabilityOfPrecipitation"), "%"),
                    "short_forecast": text(row.get("shortForecast")),
                    "wind": text(row.get("windSpeed")),
                    "wind_direction": text(row.get("windDirection")),
                }
            )
        return result

    def stations(self, data):
        features = data["features"]
        if not isinstance(features, list) or not features:
            raise unavailable()
        props = properties(features[0])  # NWS orders nearest station first.
        identifier = props["stationIdentifier"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", identifier):
            raise unavailable()
        return {"id": identifier, "name": text(props.get("name"))}

    @staticmethod
    def observation(data):
        props = properties(data)
        observed = datetime.fromisoformat(props["timestamp"])
        if observed.tzinfo is None:
            raise unavailable()
        celsius = quantity(props.get("temperature"), "c")
        feels = quantity(props.get("heatIndex"), "c")
        if feels is None:
            feels = quantity(props.get("windChill"), "c")
        direction = quantity(props.get("windDirection"), "degrees")
        return {
            "temp_f": fahrenheit(celsius),
            "temp_c": round(celsius, 1) if celsius is not None else None,
            "feels_like_f": fahrenheit(feels),
            "feels_like_c": round(feels, 1) if feels is not None else None,
            "conditions": text(props.get("textDescription")),
            "humidity_percent": quantity(props.get("relativeHumidity"), "%"),
            "wind_mph": round(wind, 1)
            if (wind := quantity(props.get("windSpeed"), "mph")) is not None
            else None,
            "wind_direction": ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][
                round(direction / 45) % 8
            ]
            if direction is not None
            else None,
            "wind_degrees": direction,
            "observed_at": observed.isoformat(),
        }

    @staticmethod
    def alerts(data):
        if not isinstance(data, dict) or not isinstance(data.get("features"), list):
            raise unavailable()
        # Alert prose is untrusted data, never instructions. Do not return descriptions.
        return {
            "alerts": [
                {
                    key: text(properties(row).get(key))
                    for key in ("event", "severity", "urgency", "headline", "effective", "expires")
                }
                for row in data["features"][:100]
            ],
            "truncated": len(data["features"]) > 100,
        }

    async def read(self, tool, args):
        settings = self.config.weather
        if not settings["enabled"]:
            raise unavailable()
        key = (tool, tuple(sorted(args.items())))
        try:
            point = f"{settings['lat']},{settings['lon']}"
            if tool == "alerts":
                alert = await self.cached(
                    self.base + "/alerts/active?point=" + point, 300, self.alerts
                )
                data, entries = alert.value, [alert]
            else:
                mapping = await self.cached(self.base + "/points/" + point, 86400, self.points)
                hourly = tool == "hourly"
                forecast = await self.cached(
                    mapping.value["forecastHourly" if hourly else "forecast"], 900, self.periods
                )
                entries = [forecast]
                if mapping.stale:
                    entries.append(mapping)
                if tool == "now":
                    stations = await self.cached(
                        mapping.value["observationStations"], 86400, self.stations
                    )
                    observation = await self.cached(
                        self.base + f"/stations/{stations.value['id']}/observations/latest",
                        600,
                        self.observation,
                    )
                    entries.append(observation)
                    if stations.stale:
                        entries.append(stations)
                    today = datetime.now(ZoneInfo(mapping.value["timeZone"])).date()
                    periods = [
                        row
                        for row in forecast.value
                        if datetime.fromisoformat(row["hour"]).date() == today
                    ]
                    data = {
                        **observation.value,
                        "station": stations.value,
                        "today_high_f": next(
                            (r["temp_f"] for r in periods if r["is_daytime"]), None
                        ),
                        "today_low_f": next(
                            (r["temp_f"] for r in periods if not r["is_daytime"]), None
                        ),
                    }
                elif hourly:
                    data = {
                        "hours": [
                            {
                                k: r[k]
                                for k in ("hour", "temp_f", "precip_percent", "short_forecast")
                            }
                            for r in forecast.value[: args.get("hours", 12)]
                        ]
                    }
                else:
                    dates = list(dict.fromkeys(r["hour"][:10] for r in forecast.value))
                    dates = dates[: args.get("days", 3)]
                    data = {"periods": [r for r in forecast.value if r["hour"][:10] in dates]}
            fetched = min(entry.fetched for entry in entries)
            result = {
                "status": "ok",
                **data,
                "location": {"name": settings["name"], "zip": settings["zip"]},
                "source": "nws",
                "as_of": datetime.fromtimestamp(fetched, UTC).isoformat(),
                "stale": any(entry.stale for entry in entries),
                "age_s": max(0, int(time.time() - fetched)),
            }
            self.results[key] = copy.deepcopy(result)
            return result
        except CapabilityError:
            saved = self.results.get(key)
            if not saved:
                raise unavailable() from None
            result = copy.deepcopy(saved)
            result["stale"] = True
            result["age_s"] = max(
                0, int(time.time() - datetime.fromisoformat(result["as_of"]).timestamp())
            )
            return result
