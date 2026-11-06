"""Opt-in storage sampling and closed, finite alert thresholds."""

DEFAULT_STORAGE = {
    "enabled": False,
    "warn_percent": 90,
    "critical_percent": 95,
    "warn_days_to_full": 14,
    "hysteresis_percent": 2,
    "temperature_c": 55,
    "nvme_percentage_used": 90,
}


def storage_settings(value):
    if not isinstance(value, dict) or set(value) - DEFAULT_STORAGE.keys():
        raise ValueError("Invalid storage fields")
    result = {**DEFAULT_STORAGE, **value}
    if type(result["enabled"]) is not bool:
        raise ValueError("Storage enabled must be boolean")
    for key, low, high in (
        ("warn_percent", 1, 100),
        ("critical_percent", 1, 100),
        ("warn_days_to_full", 1, 365),
        ("hysteresis_percent", 0, 20),
        ("temperature_c", 20, 100),
        ("nvme_percentage_used", 1, 100),
    ):
        if type(result[key]) not in (int, float) or not low <= result[key] <= high:
            raise ValueError("Invalid storage threshold")
    if not result["hysteresis_percent"] < result["warn_percent"] < result["critical_percent"]:
        raise ValueError("Storage thresholds must be ordered")
    return result
