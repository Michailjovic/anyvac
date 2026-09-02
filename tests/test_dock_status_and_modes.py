"""Tests for the `_extract_device` fields HA 2026.9 / python-roborock 7.x touched.

Two independent things land here, both in `_extract_device`, which the rest of
the suite deliberately leaves alone ("that coupling is exercised in the field",
`test_coordinator_pipeline.py`). They are tested because both are cases where
the *upstream library changed its answer* rather than the code changing its
question — exactly the kind of drift a field test notices late and a fake
notices immediately:

1. `_mode_is_off` / the `vacuuming` flag. python-roborock 7.x stopped offering
   `VacuumModes.OFF_RAISE_MAIN_BRUSH` (code 109) in `fan_speed_options` for
   pure-clean-mop devices that can raise the main brush, so `fan_speed_name`
   went from `"off_raise_main_brush"` (5.x) to `"off"` (7.x) on exactly those
   devices. The old exact-match test did not recognise the 5.x spelling as off,
   so a mop-only pass counted as vacuuming and painted the dry layer — the
   docs/16 bug class.

2. `dock_status.features` / `dock_status.running`. docs/26 §3 recorded that
   HA/firmware has no documented way to report installed dock accessories. HA
   2026.9's own dock switches disproved that by gating on
   `device_features.dock_features`, so the coordinator now publishes those flags
   (and which cycle is running) instead of leaving the card to infer capability
   from a hand-maintained `dock_type` table.

`_extract_device` touches no instance state, so it is called unbound on a fake
Roborock coordinator — a plain object tree of exactly the attributes it reads.
Everything is `getattr`-based upstream, so an unset attribute is a legitimate
"library does not report this", not a broken fixture.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.anyvac.coordinator import AnyVacCoordinator, _mode_is_off


class _Room:
    def __init__(self, number: int, name: str) -> None:
        self.number = number
        self.name = name
        self.x0 = self.y0 = 0
        self.x1 = self.y1 = 10
        self.pos_x = self.pos_y = 5


def _map_data() -> Any:
    """Minimal parser MapData stand-in — every field is read with getattr."""
    return SimpleNamespace(
        vacuum_position=None,
        charger=None,
        path=None,
        mop_path=None,
        rooms={16: _Room(16, "Kitchen")},
        cleaned_rooms=[],
        vacuum_room=None,
        vacuum_room_name=None,
        image=None,
    )


def _rb_coordinator(
    *,
    status: dict[str, Any],
    dock_features: Any = None,
    smart_wash: Any = None,
) -> Any:
    """A fake `roborock` v1 coordinator shaped like the real object tree."""
    md = _map_data()
    home = SimpleNamespace(
        home_map_content={0: SimpleNamespace(map_data=md)},
        current_map_data=SimpleNamespace(map_flag=0),
        current_rooms=[SimpleNamespace(segment_id=16, name="Kitchen")],
    )
    return SimpleNamespace(
        duid="abc123",
        duid_slug="abc123",
        device=SimpleNamespace(name="S8 MaxV Ultra"),
        properties_api=SimpleNamespace(
            home=home,
            status=SimpleNamespace(**status),
            smart_wash_params=smart_wash,
            device_features=SimpleNamespace(dock_features=dock_features)
            if dock_features is not None
            else None,
        ),
    )


def _extract(coord: Any) -> dict[str, Any]:
    device = AnyVacCoordinator._extract_device(None, coord)  # type: ignore[arg-type]
    assert device is not None
    return device.data


# --------------------------------------------------------------------------
# 1. fan/water mode "is it off?"
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("off", True),
        ("OFF", True),
        (" off ", True),
        # python-roborock 5.31.1 (HA <= 2026.8) spelling for the same thing.
        ("off_raise_main_brush", True),
        ("none", True),
        ("closed", True),
        ("quiet", False),
        ("max_plus", False),
        ("standard", False),
        (None, False),
    ],
)
def test_mode_is_off(name: Any, expected: bool) -> None:
    assert _mode_is_off(name) is expected


def test_mop_only_pass_with_legacy_fan_name_is_not_vacuuming() -> None:
    """A mop-only pass must not count as vacuuming, on either library version.

    Regression: on python-roborock 5.31.1 an S8-class device reported
    `fan_speed_name == "off_raise_main_brush"` for the app's mop-only mode. The
    old membership test (`not in ("off", "none", "closed")`) said "vacuuming",
    so the dry layer and dry coverage were painted by a wet clean.
    """
    coord = _rb_coordinator(
        status={
            "in_cleaning": True,
            "state_name": "cleaning",
            "fan_speed_name": "off_raise_main_brush",
            "water_mode_name": "medium",
            "is_water_box_carriage_attached": True,
        }
    )
    data = _extract(coord)
    assert data["clean_type"] == "wet"
    assert data["vacuuming"] is False


def test_mop_only_pass_with_current_fan_name_is_not_vacuuming() -> None:
    """The same clean on python-roborock 7.1.1, which now reports plain "off"."""
    coord = _rb_coordinator(
        status={
            "in_cleaning": True,
            "state_name": "cleaning",
            "fan_speed_name": "off",
            "water_mode_name": "medium",
            "is_water_box_carriage_attached": True,
        }
    )
    data = _extract(coord)
    assert data["clean_type"] == "wet"
    assert data["vacuuming"] is False


def test_suction_on_still_counts_as_vacuuming() -> None:
    """Control: the fix must not turn a real dry pass into "not vacuuming"."""
    coord = _rb_coordinator(
        status={
            "in_cleaning": True,
            "state_name": "cleaning",
            "fan_speed_name": "balanced",
            "water_mode_name": "off",
        }
    )
    data = _extract(coord)
    assert data["clean_type"] == "dry"
    assert data["vacuuming"] is True


# --------------------------------------------------------------------------
# 2. dock capabilities + running cycle
# --------------------------------------------------------------------------


class _FullDock:
    """Stands in for RoborockDockFeatures on a wash+dry dock (S8 MaxV Ultra)."""

    has_dock = True
    is_collectable = True
    is_washable = True
    is_dryable = True


class _EmptyOnlyDock:
    """An auto-empty-only dock (S7 MaxV): collect yes, wash/dry no."""

    has_dock = True
    is_collectable = True
    is_washable = False
    is_dryable = False


def test_dock_features_published_for_a_full_dock() -> None:
    coord = _rb_coordinator(status={"state_name": "charging"}, dock_features=_FullDock())
    features = _extract(coord)["dock_status"]["features"]
    assert features == {
        "has_dock": True,
        "is_collectable": True,
        "is_washable": True,
        "is_dryable": True,
    }


def test_dock_features_distinguish_an_empty_only_dock() -> None:
    """The distinction the card used to guess from a `dock_type` tier table."""
    coord = _rb_coordinator(
        status={"state_name": "charging"}, dock_features=_EmptyOnlyDock()
    )
    features = _extract(coord)["dock_status"]["features"]
    assert features["is_collectable"] is True
    assert features["is_washable"] is False
    assert features["is_dryable"] is False


def test_missing_device_features_degrades_to_none() -> None:
    """No `device_features` (older library, or a vacuum with no dock at all).

    All-None is the signal the card falls back to its `dock_type` tier on — so
    this must never become False, which would read as "confirmed absent" and
    hide every dock button.
    """
    coord = _rb_coordinator(status={"state_name": "charging"})
    features = _extract(coord)["dock_status"]["features"]
    assert set(features.values()) == {None}


def test_a_raising_dock_feature_does_not_break_the_poll() -> None:
    """Every flag is a computed property upstream; one blowing up is not fatal."""

    class _Exploding:
        has_dock = True
        is_collectable = True

        @property
        def is_washable(self) -> bool:
            raise RuntimeError("library changed under us")

        is_dryable = False

    data = _extract(
        _rb_coordinator(status={"state_name": "charging"}, dock_features=_Exploding())
    )
    features = data["dock_status"]["features"]
    assert features["has_dock"] is True
    assert features["is_washable"] is None
    # ...and the rest of the poll still produced a normal payload.
    assert data["rooms"][0]["name"] == "Kitchen"


@pytest.mark.parametrize(
    ("state_name", "running_key"),
    [
        ("emptying_the_bin", "empty"),
        ("washing_the_mop", "wash"),
        ("washing_the_mop_2", "wash"),
    ],
)
def test_running_cycle_from_raw_state(state_name: str, running_key: str) -> None:
    """Mirrors what HA 2026.9's own dock switches report as `is_on`."""
    data = _extract(_rb_coordinator(status={"state_name": state_name}))
    running = data["dock_status"]["running"]
    assert running[running_key] is True
    for other in ("empty", "wash"):
        if other != running_key:
            assert running[other] is False


def test_dry_running_comes_from_dry_status() -> None:
    on = _extract(_rb_coordinator(status={"state_name": "charging", "dry_status": 1}))
    assert on["dock_status"]["dry_status"] == 1
    assert on["dock_status"]["running"]["dry"] is True

    off = _extract(_rb_coordinator(status={"state_name": "charging", "dry_status": 0}))
    assert off["dock_status"]["running"]["dry"] is False


def test_dry_running_is_none_when_not_reported() -> None:
    """A dock that does not report `dry_status` must not read as "not drying"."""
    data = _extract(_rb_coordinator(status={"state_name": "charging"}))
    assert data["dock_status"]["running"]["dry"] is None
