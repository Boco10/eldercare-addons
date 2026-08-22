"""Tests for the runtime privacy settings."""

from __future__ import annotations

import pytest

from app.config import settings
from app.storage import runtime_settings
from app.storage.database import Database


@pytest.fixture(autouse=True)
def restore_settings():
    """The settings are global — restore them after each test."""
    original = runtime_settings.PrivacySettings.current().to_dict()
    yield
    for key, value in original.items():
        setattr(settings, key, value)


async def make_db(tmp_path) -> Database:
    db = Database(tmp_path)
    await db.connect()
    return db


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize("mode", ["never", "critical_only", "on_request", "always"])
def test_valid_image_modes_accepted(mode):
    cleaned, error = runtime_settings.validate({"image_upload_mode": mode})
    assert error is None
    assert cleaned["image_upload_mode"] == mode


def test_unknown_image_mode_rejected():
    _, error = runtime_settings.validate({"image_upload_mode": "kuldj_mindent"})
    assert error is not None and "Unknown" in error


def test_retention_out_of_range_rejected():
    for value in (0, 400, -5):
        _, error = runtime_settings.validate({"local_raw_retention_days": value})
        assert error is not None


def test_non_editable_field_ignored():
    """The cloud address and the thresholds cannot be set from the local UI."""
    cleaned, error = runtime_settings.validate({
        "cloud_api_url": "http://tamado.example.com",
        "anomaly_threshold_high": 0.1,
        "send_daily_features": False,
    })
    assert error is None
    assert cleaned == {"send_daily_features": False}


# ------------------------------------------------------------- save and load

@pytest.mark.asyncio
async def test_save_applies_immediately(tmp_path):
    db = await make_db(tmp_path)
    await runtime_settings.save(db, {"image_upload_mode": "never"})

    assert settings.image_upload_mode == "never", "it has to take effect at once"
    await db.close()


@pytest.mark.asyncio
async def test_save_survives_restart(tmp_path):
    db = await make_db(tmp_path)
    await runtime_settings.save(db, {"image_upload_mode": "always",
                                     "send_daily_features": False})
    await db.close()

    # Restart: the default returns, then the override is loaded.
    settings.image_upload_mode = "critical_only"
    settings.send_daily_features = True

    db2 = await make_db(tmp_path)
    loaded = await runtime_settings.load(db2)

    assert loaded.image_upload_mode == "always"
    assert settings.image_upload_mode == "always"
    assert settings.send_daily_features is False
    await db2.close()


@pytest.mark.asyncio
async def test_invalid_save_changes_nothing(tmp_path):
    db = await make_db(tmp_path)
    before = settings.image_upload_mode

    result, error = await runtime_settings.save(db, {"image_upload_mode": "hopp"})

    assert result is None
    assert error is not None
    assert settings.image_upload_mode == before
    await db.close()


@pytest.mark.asyncio
async def test_corrupt_stored_value_does_not_break_startup(tmp_path):
    """A corrupt saved value must not prevent startup."""
    db = await make_db(tmp_path)
    await db.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                        (runtime_settings.META_KEY, "{ nem json"))
    await db.commit()

    loaded = await runtime_settings.load(db)

    assert loaded.image_upload_mode in runtime_settings.IMAGE_MODES
    await db.close()


@pytest.mark.asyncio
async def test_never_mode_blocks_snapshot(tmp_path):
    """The switch really bites: in never mode the image cannot even be fetched."""
    from app.ha.camera_client import CameraClient, may_upload_image

    db = await make_db(tmp_path)
    await runtime_settings.save(db, {"image_upload_mode": "never"})

    assert may_upload_image("manual") is False
    assert may_upload_image("critical") is False
    assert await CameraClient().snapshot("camera.living") is None
    await db.close()
