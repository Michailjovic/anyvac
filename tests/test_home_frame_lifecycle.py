"""Tests for docs/40 Fáze 1's coordinator-level home-frame lifecycle (prompt
§4.5 test categories 1 "dekodér+self-test", 2 "registrace", 3 "frame
lifecycle", 6 "cache key", and 7 "home_px_to_mm/mm_to_home_px round trip").

`_compute_home_frame_result` (the pure, executor-safe registration step) is
exercised directly with synthetic raw map blobs (`_synthetic_raw_map.py`) —
no real device dumps needed, so this suite never depends on files under
`samples/`. `_maybe_register_home_frame`'s cache-key short-circuit and
`_async_run_home_frame_registration`'s apply-back-on-the-event-loop step are
covered against a `_bare_coordinator` mirroring `test_homeframe_persistence.
py`'s own pattern.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.anyvac.coordinator import (
    AnyVacCoordinator,
    AnyVacDevice,
    _apply_home_frame_contract,
    _compute_home_frame_result,
)

from ._synthetic_raw_map import FakeLibRoom, lib_rooms_for, make_raw_map

NOW = "2026-09-13T00:00:00+00:00"


# ── 1) decode + self-test ──────────────────────────────────────────────────


def test_grid_self_test_passes_against_matching_lib_rooms() -> None:
    rooms = {1: (10, 10, 40, 40)}
    raw = make_raw_map(60, 60, top=5, left=3, rooms=rooms)
    lib_rooms = lib_rooms_for(rooms, top=5, left=3)
    res = _compute_home_frame_result({}, {}, "s7", raw, 1, 1, "sha1", lib_rooms, NOW)
    assert res["disabled"] is False
    assert res["status"] == "reference"


def test_grid_self_test_mismatch_disables_registration() -> None:
    rooms = {1: (10, 10, 40, 40)}
    raw = make_raw_map(60, 60, top=5, left=3, rooms=rooms)
    bad_lib_rooms = {1: FakeLibRoom(999999, 999999, 999999, 999999)}
    res = _compute_home_frame_result({}, {}, "s7", raw, 1, 1, "sha1", bad_lib_rooms, NOW)
    assert res["disabled"] is True
    assert res["status"] == "disabled"
    assert res["robot_frame"] is None


def test_decode_error_never_raises_just_reports() -> None:
    res = _compute_home_frame_result({}, {}, "s7", b"\x00\x01", None, None, "sha1", {}, NOW)
    assert res["disabled"] is False
    assert res["error"] is not None
    assert res["robot_frame"] is None


# ── 2) registration (rotation / gate threshold) ────────────────────────────


def _rotated_90_rooms(
    rooms: dict[int, tuple[int, int, int, int]], src_w: int
) -> dict[int, tuple[int, int, int, int]]:
    """The `np.rot90(k=1)` transform of every room rect, for building a
    second robot's raw map whose floor mask is a TRUE 90° rotation of the
    first's — exactly what `register()` was validated against in Fáze 0."""
    out = {}
    for seg_id, (x0, y0, x1, y1) in rooms.items():
        out[seg_id] = (y0, src_w - x1, y1, src_w - x0)
    return out


def test_second_robot_rotated_duplicate_registers_aligned() -> None:
    rooms_a = {1: (10, 10, 40, 40), 2: (50, 10, 90, 60), 3: (10, 50, 40, 90)}
    raw_a = make_raw_map(120, 100, top=5, left=3, rooms=rooms_a)
    lib_a = lib_rooms_for(rooms_a, top=5, left=3)
    res1 = _compute_home_frame_result({}, {}, "s7", raw_a, 1, 1, "sha_a", lib_a, NOW)
    frames = {res1["robot_frame"]: res1["updated_frames"][res1["robot_frame"]]}
    robot_frame = {"s7": res1["robot_frame"]}

    rooms_b = _rotated_90_rooms(rooms_a, src_w=120)
    raw_b = make_raw_map(100, 120, top=50, left=-20, rooms=rooms_b)
    lib_b = lib_rooms_for(rooms_b, top=50, left=-20)
    res2 = _compute_home_frame_result(frames, robot_frame, "s6", raw_b, 1, 1, "sha_b", lib_b, NOW)

    assert res2["status"] == "aligned"
    assert round(res2["rot_deg"]) in (90, 270)
    assert res2["score"] >= 0.6 and res2["iou"] >= 0.4
    assert res2["robot_frame"] == res1["robot_frame"]  # same frame, not a new one


