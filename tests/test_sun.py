"""Unit tests for solar position and event routines."""

from dataclasses import FrozenInstanceError, is_dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from switchboard.routines.sun import SunTimes, elevation, next_event, sun_times


def test_suntimes_dataclass_properties() -> None:
    """SunTimes must be a frozen dataclass."""
    tz = ZoneInfo("America/New_York")
    st = sun_times(date(2026, 6, 21), 40.7128, -74.0060, tz)

    assert is_dataclass(st)
    assert isinstance(st, SunTimes)
    with pytest.raises(FrozenInstanceError):
        st.always_up = True  # type: ignore[misc]


def test_new_york_accuracy_solstices() -> None:
    """New York sunrise and sunset must be within 3 minutes of NOAA expected values."""
    nyc = ZoneInfo("America/New_York")

    # 2026-06-21: sunrise ≈ 05:25 EDT, sunset ≈ 20:31 EDT
    st_summer = sun_times(date(2026, 6, 21), 40.7128, -74.0060, nyc)
    assert st_summer.sunrise is not None
    assert st_summer.sunset is not None
    assert not st_summer.always_up
    assert not st_summer.always_down

    expected_summer_rise = datetime(2026, 6, 21, 5, 25, tzinfo=nyc)
    expected_summer_set = datetime(2026, 6, 21, 20, 31, tzinfo=nyc)
    assert abs((st_summer.sunrise - expected_summer_rise).total_seconds()) <= 180
    assert abs((st_summer.sunset - expected_summer_set).total_seconds()) <= 180

    # 2026-12-21: sunrise ≈ 07:17 EST, sunset ≈ 16:32 EST
    st_winter = sun_times(date(2026, 12, 21), 40.7128, -74.0060, nyc)
    assert st_winter.sunrise is not None
    assert st_winter.sunset is not None
    assert not st_winter.always_up
    assert not st_winter.always_down

    expected_winter_rise = datetime(2026, 12, 21, 7, 17, tzinfo=nyc)
    expected_winter_set = datetime(2026, 12, 21, 16, 32, tzinfo=nyc)
    assert abs((st_winter.sunrise - expected_winter_rise).total_seconds()) <= 180
    assert abs((st_winter.sunset - expected_winter_set).total_seconds()) <= 180


def test_london_accuracy_summer_solstice() -> None:
    """London sunrise and sunset on summer solstice must be within 3 minutes of NOAA."""
    london = ZoneInfo("Europe/London")
    st = sun_times(date(2026, 6, 21), 51.5074, -0.1278, london)

    assert st.sunrise is not None
    assert st.sunset is not None
    # 2026-06-21: sunrise ≈ 04:43 BST, sunset ≈ 21:21 BST
    expected_rise = datetime(2026, 6, 21, 4, 43, tzinfo=london)
    expected_set = datetime(2026, 6, 21, 21, 21, tzinfo=london)
    assert abs((st.sunrise - expected_rise).total_seconds()) <= 180
    assert abs((st.sunset - expected_set).total_seconds()) <= 180


