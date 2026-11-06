# Sun calculation routines.
# Ported from Sun.swift (created by Marco Arment, 1/17/21).
# Solar-math functions are directly translated from the NOAA Solar Calculator:
# https://www.esrl.noaa.gov/gmd/grad/solcalc/
#
# This is free and unencumbered software released into the public domain.
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any means.
# For more information, please refer to <http://unlicense.org/>

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SunTimes:
    """Solar event times and polar day/night status for a local calendar day."""

    sunrise: datetime | None
    sunset: datetime | None
    dawn: datetime | None
    dusk: datetime | None
    solar_noon: datetime
    always_up: bool
    always_down: bool


def _degrees_to_radians(degrees: float) -> float:
    return degrees * math.pi / 180.0


def _radians_to_degrees(radians: float) -> float:
    return radians * 180.0 / math.pi


def _julian_day(year: int, month: int, day: int) -> float:
    y = float(year)
    m = float(month)
    d = float(day)
    if m <= 2.0:
        y -= 1.0
        m += 12.0
    a = math.floor(y / 100.0)
    b = 2.0 - a + math.floor(a / 4.0)
    y += 4716.0
    m += 1.0
    return math.floor(365.25 * y) + math.floor(30.6001 * m) + (d + b - 1524.5)


def _julian_cent(jd: float) -> float:
    return (jd - 2451545.0) / 36525.0


def _geom_mean_long_sun(t: float) -> float:
    l0 = 280.46646 + t * (36000.76983 + t * 0.0003032)
    return l0 % 360.0


def _geom_mean_anomaly_sun(t: float) -> float:
    return 357.52911 + t * (35999.05029 - 0.0001537 * t)


def _eccentricity_earth_orbit(t: float) -> float:
    return 0.016708634 - t * (0.000042037 + 0.0000001267 * t)


def _sun_eq_of_center(t: float) -> float:
    mrad = _degrees_to_radians(_geom_mean_anomaly_sun(t))
    return (
        math.sin(mrad) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2.0 * mrad) * (0.019993 - 0.000101 * t)
        + math.sin(3.0 * mrad) * 0.000289
    )


def _sun_true_long(t: float) -> float:
    return _geom_mean_long_sun(t) + _sun_eq_of_center(t)


def _sun_apparent_long(t: float) -> float:
    omega = 125.04 - 1934.136 * t
    return _sun_true_long(t) - 0.00569 - 0.00478 * math.sin(_degrees_to_radians(omega))