def test_unrelated_floor_gets_its_own_frame_unaligned() -> None:
    rooms_a = {1: (10, 10, 40, 40), 2: (50, 10, 90, 60), 3: (10, 50, 40, 90)}
    raw_a = make_raw_map(120, 100, top=5, left=3, rooms=rooms_a)
    lib_a = lib_rooms_for(rooms_a, top=5, left=3)
    res1 = _compute_home_frame_result({}, {}, "s7", raw_a, 1, 1, "sha_a", lib_a, NOW)
    frames = {res1["robot_frame"]: res1["updated_frames"][res1["robot_frame"]]}
    robot_frame = {"s7": res1["robot_frame"]}

    rooms_c = {1: (5, 5, 15, 15)}
    raw_c = make_raw_map(30, 30, top=5000, left=5000, rooms=rooms_c)
    lib_c = lib_rooms_for(rooms_c, top=5000, left=5000)
    res3 = _compute_home_frame_result(frames, robot_frame, "s8", raw_c, 1, 1, "sha_c", lib_c, NOW)

    assert res3["status"] == "unaligned"
    assert res3["robot_frame"] != res1["robot_frame"]
    new_frame = res3["updated_frames"][res3["robot_frame"]]
    assert new_frame["robots"]["s8"]["method"] == "reference"


def test_first_robot_ever_is_reference_not_unaligned() -> None:
    """An empty `frames_snapshot` is the ONE case a brand-new frame's founder
    is published as `reference`, not `unaligned` — docs/40 §4.2 point 2."""
    rooms = {1: (10, 10, 20, 20)}
    raw = make_raw_map(30, 30, top=0, left=0, rooms=rooms)
    lib = lib_rooms_for(rooms, top=0, left=0)
    res = _compute_home_frame_result({}, {}, "s7", raw, 1, 1, "sha", lib, NOW)
    assert res["status"] == "reference"


# ── 3) frame lifecycle: continuity remap (grow, no shift) + founder-remap failure ─


def test_founder_remap_with_new_exploration_stays_reference_and_merges() -> None:
    rooms_a = {1: (10, 10, 40, 40), 2: (50, 10, 90, 60)}
    raw_a = make_raw_map(120, 100, top=5, left=3, map_index=1, rooms=rooms_a)
    lib_a = lib_rooms_for(rooms_a, top=5, left=3)
    res1 = _compute_home_frame_result({}, {}, "s7", raw_a, 1, 1, "sha1", lib_a, NOW)
    frames = {res1["robot_frame"]: res1["updated_frames"][res1["robot_frame"]]}
    robot_frame = {"s7": res1["robot_frame"]}

    rooms_a2 = dict(rooms_a)
    rooms_a2[3] = (10, 50, 40, 90)  # a newly-explored room, same canvas
    raw_a2 = make_raw_map(120, 100, top=5, left=3, map_index=2, rooms=rooms_a2)
    lib_a2 = lib_rooms_for(rooms_a2, top=5, left=3)
    res2 = _compute_home_frame_result(frames, robot_frame, "s7", raw_a2, 2, 1, "sha2", lib_a2, NOW)

    assert res2["status"] == "reference"
    assert res2["robot_frame"] == res1["robot_frame"]
    assert res2["origin_shifted"] is False
    merged = res2["updated_frames"][res2["robot_frame"]]
    assert merged["robots"]["s7"]["method"] == "reference"


def test_founder_remap_that_fails_gate_creates_new_frame_and_flags_old_stale() -> None:
    rooms_a = {1: (10, 10, 40, 40), 2: (50, 10, 90, 60)}
    raw_a = make_raw_map(120, 100, top=5, left=3, rooms=rooms_a)
    lib_a = lib_rooms_for(rooms_a, top=5, left=3)
    res1 = _compute_home_frame_result({}, {}, "s7", raw_a, 1, 1, "sha1", lib_a, NOW)
    frames = {res1["robot_frame"]: res1["updated_frames"][res1["robot_frame"]]}
    robot_frame = {"s7": res1["robot_frame"]}

    # A deliberately huge, uniformly-floor "different apartment": `covered`
    # is bounded by (old frame's floor cells) / (this mask's own huge floor
    # cell count), so it stays well under the 0.6 gate for any shift/rotation
    # the search tries — a small mask can coincidentally fully-contain inside
    # a sparse existing frame and pass by fluke (see this file's git history).
    rooms_foreign = {1: (1, 1, 298, 298)}
    raw_foreign = make_raw_map(300, 300, top=9000, left=9000, map_index=2, rooms=rooms_foreign)
    lib_foreign = lib_rooms_for(rooms_foreign, top=9000, left=9000)
    res2 = _compute_home_frame_result(
        frames, robot_frame, "s7", raw_foreign, 2, 1, "sha2", lib_foreign, NOW
    )

    assert res2["robot_frame"] != res1["robot_frame"]
    assert res2["stale_frame_id"] == res1["robot_frame"]
    assert res2["status"] == "unaligned"


