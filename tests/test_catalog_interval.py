import os

from pterodactyl_operator.handlers.catalog import (
    DEFAULT_CATALOG_REFRESH_SECONDS,
    catalog_refresh_interval,
)


def test_catalog_refresh_defaults_to_fifteen_minutes() -> None:
    assert DEFAULT_CATALOG_REFRESH_SECONDS == 900.0
    assert catalog_refresh_interval("") == 900.0
    previous = os.environ.pop("PTERODACTYL_CATALOG_REFRESH_INTERVAL", None)
    try:
        assert catalog_refresh_interval() == 900.0
    finally:
        if previous is not None:
            os.environ["PTERODACTYL_CATALOG_REFRESH_INTERVAL"] = previous


def test_catalog_refresh_accepts_duration() -> None:
    assert catalog_refresh_interval("5m") == 300.0
    assert catalog_refresh_interval("90s") == 90.0
    assert catalog_refresh_interval("1h") == 3600.0
    assert catalog_refresh_interval("120") == 120.0


def test_catalog_refresh_rejects_bad_values() -> None:
    assert catalog_refresh_interval("0") == DEFAULT_CATALOG_REFRESH_SECONDS
    assert catalog_refresh_interval("-5m") == DEFAULT_CATALOG_REFRESH_SECONDS
    assert catalog_refresh_interval("soon") == DEFAULT_CATALOG_REFRESH_SECONDS