def _mean_obliquity(t: float) -> float:
    return 23.0 + (26.0 + (21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0


def _obliquity_correction(t: float) -> float:
    return _mean_obliquity(t) + 0.00256 * math.cos(_degrees_to_radians(125.04 - 1934.136 * t))


def _sun_declination(t: float) -> float:
    e = _obliquity_correction(t)
    lambd = _sun_apparent_long(t)
    sint = math.sin(_degrees_to_radians(e)) * math.sin(_degrees_to_radians(lambd))
    return _radians_to_degrees(math.asin(sint))


def _equation_of_time(t: float) -> float:
    epsilon = _obliquity_correction(t)
    l0 = _geom_mean_long_sun(t)
    e = _eccentricity_earth_orbit(t)
    m = _geom_mean_anomaly_sun(t)
    y = math.tan(_degrees_to_radians(epsilon) / 2.0) ** 2
    sin2l0 = math.sin(2.0 * _degrees_to_radians(l0))
    sinm = math.sin(_degrees_to_radians(m))
    cos2l0 = math.cos(2.0 * _degrees_to_radians(l0))
    sin4l0 = math.sin(4.0 * _degrees_to_radians(l0))
    sin2m = math.sin(2.0 * _degrees_to_radians(m))
    etime = (
        y * sin2l0
        - 2.0 * e * sinm
        + 4.0 * e * y * sinm * cos2l0
        - 0.5 * y * y * sin4l0
        - 1.25 * e * e * sin2m
    )
    return _radians_to_degrees(etime) * 4.0


def _hour_angle_sunrise(latitude: float, declination: float, zenith: float = 90.833) -> float:
    lat_rad = _degrees_to_radians(latitude)
    sd_rad = _degrees_to_radians(declination)
    cos_lat = math.cos(lat_rad)
    if abs(cos_lat) < 1e-9:
        zen = 90.0 - declination if latitude > 0.0 else 90.0 + declination
        return 1.1 if zen > zenith else -1.1
    return math.cos(_degrees_to_radians(zenith)) / (cos_lat * math.cos(sd_rad)) - math.tan(
        lat_rad
    ) * math.tan(sd_rad)


def _sunrise_or_set_utc(
    rise: bool, jd: float, latitude: float, longitude: float, zenith: float = 90.833
) -> float | None:
    t = _julian_cent(jd)
    eq_time = _equation_of_time(t)
    solar_dec = _sun_declination(t)
    cos_ha = _hour_angle_sunrise(latitude, solar_dec, zenith=zenith)
    if cos_ha > 1.0 or cos_ha < -1.0:
        return None
    ha = _radians_to_degrees(math.acos(cos_ha))
    if not rise:
        ha = -ha
    delta = longitude + ha
    return 720.0 - (4.0 * delta) - eq_time


def _refraction(elevation: float) -> float:
    if elevation > 85.0:
        return 0.0
    te = math.tan(_degrees_to_radians(elevation))
    te3 = te * te * te
    if elevation > 5.0:
        correction = 58.1 / te - 0.07 / te3 + 0.000086 / (te3 * te * te)
    elif elevation > -0.575:
        correction = 1735.0 + elevation * (
            -518.2 + elevation * (103.4 + elevation * (-12.79 + elevation * 0.711))
        )
    else:
        correction = -20.774 / te
    return correction / 3600.0


def _calc_solar_noon(day: date, latitude: float, longitude: float, tz: ZoneInfo) -> datetime:
    approx_noon_local = datetime(day.year, day.month, day.day, 12, 0, tzinfo=tz)
    approx_noon_utc = approx_noon_local.astimezone(UTC)
    base_date = approx_noon_utc.date()

    candidates: list[datetime] = []
    for offset_days in (0, -1, 1):
        target_base = base_date + timedelta(days=offset_days)
        jd = _julian_day(target_base.year, target_base.month, target_base.day)
        tnoon = _julian_cent(jd - longitude / 360.0)
        eq_time = _equation_of_time(tnoon)
        sol_noon_offset = 720.0 - (longitude * 4.0) - eq_time
        newt = _julian_cent(jd + sol_noon_offset / 1440.0)
        eq_time = _equation_of_time(newt)
        noon_utc_min = 720.0 - (longitude * 4.0) - eq_time
        dt_utc = datetime(
            target_base.year, target_base.month, target_base.day, tzinfo=UTC
        ) + timedelta(minutes=noon_utc_min)
        dt_local = dt_utc.astimezone(tz)
        if dt_local.date() == day:
            candidates.append(dt_local)

    if candidates:
        candidates.sort(key=lambda dt: abs((dt - approx_noon_local).total_seconds()))
        return candidates[0]
    return approx_noon_local


def _calc_event(
    day: date, latitude: float, longitude: float, tz: ZoneInfo, rise: bool, zenith: float
) -> datetime | None:
    approx_noon_local = datetime(day.year, day.month, day.day, 12, 0, tzinfo=tz)
    approx_noon_utc = approx_noon_local.astimezone(UTC)
    base_date = approx_noon_utc.date()

    candidates: list[datetime] = []
    for offset_days in (0, -1, 1):
        target_base = base_date + timedelta(days=offset_days)
        jd = _julian_day(target_base.year, target_base.month, target_base.day)
        t1 = _sunrise_or_set_utc(rise, jd, latitude, longitude, zenith=zenith)
        if t1 is None:
            continue
        t2 = _sunrise_or_set_utc(rise, jd + t1 / 1440.0, latitude, longitude, zenith=zenith)
        if t2 is None:
            continue
        dt_utc = datetime(
            target_base.year, target_base.month, target_base.day, tzinfo=UTC
        ) + timedelta(minutes=t2)
        dt_local = dt_utc.astimezone(tz)
        if dt_local.date() == day:
            candidates.append(dt_local)

    if not candidates:
        return None
    candidates.sort(key=lambda dt: abs((dt - approx_noon_local).total_seconds()))
    return candidates[0]


def sun_times(day: date, latitude: float, longitude: float, tz: ZoneInfo) -> SunTimes:
    """Calculate solar event times and polar day/night status for a calendar day in a timezone."""
    if not -90.0 <= latitude <= 90.0:
        raise ValueError(f"latitude out of range [-90, 90]: {latitude}")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"longitude out of range [-180, 180]: {longitude}")

    solar_noon = _calc_solar_noon(day, latitude, longitude, tz)

    jd_noon = 2440587.5 + solar_noon.astimezone(UTC).timestamp() / 86400.0
    t_noon = _julian_cent(jd_noon)
    dec_noon = _sun_declination(t_noon)
    cos_ha = _hour_angle_sunrise(latitude, dec_noon, zenith=90.833)

    always_up = cos_ha < -1.0
    always_down = cos_ha > 1.0

    if always_up:
        sunrise = None
        sunset = None
        dawn = None
        dusk = None
    elif always_down:
        sunrise = None
        sunset = None
        dawn = _calc_event(day, latitude, longitude, tz, rise=True, zenith=96.0)
        dusk = _calc_event(day, latitude, longitude, tz, rise=False, zenith=96.0)
    else:
        sunrise = _calc_event(day, latitude, longitude, tz, rise=True, zenith=90.833)
        sunset = _calc_event(day, latitude, longitude, tz, rise=False, zenith=90.833)
        dawn = _calc_event(day, latitude, longitude, tz, rise=True, zenith=96.0)
        dusk = _calc_event(day, latitude, longitude, tz, rise=False, zenith=96.0)

    return SunTimes(
        sunrise=sunrise,
        sunset=sunset,
        dawn=dawn,
        dusk=dusk,
        solar_noon=solar_noon,
        always_up=always_up,
        always_down=always_down,
    )


def elevation(when: datetime, latitude: float, longitude: float) -> float:
    """Return solar elevation in degrees above the horizon for a tz-aware datetime."""
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("when must be tz-aware")
    if not -90.0 <= latitude <= 90.0:
        raise ValueError(f"latitude out of range [-90, 90]: {latitude}")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"longitude out of range [-180, 180]: {longitude}")

    when_utc = when.astimezone(UTC)
    jd = 2440587.5 + when_utc.timestamp() / 86400.0
    t = _julian_cent(jd)
    eq_time = _equation_of_time(t)
    theta = _sun_declination(t)

    utc_minutes = (
        when_utc.hour * 3600 + when_utc.minute * 60 + when_utc.second + when_utc.microsecond / 1e6
    ) / 60.0
    true_solar_time = (utc_minutes + eq_time + 4.0 * longitude) % 1440.0

    hour_angle = true_solar_time / 4.0 - 180.0
    if hour_angle < -180.0:
        hour_angle += 360.0
    elif hour_angle > 180.0:
        hour_angle -= 360.0

    ha_rad = _degrees_to_radians(hour_angle)
    lat_rad = _degrees_to_radians(latitude)
    theta_rad = _degrees_to_radians(theta)

    csz = math.sin(lat_rad) * math.sin(theta_rad) + math.cos(lat_rad) * math.cos(
        theta_rad
    ) * math.cos(ha_rad)
    csz = max(-1.0, min(1.0, csz))

    zenith = _radians_to_degrees(math.acos(csz))
    exoatm_elevation = 90.0 - zenith
    ref_corr = _refraction(exoatm_elevation)
    solar_zen = zenith - ref_corr
    return 90.0 - solar_zen