# ── 6) cache key (unchanged map -> no recompute) ───────────────────────────


class _FakeBus:
    def async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        pass


class _FakeHass:
    def __init__(self) -> None:
        self.bus = _FakeBus()
        self.created_tasks: list[Any] = []

    def async_create_task(self, coro: Any) -> None:
        self.created_tasks.append(coro)
        coro.close()  # never actually run in these cache-key-only tests


def _bare_coordinator() -> AnyVacCoordinator:
    coord = object.__new__(AnyVacCoordinator)
    coord.hass = _FakeHass()
    coord._home_frame_disabled = set()
    coord._home_frame_inflight = set()
    coord._home_frame_cache_key = {}
    return coord


def _device_with_debug(duid: str, map_index: Any, map_sequence: Any, raw_sha1: str) -> AnyVacDevice:
    return AnyVacDevice(
        duid=duid,
        slug=duid,
        name=duid,
        data={
            "debug_map": {"map_index": map_index, "map_sequence": map_sequence, "raw_sha1": raw_sha1},
            "_lib_rooms": {},
        },
    )


def test_unchanged_cache_key_never_dispatches_a_registration_job() -> None:
    coord = _bare_coordinator()
    coord._home_frame_cache_key["s7"] = (1, 1, "sha1")
    device = _device_with_debug("s7", 1, 1, "sha1")
    coord._maybe_register_home_frame(device)
    assert coord.hass.created_tasks == []


def test_changed_cache_key_dispatches_a_registration_job(monkeypatch: pytest.MonkeyPatch) -> None:
    coord = _bare_coordinator()
    coord._home_frame_cache_key["s7"] = (1, 1, "sha1")
    monkeypatch.setattr(coord, "raw_map_for", lambda duid: (b"rawbytes", {}))
    device = _device_with_debug("s7", 1, 2, "sha2")
    coord._maybe_register_home_frame(device)
    assert len(coord.hass.created_tasks) == 1


def test_disabled_duid_never_dispatches_even_on_a_changed_key(monkeypatch: pytest.MonkeyPatch) -> None:
    coord = _bare_coordinator()
    coord._home_frame_disabled.add("s7")
    monkeypatch.setattr(coord, "raw_map_for", lambda duid: (b"rawbytes", {}))
    device = _device_with_debug("s7", 1, 2, "sha2")
    coord._maybe_register_home_frame(device)
    assert coord.hass.created_tasks == []


def test_inflight_duid_is_not_dispatched_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    coord = _bare_coordinator()
    coord._home_frame_inflight.add("s7")
    monkeypatch.setattr(coord, "raw_map_for", lambda duid: (b"rawbytes", {}))
    device = _device_with_debug("s7", 1, 2, "sha2")
    coord._maybe_register_home_frame(device)
    assert coord.hass.created_tasks == []


# ── 7) home_px_to_mm / mm_to_home_px round trip (coordinator methods) ──────


def _coordinator_with_aligned_robot() -> AnyVacCoordinator:
    """A `_bare_coordinator` seeded with one frame + one robot registered at
    a 90° rotation, via the SAME `homeframe.*` pipeline the async job uses —
    not a hand-rolled shortcut, so this exercises the real published
    geometry."""
    from custom_components.anyvac import homeframe

    rooms_a = {1: (10, 10, 40, 40), 2: (50, 10, 90, 60)}
    raw_a = make_raw_map(120, 100, top=5, left=3, rooms=rooms_a)
    lib_a = lib_rooms_for(rooms_a, top=5, left=3)
    res1 = _compute_home_frame_result({}, {}, "s7", raw_a, 1, 1, "sha_a", lib_a, NOW)
    frames = {res1["robot_frame"]: res1["updated_frames"][res1["robot_frame"]]}
    robot_frame = {"s7": res1["robot_frame"]}

    rooms_b = _rotated_90_rooms(rooms_a, src_w=120)
    raw_b = make_raw_map(100, 120, top=50, left=-20, rooms=rooms_b)
    lib_b = lib_rooms_for(rooms_b, top=50, left=-20)
    res2 = _compute_home_frame_result(frames, robot_frame, "s6", raw_b, 1, 1, "sha_b", lib_b, NOW)
    frames[res2["robot_frame"]] = res2["updated_frames"][res2["robot_frame"]]
    robot_frame["s6"] = res2["robot_frame"]

    coord = object.__new__(AnyVacCoordinator)
    coord._home_frames = frames
    coord._robot_frame = robot_frame
    return coord


