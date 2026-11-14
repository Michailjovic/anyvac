"""`clean_type_trace` (1.51.6, plan po auditu A0).

A diagnostic capture of the full polls around a clean's falling edge, so the
field report "a dry S7 MaxV run is announced as wet" can be pinned to one of the
two paths the coordinator simulation found (no room name during the run, or
in_cleaning still set while the water mode is already reset). These tests pin
the capture itself — window, freeze, and that the edge poll's
``event_clean_type`` is exactly what the finish events carried — not the bug.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import custom_components.anyvac.coordinator as coordinator_mod
from tests.test_coordinator_pipeline import HALL, _Clock, _device, _new_coordinator, _poll

DOMAIN = coordinator_mod.DOMAIN


def _rig(monkeypatch: pytest.MonkeyPatch):
    clock = _Clock(datetime(2026, 11, 10, 12, 0, tzinfo=UTC))
    coord = _new_coordinator(monkeypatch, clock)
    coord._ct_trace_buf = {}
    coord._ct_trace_prev = {}
    coord._ct_trace_tail = {}
    coord._ct_trace_last = {}
    coord._ct_trace_events = {}
    # Route the fake bus through the coordinator's own event recorder, the way
    # the real `hass.bus.async_listen` registration in __init__ does.
    fire = coord.hass.bus.async_fire

    def _fire(event_type: str, data: dict) -> None:
        fire(event_type, data)
        if event_type.removeprefix(f"{DOMAIN}_") in ("clean_started", "clean_finished", "run_finished"):
            coord._ct_trace_on_event(SimpleNamespace(event_type=event_type, data=data))

    coord.hass.bus.async_fire = _fire
    return coord, clock


def _step(coord, clock, **kw) -> None:
    device = _device("s7", **kw)
    before = coord._session_clean_type.get("s7")
    _poll(coord, device)
    coord._record_clean_type_trace(device, before)
    clock.advance(seconds=30)


IDLE = dict(in_cleaning=False, vacuuming=False, clean_type="wet")


def test_idle_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    coord, clock = _rig(monkeypatch)
    for _ in range(5):
        _step(coord, clock, **IDLE)
    assert coord._clean_type_trace("s7") is None


def test_window_freezes_after_the_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    coord, clock = _rig(monkeypatch)
    _step(coord, clock, **IDLE)
    # A long dry run: more cleaning polls than the window keeps.
    n_run = coordinator_mod._CT_TRACE_BEFORE + 5
    for i in range(1, n_run + 1):
        _step(coord, clock, clean_type="dry", vacuum_room_name="Hall",
              _path_dry=HALL[: max(1, len(HALL) * i // n_run)])
    # Edge + the after-polls; the trace freezes on the last of them.
    for _ in range(1 + coordinator_mod._CT_TRACE_AFTER):
        assert "polls" not in (coord._clean_type_trace("s7") or {})
        _step(coord, clock, in_cleaning=False, vacuuming=False, clean_type="wet", _path_dry=HALL)
    trace = coord._clean_type_trace("s7")
    polls = trace["polls"]
    assert len(polls) == coordinator_mod._CT_TRACE_BEFORE + 1 + coordinator_mod._CT_TRACE_AFTER
    edge = [p for p in polls if p["edge"]]
    assert len(edge) == 1 and trace["edge_at"] == edge[0]["at"]
    assert polls.index(edge[0]) == coordinator_mod._CT_TRACE_BEFORE
    assert all(p["in_cleaning"] for p in polls[: coordinator_mod._CT_TRACE_BEFORE])
    # Later idle polls do not touch the frozen trace.
    _step(coord, clock, **IDLE)
    assert coord._clean_type_trace("s7")["polls"] == polls


ACTIVE = dict(status_state="segment_cleaning")


@pytest.mark.parametrize(
    "run_polls, expected",
    [
        # S1: the room is known during the run.
        ([dict(clean_type="dry", vacuum_room_name="Hall", _path_dry=HALL, **ACTIVE)] * 2, "dry"),
        # S2 (docs/49): no room name during the run. Used to announce "wet" (the
        # edge poll's reset water mode won); the knob is now captured in active
        # cleaning regardless of the room name.
        ([dict(clean_type="dry", _path_dry=HALL, **ACTIVE)] * 2, "dry"),
    ],
)
def test_finish_event_keeps_the_dry_verdict_after_the_knob_reset(
    monkeypatch: pytest.MonkeyPatch, run_polls, expected
) -> None:
    coord, clock = _rig(monkeypatch)
    _step(coord, clock, **IDLE)
    for kw in run_polls:
        _step(coord, clock, **kw)
    for _ in range(1 + coordinator_mod._CT_TRACE_AFTER):
        _step(coord, clock, in_cleaning=False, vacuuming=False, clean_type="wet", _path_dry=HALL)
    trace = coord._clean_type_trace("s7")
    finished = [e for e in trace["events"] if e["event"] == "run_finished"]
    assert finished and finished[-1]["clean_type"] == expected


def test_events_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    coord, _clock = _rig(monkeypatch)
    for i in range(coordinator_mod._CT_TRACE_EVENTS + 4):
        coord._ct_trace_on_event(SimpleNamespace(
            event_type=f"{DOMAIN}_clean_finished", data={"duid": "s7", "clean_type": str(i)}))
    events = coord._clean_type_trace("s7")["events"]
    assert len(events) == coordinator_mod._CT_TRACE_EVENTS
    assert events[-1]["clean_type"] == str(coordinator_mod._CT_TRACE_EVENTS + 3)
    assert isinstance(coord._ct_trace_events["s7"], deque)