def next_event(
    event: str,
    after: datetime,
    latitude: float,
    longitude: float,
    tz: ZoneInfo,
    offset_minutes: int = 0,
) -> datetime | None:
    """event is "sunrise", "sunset", "dawn" or "dusk"; the first occurrence
    (plus offset) strictly after `after`, searching up to 370 days; None if none (polar).
    """
    valid_events = ("sunrise", "sunset", "dawn", "dusk")
    if event not in valid_events:
        raise ValueError(f"Unknown solar event {event!r}, expected one of {valid_events}")
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("after must be tz-aware")
    if not -90.0 <= latitude <= 90.0:
        raise ValueError(f"latitude out of range [-90, 90]: {latitude}")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"longitude out of range [-180, 180]: {longitude}")

    start_date = after.astimezone(tz).date()
    days_back = max(0, (offset_minutes + 1439) // 1440) if offset_minutes > 0 else 0

    for day_offset in range(-days_back, 371):
        cur_day = start_date + timedelta(days=day_offset)
        st = sun_times(cur_day, latitude, longitude, tz)
        match event:
            case "sunrise":
                ev_dt = st.sunrise
            case "sunset":
                ev_dt = st.sunset
            case "dawn":
                ev_dt = st.dawn
            case "dusk":
                ev_dt = st.dusk
            case _:
                ev_dt = None

        if ev_dt is not None:
            candidate = (ev_dt.astimezone(UTC) + timedelta(minutes=offset_minutes)).astimezone(tz)
            if candidate > after:
                return candidate

    return None