def test_home_px_to_mm_round_trips_for_a_rotated_aligned_robot() -> None:
    coord = _coordinator_with_aligned_robot()
    x_mm, y_mm = 1234.0, -567.0
    px = coord.mm_to_home_px("s6", x_mm, y_mm)
    assert px is not None
    back = coord.home_px_to_mm("s6", px[0], px[1])
    assert back is not None
    assert back[0] == pytest.approx(x_mm, abs=1e-6)
    assert back[1] == pytest.approx(y_mm, abs=1e-6)


def test_home_px_to_mm_is_none_for_an_unknown_duid() -> None:
    coord = _coordinator_with_aligned_robot()
    assert coord.home_px_to_mm("ghost", 0, 0) is None
    assert coord.mm_to_home_px("ghost", 0, 0) is None


# ── end-to-end: _async_run_home_frame_registration applies + debounce-saves ─


class _FakeAsyncHass(_FakeHass):
    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


class _FakeStore:
    def __init__(self) -> None:
        self.saved: Any = None

    async def async_load(self) -> Any:
        return self.saved

    def async_delay_save(self, get_data: Any, delay: float) -> None:
        self.saved = get_data()


def _async_bare_coordinator() -> AnyVacCoordinator:
    coord = object.__new__(AnyVacCoordinator)
    coord.hass = _FakeAsyncHass()
    coord._home_frames = {}
    coord._robot_frame = {}
    coord._home_frame_room_masks = {}
    coord._home_frame_cache_key = {}
    coord._home_frame_disabled = set()
    coord._home_frame_inflight = set()
    coord._home_frame_store = _FakeStore()
    coord._listeners = {}
    return coord


@pytest.mark.asyncio
async def test_async_run_registration_applies_first_robot_and_debounce_saves() -> None:
    coord = _async_bare_coordinator()
    rooms = {1: (10, 10, 40, 40)}
    raw = make_raw_map(60, 60, top=5, left=3, rooms=rooms)
    lib_rooms = lib_rooms_for(rooms, top=5, left=3)

    await coord._async_run_home_frame_registration("s7", raw, (1, 1, "sha1"), lib_rooms)

    assert "s7" not in coord._home_frame_inflight
    assert coord._home_frame_cache_key["s7"] == (1, 1, "sha1")
    frame_id = coord._robot_frame["s7"]
    assert frame_id in coord._home_frames
    assert coord._home_frames[frame_id]["robots"]["s7"]["method"] == "reference"
    # A debounced save must actually have happened, through the real
    # `_home_frame_for_save`/`homeframe.frames_to_storage` path (docs/14
    # rule 1 — no second serialisation here).
    assert coord._home_frame_store.saved is not None
    assert set(coord._home_frame_store.saved["frames"]) == {frame_id}


# ── kontrakt v3 publication (`_apply_home_frame_contract`) ─────────────────


def _registered_two_robot_setup() -> tuple[
    dict[str, dict[str, Any]], dict[str, str], dict[str, dict[str, dict[int, Any]]]
]:
    """s7 founds a frame with rooms {1: shared, 3: s7-only}; s6 registers
    aligned (90° rotated) with rooms {1: the SAME physical room as s7's 1,
    2: an s6-only room} — set up to exercise every `home_room_id` case:
    shared, reference-only, and non-reference-only."""
    rooms_a = {1: (10, 10, 40, 40), 3: (10, 50, 40, 90)}
    raw_a = make_raw_map(120, 100, top=5, left=3, rooms=rooms_a)
    lib_a = lib_rooms_for(rooms_a, top=5, left=3)
    res1 = _compute_home_frame_result({}, {}, "s7", raw_a, 1, 1, "sha_a", lib_a, NOW)
    frames = {res1["robot_frame"]: res1["updated_frames"][res1["robot_frame"]]}
    robot_frame = {"s7": res1["robot_frame"]}
    room_masks = {res1["robot_frame"]: {"s7": res1["segment_masks"]}}

    rooms_b = _rotated_90_rooms(rooms_a, src_w=120)
    rooms_b[2] = (85, 5, 95, 15)  # an s6-only room, far from the shared one
    raw_b = make_raw_map(100, 120, top=50, left=-20, rooms=rooms_b)
    lib_b = lib_rooms_for(rooms_b, top=50, left=-20)
    res2 = _compute_home_frame_result(frames, robot_frame, "s6", raw_b, 1, 1, "sha_b", lib_b, NOW)
    frames[res2["robot_frame"]] = res2["updated_frames"][res2["robot_frame"]]
    robot_frame["s6"] = res2["robot_frame"]
    room_masks[res2["robot_frame"]]["s6"] = res2["segment_masks"]

    return frames, robot_frame, room_masks


