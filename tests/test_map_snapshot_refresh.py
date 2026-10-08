"""Map-snapshot driven refresh (docs/47).

Two halves:

* The listener: AnyVac subscribes to every Roborock v1 coordinator and asks for
  a refresh only when that coordinator's raw map bytes changed — not on a bare
  status push (docs/47 §1.1).
* The pipeline: time attribution and the room-confirmation debounce count map
  SNAPSHOTS, not polls (docs/47 §1.3/§1.5). A poll on an unchanged map while
  the robot is actively cleaning defers its time to the next snapshot; in any
  other state (paused, ...) the time still drops out, as before.

Pipeline tests reuse the bare-coordinator helpers of
`test_coordinator_pipeline.py` and drive freshness through the real
`_note_snapshot`, exactly as `_async_update_data` does.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.anyvac.coordinator import AnyVacCoordinator

from .test_coordinator_pipeline import HALL, _Clock, _device, _new_coordinator, _poll


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


class _FakeRoborockCoordinator:
    """Just enough of a Roborock v1 coordinator: a duid, the map-content tree
    `_resolve_map_content` walks, and HA's listener API."""

    def __init__(self, duid: str, raw: bytes) -> None:
        self.duid = duid
        self._content = SimpleNamespace(raw_api_response=raw, map_data=None)
        home = SimpleNamespace(
            home_map_content={1: self._content},
            current_map_data=SimpleNamespace(map_flag=1),
        )
        self.properties_api = SimpleNamespace(home=home)
        self.listeners: list[Any] = []

    def set_raw(self, raw: bytes) -> None:
        self._content.raw_api_response = raw

    def async_add_listener(self, cb: Any) -> Any:
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)

    def notify(self) -> None:
        for cb in list(self.listeners):
            cb()


class _TaskHass:
    def __init__(self) -> None:
        self.tasks = 0

    def async_create_task(self, coro: Any) -> None:
        self.tasks += 1
        coro.close()


def _listener_coordinator() -> AnyVacCoordinator:
    coord = object.__new__(AnyVacCoordinator)
    coord.hass = _TaskHass()
    coord._rb_listeners = {}
    coord._rb_seen_raw = {}
    coord._map_fresh = {}

    async def _request_refresh() -> None:
        return None

    coord.async_request_refresh = _request_refresh  # type: ignore[method-assign]
    return coord


def test_subscribes_once_per_roborock_coordinator() -> None:
    coord = _listener_coordinator()
    rb = _FakeRoborockCoordinator("d1", b"map-a")
    coord._sync_roborock_listeners([rb])
    coord._sync_roborock_listeners([rb])
    assert len(rb.listeners) == 1


def test_refresh_only_when_the_map_changed() -> None:
    coord = _listener_coordinator()
    rb = _FakeRoborockCoordinator("d1", b"map-a")
    coord._sync_roborock_listeners([rb])
    coord._note_snapshot("d1", b"map-a")  # our poll already processed this map

    rb.notify()  # e.g. a status push: same map
    assert coord.hass.tasks == 0

    rb.set_raw(bytes(bytearray(b"map-a")))  # equal bytes, new object (python-roborock < 7.12)
    rb.notify()
    assert coord.hass.tasks == 0

    rb.set_raw(b"map-b")  # a new snapshot
    rb.notify()
    assert coord.hass.tasks == 1


def test_vanished_coordinator_is_unsubscribed() -> None:
    coord = _listener_coordinator()
    old = _FakeRoborockCoordinator("d1", b"map-a")
    new = _FakeRoborockCoordinator("d1", b"map-a")  # Roborock reload
    coord._sync_roborock_listeners([old])
    coord._sync_roborock_listeners([new])
    assert old.listeners == []
    assert len(new.listeners) == 1
    coord.unsubscribe_roborock()
    assert new.listeners == []
    assert coord._rb_listeners == {}


def test_note_snapshot_freshness() -> None:
    coord = _listener_coordinator()
    coord._note_snapshot("d1", b"a")
    assert coord._is_map_fresh("d1") is True
    coord._note_snapshot("d1", bytes(bytearray(b"a")))
    assert coord._is_map_fresh("d1") is False
    coord._note_snapshot("d1", b"b")
    assert coord._is_map_fresh("d1") is True
    coord._note_snapshot("d1", None)  # no raw bytes -> old behaviour
    assert coord._is_map_fresh("d1") is True


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> _Clock:
    from datetime import datetime, timezone

    return _Clock(datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc))


def _snapshot_coordinator(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> AnyVacCoordinator:
    coord = _new_coordinator(monkeypatch, clock)
    coord._rb_seen_raw = {}
    coord._map_fresh = {}
    return coord


def _snap_poll(coord: AnyVacCoordinator, raw: bytes, n: int, state: str, duid: str = "d1") -> None:
    coord._note_snapshot(duid, raw)
    _poll(
        coord,
        _device(duid, vacuum_room_name="Hall", target_segments=[1], _path_dry=HALL[:n],
                status_state=state),
    )


def test_stale_poll_while_cleaning_defers_its_time(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Our poll lands between two map snapshots while the robot cleans: its 20 s
    are not lost, the next snapshot gets the whole 35 s since the previous one."""
    coord = _snapshot_coordinator(monkeypatch, clock)
    _snap_poll(coord, b"m0", 12, "segment_cleaning")
    clock.advance(seconds=30)
    _snap_poll(coord, b"m1", 20, "segment_cleaning")
    assert coord._room_elapsed["d1"]["Hall"] == pytest.approx(30.0)

    clock.advance(seconds=20)
    _snap_poll(coord, bytes(bytearray(b"m1")), 20, "segment_cleaning")  # same snapshot
    assert coord._room_elapsed["d1"]["Hall"] == pytest.approx(30.0)

    clock.advance(seconds=15)
    _snap_poll(coord, b"m2", 28, "segment_cleaning")
    assert coord._room_elapsed["d1"]["Hall"] == pytest.approx(65.0)  # was 45 before docs/47


def test_stale_poll_while_paused_still_drops_the_pause(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    coord = _snapshot_coordinator(monkeypatch, clock)
    _snap_poll(coord, b"m0", 12, "segment_cleaning")
    clock.advance(seconds=30)
    _snap_poll(coord, b"m1", 20, "segment_cleaning")

    clock.advance(seconds=120)  # paused, the map did not change
    _snap_poll(coord, bytes(bytearray(b"m1")), 20, "paused")

    clock.advance(seconds=15)
    _snap_poll(coord, b"m2", 28, "segment_cleaning")
    assert coord._room_elapsed["d1"]["Hall"] == pytest.approx(45.0)  # pause not credited


def test_room_debounce_counts_snapshots_not_polls(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Two polls on the same snapshot must not confirm a room."""
    coord = _snapshot_coordinator(monkeypatch, clock)
    _snap_poll(coord, b"m0", 12, "segment_cleaning")
    clock.advance(seconds=20)
    _snap_poll(coord, bytes(bytearray(b"m0")), 12, "segment_cleaning")
    assert coord._confirmed_room.get("d1") is None

    clock.advance(seconds=15)
    _snap_poll(coord, b"m1", 20, "segment_cleaning")
    assert coord._confirmed_room["d1"] == "Hall"