def test_dst_transitions_new_york() -> None:
    """DST change days land on the local date and sunrise/sunset match neighbouring days."""
    nyc = ZoneInfo("America/New_York")

    # Spring forward transition: 2026-03-08
    st_mar7 = sun_times(date(2026, 3, 7), 40.7128, -74.0060, nyc)
    st_mar8 = sun_times(date(2026, 3, 8), 40.7128, -74.0060, nyc)
    st_mar9 = sun_times(date(2026, 3, 9), 40.7128, -74.0060, nyc)

    assert st_mar8.sunrise is not None
    assert st_mar8.sunset is not None
    assert st_mar8.sunrise.date() == date(2026, 3, 8)
    assert st_mar8.sunset.date() == date(2026, 3, 8)
    assert st_mar8.solar_noon.date() == date(2026, 3, 8)

    # Physical duration in UTC between consecutive sunrise days is ~24 hours (within 3 minutes)
    assert st_mar7.sunrise is not None
    mar7_utc = st_mar7.sunrise.astimezone(UTC)
    mar8_utc = st_mar8.sunrise.astimezone(UTC)
    assert abs((mar8_utc - mar7_utc).total_seconds() - 86400) <= 180

    # Wall-clock difference between March 8 and March 9 (both EDT) is about 1 minute, not 60
    assert st_mar9.sunrise is not None
    mar_clock_diff = abs((st_mar9.sunrise - st_mar8.sunrise).total_seconds() - 86400)
    assert mar_clock_diff <= 120

    # Fall back transition: 2026-11-01
    st_oct31 = sun_times(date(2026, 10, 31), 40.7128, -74.0060, nyc)
    st_nov1 = sun_times(date(2026, 11, 1), 40.7128, -74.0060, nyc)
    st_nov2 = sun_times(date(2026, 11, 2), 40.7128, -74.0060, nyc)

    assert st_nov1.sunrise is not None
    assert st_nov1.sunset is not None
    assert st_nov1.sunrise.date() == date(2026, 11, 1)
    assert st_nov1.sunset.date() == date(2026, 11, 1)
    assert st_nov1.solar_noon.date() == date(2026, 11, 1)

    # Physical duration in UTC between consecutive sunrise days is ~24 hours (within 3 minutes)
    assert st_oct31.sunrise is not None
    oct31_utc = st_oct31.sunrise.astimezone(UTC)
    nov1_utc = st_nov1.sunrise.astimezone(UTC)
    assert abs((nov1_utc - oct31_utc).total_seconds() - 86400) <= 180

    # Wall-clock difference between Nov 1 and Nov 2 (both EST) is about 1 minute, not 60
    assert st_nov2.sunrise is not None
    nov_clock_diff = abs((st_nov2.sunrise - st_nov1.sunrise).total_seconds() - 86400)
    assert nov_clock_diff <= 120


def test_polar_cases_tromso() -> None:
    """Tromsø polar day in summer, polar night in winter, and next sunrise in January."""
    tromso_tz = ZoneInfo("Europe/Oslo")
    lat, lon = 69.6492, 18.9553

    # Polar day: 2026-06-21
    st_summer = sun_times(date(2026, 6, 21), lat, lon, tromso_tz)
    assert st_summer.always_up is True
    assert st_summer.always_down is False
    assert st_summer.sunrise is None
    assert st_summer.sunset is None
    assert st_summer.dawn is None
    assert st_summer.dusk is None

    # Polar night: 2026-12-21
    st_winter = sun_times(date(2026, 12, 21), lat, lon, tromso_tz)
    assert st_winter.always_up is False
    assert st_winter.always_down is True
    assert st_winter.sunrise is None
    assert st_winter.sunset is None
    # Civil twilight still occurs near solar noon in Tromsø on Dec 21
    assert st_winter.dawn is not None
    assert st_winter.dusk is not None

    # next_event from winter solstice finds the first sunrise in January
    after_dt = datetime(2026, 12, 21, 12, 0, tzinfo=tromso_tz)
    first_sunrise = next_event("sunrise", after_dt, lat, lon, tromso_tz)
    assert first_sunrise is not None
    assert first_sunrise.year == 2027
    assert first_sunrise.month == 1
    assert 14 <= first_sunrise.day <= 20