def test_apply_home_frame_contract_populates_home_frame_and_registration() -> None:
    frames, robot_frame, room_masks = _registered_two_robot_setup()
    device = AnyVacDevice(
        duid="s7", slug="s7", name="s7",
        data={"vacuum_position": {"x": 100.0, "y": 200.0}, "charger": None, "rooms": []},
    )
    _apply_home_frame_contract(device, [], [], frames, robot_frame, room_masks)

    assert device.data["home_frame"]["id"] == robot_frame["s7"]
    assert device.data["registration"]["status"] == "reference"
    assert device.data["registration"]["rotation_deg"] == 0.0
    assert device.data["vacuum_position_home_px"] is not None
    assert device.data["charger_home_px"] is None  # charger was None on the input


def test_apply_home_frame_contract_is_none_for_unregistered_duid() -> None:
    frames, robot_frame, room_masks = _registered_two_robot_setup()
    device = AnyVacDevice(duid="ghost", slug="ghost", name="ghost", data={"rooms": []})
    _apply_home_frame_contract(device, [], [], frames, robot_frame, room_masks)

    assert device.data["home_frame"] is None
    assert device.data["registration"] is None
    assert device.data["vacuum_position_home_px"] is None
    assert device.data["path_dry_home_px"] == []


def test_apply_home_frame_contract_shared_room_gets_same_home_room_id() -> None:
    frames, robot_frame, room_masks = _registered_two_robot_setup()

    dev_s7 = AnyVacDevice(
        duid="s7", slug="s7", name="s7",
        data={
            "vacuum_position": None, "charger": None,
            "rooms": [
                {"segment_id": 1, "x0": 500, "y0": 500, "x1": 2000, "y1": 2000},
                {"segment_id": 3, "x0": 500, "y0": 2500, "x1": 2000, "y1": 4500},
            ],
        },
    )
    dev_s6 = AnyVacDevice(
        duid="s6", slug="s6", name="s6",
        data={
            "vacuum_position": None, "charger": None,
            "rooms": [{"segment_id": 1, "x0": 500, "y0": 500, "x1": 2000, "y1": 2000}],
        },
    )
    _apply_home_frame_contract(dev_s7, [], [], frames, robot_frame, room_masks)
    _apply_home_frame_contract(dev_s6, [], [], frames, robot_frame, room_masks)

    s7_room1_id = dev_s7.data["rooms"][0]["home_room_id"]
    s7_room3_id = dev_s7.data["rooms"][1]["home_room_id"]
    s6_room1_id = dev_s6.data["rooms"][0]["home_room_id"]

    assert s7_room1_id is not None and s6_room1_id is not None
    assert s7_room1_id == s6_room1_id  # same physical room, seen by two robots
    assert s7_room3_id is not None
    assert s7_room3_id != s7_room1_id  # s7's OWN unshared room is a distinct id


def test_apply_home_frame_contract_room_outline_uses_frame_registered_mask() -> None:
    frames, robot_frame, room_masks = _registered_two_robot_setup()
    dev_s7 = AnyVacDevice(
        duid="s7", slug="s7", name="s7",
        data={
            "vacuum_position": None, "charger": None,
            "rooms": [{"segment_id": 1, "x0": 500, "y0": 500, "x1": 2000, "y1": 2000}],
        },
    )
    _apply_home_frame_contract(dev_s7, [], [], frames, robot_frame, room_masks)
    outline = dev_s7.data["rooms"][0]["outline_home_px"]
    assert outline is not None and len(outline) >= 4
    assert all(len(pt) == 2 for pt in outline)


@pytest.mark.asyncio
async def test_async_run_registration_disables_on_self_test_failure() -> None:
    coord = _async_bare_coordinator()
    rooms = {1: (10, 10, 40, 40)}
    raw = make_raw_map(60, 60, top=5, left=3, rooms=rooms)
    bad_lib_rooms = {1: FakeLibRoom(999999, 999999, 999999, 999999)}

    await coord._async_run_home_frame_registration("s7", raw, (1, 1, "sha1"), bad_lib_rooms)

    assert "s7" in coord._home_frame_disabled
    assert coord._robot_frame == {}
    assert coord._home_frame_store.saved is None  # never saved a disabled attempt
