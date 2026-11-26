"""docs/53 §5: battery model, the timing model with charging, the runner."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.anyvac import battery as bm
from custom_components.anyvac import coordinator as c
from custom_components.anyvac.planner import simulate_queues




def test_charge_minutes_is_slower_above_80() -> None:
    m = bm.Model(charge={"lo": 1.0, "hi": 0.25})
    assert m.charge_minutes(30, 80) == 50
    assert m.charge_minutes(70, 90) == 10 + 40
    assert m.charge_minutes(90, 80) == 0


def test_drain_prefers_the_fan_speed_it_was_measured_at() -> None:
    m = bm.Model(drain={"dry": 0.5, "dry@max": 0.8})
    assert m.drain_rate("dry", "max") == 0.8
    assert m.drain_rate("dry", "quiet") == 0.5
    assert m.drain_rate("wet") == bm.DEFAULT_DRAIN["wet"]


def test_charge_target_follows_p1() -> None:
    assert bm.charge_target(40) == 63
    assert bm.charge_target(120) == 100


def _co(monkeypatch, now):
    co = object.__new__(c.AnyVacCoordinator)
    co._bat_seg, co._battery_learned = {}, {}
    co._battery_store = None
    co._delay_save = lambda *a, **k: None
    monkeypatch.setattr(c.dt_util, "utcnow", lambda: now[0])
    return co


def _dev(**d):
    base = {"in_cleaning": True, "status_state": "segment_cleaning", "clean_type": "dry",
            "mop_signal": {"fan_speed_name": "max"}}
    base.update(d)
    return SimpleNamespace(duid="s6", data=base)


def test_coordinator_learns_drain_and_charging(monkeypatch) -> None:
    t0 = c.dt_util.utcnow()
    now = [t0]
    co = _co(monkeypatch, now)

    def at(minute, **d):
        now[0] = t0 + timedelta(minutes=minute)
        co._note_battery(_dev(**d))

    for m in range(11):  # snapshots every minute, 0.6 %/min
        at(m, battery=80 - round(0.6 * m))
    at(11, battery=74, in_cleaning=False, status_state="charging")  # docked: closes
    assert co._battery_learned["s6"]["drain"] == {"dry": 0.6, "dry@max": 0.6}
    for m in range(12, 22):
        at(m, battery=74 + round(0.5 * (m - 11)), in_cleaning=False, status_state="charging")
    at(22, battery=80, in_cleaning=False, status_state="charging")  # now in "hi"
    assert co._battery_learned["s6"]["charge"]["lo"] == pytest.approx(0.5, abs=0.06)
    assert len(co._battery_learned["s6"]["samples"]) == 2
    assert co.battery_model("s6").drain_rate("dry", "max") == 0.6


def test_short_segments_teach_nothing(monkeypatch) -> None:
    t0 = c.dt_util.utcnow()
    now = [t0]
    co = _co(monkeypatch, now)
    co._note_battery(_dev(battery=80))
    now[0] = t0 + timedelta(minutes=2)
    co._note_battery(_dev(battery=78, in_cleaning=False, status_state="charging"))
    assert co._battery_learned == {}


def test_timing_model_charges_when_the_next_room_does_not_fit() -> None:
    m = bm.Model(drain={"dry": 1.0}, charge={"lo": 1.0, "hi": 1.0})
    r = {"items": [{"id": "a", "kind": "dry", "rooms": ["A"], "est_min": 20},
                   {"id": "b", "kind": "dry", "rooms": ["B"], "est_min": 20}],
         "bat": m, "battery": 60}
    times = simulate_queues({"r": r})
    # A: 60 -> 40. B needs 20 -> 20 < 25: charge to max(20+20+3, 25+20+1) = 46.
    assert times["a"] == (0.0, 20.0)
    start, end = times["b"]
    assert end == pytest.approx(20 + 0.35 + 6 + 20)
    r["battery"] = 100
    assert simulate_queues({"r": r})["b"][1] == pytest.approx(40.35)