def test_next_event_offset_and_boundary() -> None:
    """next_event respects offsets and advances to tomorrow when after exceeds today's event."""
    nyc = ZoneInfo("America/New_York")
    lat, lon = 40.7128, -74.0060

    st_today = sun_times(date(2026, 6, 21), lat, lon, nyc)
    assert st_today.sunset is not None
    sunset_today = st_today.sunset

    # With offset = -30 minutes
    target_today = sunset_today - timedelta(minutes=30)

    # 1 minute before target_today -> should return target_today
    before = target_today - timedelta(minutes=1)
    ev_before = next_event("sunset", before, lat, lon, nyc, offset_minutes=-30)
    assert ev_before is not None
    assert ev_before == target_today

    # 1 minute after target_today -> should advance to tomorrow's sunset - 30m
    after = target_today + timedelta(minutes=1)
    ev_after = next_event("sunset", after, lat, lon, nyc, offset_minutes=-30)
    assert ev_after is not None
    assert ev_after.date() == date(2026, 6, 22)

    st_tomorrow = sun_times(date(2026, 6, 22), lat, lon, nyc)
    assert st_tomorrow.sunset is not None
    expected_tomorrow_target = st_tomorrow.sunset - timedelta(minutes=30)
    assert ev_after == expected_tomorrow_target

    # Without offset: after just after today's sunset returns tomorrow's sunset
    just_after_sunset = sunset_today + timedelta(minutes=1)
    next_sunset = next_event("sunset", just_after_sunset, lat, lon, nyc)
    assert next_sunset is not None
    assert next_sunset == st_tomorrow.sunset


def test_civil_dawn_and_dusk_elevations() -> None:
    """Elevation at dawn and dusk should be approximately -6 degrees."""
    nyc = ZoneInfo("America/New_York")
    lat, lon = 40.7128, -74.0060
    st = sun_times(date(2026, 6, 21), lat, lon, nyc)

    assert st.dawn is not None
    assert st.dusk is not None
    assert st.sunrise is not None
    assert st.sunset is not None

    # Dawn precedes sunrise, dusk follows sunset
    assert st.dawn < st.sunrise
    assert st.dusk > st.sunset

    el_dawn = elevation(st.dawn, lat, lon)
    el_dusk = elevation(st.dusk, lat, lon)
    # Solar center is at -6° at civil twilight
    assert abs(el_dawn - (-6.0)) <= 0.6
    assert abs(el_dusk - (-6.0)) <= 0.6


def test_elevation_values() -> None:
    """Elevation calculations must reflect solar noon height and nighttime values."""
    nyc = ZoneInfo("America/New_York")
    lat, lon = 40.7128, -74.0060

    # Summer solstice solar noon in NYC: ~72.7°
    noon_summer = datetime(2026, 6, 21, 12, 58, tzinfo=nyc)
    el_summer = elevation(noon_summer, lat, lon)
    assert 72.0 <= el_summer <= 73.5

    # Winter solstice solar noon in NYC: ~25.8°
    noon_winter = datetime(2026, 12, 21, 11, 54, tzinfo=nyc)
    el_winter = elevation(noon_winter, lat, lon)
    assert 25.0 <= el_winter <= 26.5

    # Deep night: negative elevation
    midnight = datetime(2026, 6, 21, 0, 0, tzinfo=nyc)
    el_night = elevation(midnight, lat, lon)
    assert el_night < -20.0


def test_input_validation() -> None:
    """Functions must validate timezone awareness, event names, and coordinates."""
    nyc = ZoneInfo("America/New_York")
    naive_dt = datetime(2026, 6, 21, 12, 0)
    aware_dt = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)

    # elevation requires tz-aware datetime
    with pytest.raises(ValueError, match="tz-aware"):
        elevation(naive_dt, 40.7128, -74.0060)

    # elevation validates coordinate boundaries
    with pytest.raises(ValueError, match="latitude"):
        elevation(aware_dt, 95.0, -74.0060)
    with pytest.raises(ValueError, match="longitude"):
        elevation(aware_dt, 40.7128, 200.0)

    # sun_times validates coordinate boundaries
    with pytest.raises(ValueError, match="latitude"):
        sun_times(date(2026, 6, 21), -95.0, -74.0060, nyc)
    with pytest.raises(ValueError, match="longitude"):
        sun_times(date(2026, 6, 21), 40.7128, -200.0, nyc)

    # next_event requires valid event name
    with pytest.raises(ValueError, match="Unknown solar event"):
        next_event("eclipse", aware_dt, 40.7128, -74.0060, nyc)

    # next_event requires tz-aware datetime
    with pytest.raises(ValueError, match="tz-aware"):
        next_event("sunrise", naive_dt, 40.7128, -74.0060, nyc)
