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


def _push(co, level):
    co._note_battery_push("s6", SimpleNamespace(properties_api=SimpleNamespace(status=SimpleNamespace(battery=level))))


def test_drain_is_measured_between_level_changes(monkeypatch) -> None:
    """1 % every 100 s (0.6 %/min). Map snapshots come every 45 s at their own
    phase; the level changes are pushed when they happen. Edge to edge gives
    the true rate; snapshot ends inside a 1 % step would not."""
    t0 = c.dt_util.utcnow()
    now = [t0]
    co = _co(monkeypatch, now)
    events = []
    for k in range(8):                       # level 80 -> 72, a change every 100 s from t=30 s
        events.append((30 + 100 * k, "push", 79 - k))
    for k in range(20):                      # snapshots every 45 s
        events.append((5 + 45 * k, "snap", None))
    events.append((900, "dock", None))
    level = 80
    for sec, kind, val in sorted(events, key=lambda e: e[0]):
        now[0] = t0 + timedelta(seconds=sec)
        if kind == "push":
            level = val
            _push(co, level)
        elif kind == "snap":
            co._note_battery(_dev(battery=level))
        else:
            co._note_battery(_dev(battery=level, in_cleaning=False, status_state="charging"))
            break
    assert co._battery_learned["s6"]["drain"]["dry"] == pytest.approx(0.6, abs=0.005)


def test_coordinator_learns_drain_and_charging(monkeypatch) -> None:
    """Without pushes (snapshots only) it still learns, from the snapshot edges."""
    t0 = c.dt_util.utcnow()
    now = [t0]
    co = _co(monkeypatch, now)

    def at(minute, **d):
        now[0] = t0 + timedelta(minutes=minute)
        co._note_battery(_dev(**d))

    for m in range(11):
        at(m, battery=80 - m // 2)            # a change every 2 min = 0.5 %/min
    at(11, battery=75, in_cleaning=False, status_state="charging")
    assert co._battery_learned["s6"]["drain"] == {"dry": 0.5, "dry@max": 0.5}
    for m in range(12, 32):
        at(m, battery=75 + (m - 11) // 4, in_cleaning=False, status_state="charging")
    at(32, battery=80, in_cleaning=False, status_state="charging")  # now in "hi"
    assert co._battery_learned["s6"]["charge"]["lo"] == pytest.approx(0.25, abs=0.01)
    assert len(co._battery_learned["s6"]["samples"]) == 2
    assert co.battery_model("s6").drain_rate("dry", "max") == 0.5


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


def test_the_timing_model_reports_its_charging_stops() -> None:
    m = bm.Model(drain={"dry": 1.0}, charge={"lo": 1.0, "hi": 1.0})
    r = {"items": [{"id": "a", "kind": "dry", "rooms": ["A"], "est_min": 20},
                   {"id": "b", "kind": "dry", "rooms": ["B"], "est_min": 20}],
         "bat": m, "battery": 60}
    stops: dict = {}
    simulate_queues({"r": r}, stops)
    assert stops == {"r": [{"before": "b", "start": pytest.approx(20.0), "end": pytest.approx(26.0), "to_pct": 46}]}
    r["battery"] = 100
    stops = {}
    simulate_queues({"r": r}, stops)
    assert stops == {"r": []}


def test_a_drive_to_the_next_room_does_not_cut_the_segment(monkeypatch) -> None:
    """Field 2026-11-27: S6 Bathroom → Corridor with `returning_home` between
    the tasks. Each room on its own was too short; together they teach."""
    t0 = c.dt_util.utcnow()
    now = [t0]
    co = _co(monkeypatch, now)

    def at(sec, **d):
        now[0] = t0 + timedelta(seconds=sec)
        co._note_battery(_dev(**d))

    level = 90
    for s in range(0, 150, 30):          # room 1: 90 -> 89 (one change only)
        at(s, battery=level - (1 if s >= 90 else 0))
    at(160, battery=89, status_state="returning_home")
    at(200, battery=89, status_state="returning_home")
    for s in range(230, 450, 30):        # room 2: seen 88 at 260, 87 at 350, 86 at 440
        lv = 89 - (1 if s >= 240 else 0) - (1 if s >= 330 else 0) - (1 if s >= 420 else 0)
        at(s, battery=lv)
    at(460, battery=86, in_cleaning=False, status_state="charging")
    # Edges 89 @ 90 s .. 86 @ 440 s: 3 % in 350 s, across the drive.
    assert co._battery_learned["s6"]["drain"]["dry"] == pytest.approx(3 / (350 / 60), abs=0.005)


def test_an_open_segment_survives_a_restart() -> None:
    t0 = c.dt_util.utcnow()
    seg = bm.Segment("wet", "off", t0, 90.0, t0 + timedelta(minutes=4), 88.0,
                     [(t0 + timedelta(minutes=1), 89.0), (t0 + timedelta(minutes=4), 88.0)])
    back = bm.seg_from_dict(bm.seg_to_dict(seg))
    assert back == seg
    assert bm.seg_from_dict({"mode": "dry"}) is None
    co = object.__new__(c.AnyVacCoordinator)
    co._battery_learned = {"s8": {"drain": {"wet": 0.3}}}
    co._bat_seg = {"s8": seg}
    payload = co._battery_payload()
    assert payload["s8"] == {"drain": {"wet": 0.3}}
    assert bm.seg_from_dict(payload["__open"]["s8"]) == seg


def test_vacuuming_with_the_mop_down_counts_as_dry() -> None:
    """Field 2026-11-27: S7 vacuum-and-mop at max+ drained 0.79 %/min, S8
    mop-only 0.29 — the suction decides, not the water."""
    assert bm.kind_of("wet", False, True) == "dry"
    assert bm.kind_of("wet", False, False) == "wet"
    assert bm.kind_of("wet", True, False) == "dry"
    assert bm.kind_of("dry", False, None) == "dry"
    m = bm.Model(drain={"dry@max_plus": 0.79, "wet": 0.29})
    assert m.drain_rate("wet", "max_plus") == 0.79
    assert m.drain_rate("wet", "off") == 0.29


def test_old_wet_samples_with_the_suction_on_are_replayed_as_dry() -> None:
    learned = {
        "drain": {"dry": 0.789, "dry@max_plus": 0.789, "wet": 0.788, "wet@max_plus": 0.788},
        "samples": [
            {"at": "2026-11-27T09:23:00+00:00", "kind": "dry", "setting": "max_plus", "rate": 0.789},
            {"at": "2026-11-27T10:40:00+00:00", "kind": "wet", "setting": "max_plus", "rate": 0.788},
            {"at": "2026-11-27T11:00:00+00:00", "kind": "lo", "setting": None, "rate": 0.8},
        ],
    }
    assert bm.reclassify(learned) is True
    assert learned["drain"] == {"dry": 0.789, "dry@max_plus": 0.789}
    assert [s["kind"] for s in learned["samples"]] == ["dry", "dry", "lo"]
    assert bm.reclassify(learned) is False
    s8 = {"drain": {"wet": 0.292, "wet@off": 0.292},
          "samples": [{"at": "x", "kind": "wet", "setting": "off", "rate": 0.292}]}
    assert bm.reclassify(s8) is False
