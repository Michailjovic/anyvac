"""docs/51 §2 — reach mask: per-room swept footprint of the last complete clean."""

from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from custom_components.anyvac import coverage as covmod
from custom_components.anyvac import reach as reachmod
from custom_components.anyvac.coordinator import AnyVacCoordinator
from custom_components.anyvac.sensor import AnyVacMapSensor

K = covmod.key


def _cells(x0: int, y0: int, w: int, h: int) -> set[int]:
    return {K(x, y) for x in range(x0, x0 + w) for y in range(y0, y0 + h)}


# ── pure module ─────────────────────────────────────────────────────────────


def test_encode_decode_round_trip() -> None:
    cells = _cells(500, 400, 7, 3) | {K(510, 405)}
    rec = reachmod.encode_cells(cells)
    assert rec["x0"] == 500 and rec["y0"] == 400 and rec["w"] == 11 and rec["h"] == 6
    x0, y0, m = reachmod.decode_mask(rec)
    back = {K(x0 + x, y0 + y) for y, x in zip(*np.nonzero(m))}
    assert back == cells
    assert reachmod.encode_cells([]) is None
    assert reachmod.decode_mask({"x0": 0, "y0": 0, "w": 2, "h": 2, "bits": "!!"}) is None


def test_record_needs_80_percent_of_the_reachable_floor() -> None:
    reach = _cells(0, 0, 10, 10)
    almost = _cells(0, 0, 10, 7) | _cells(0, 7, 9, 1)  # 79 cells
    assert reachmod.make_record(almost, reach, segment_id=16, kinds=["dry"], map_index=0, at="t") is None
    enough = almost | {K(9, 7)}  # 80
    rec = reachmod.make_record(enough | _cells(10, 0, 3, 3), reach, segment_id=16, kinds=["wet", "dry"], map_index=0, at="t")
    assert rec["floor_pct"] == 80
    assert rec["kinds"] == ["dry", "wet"]
    assert rec["segment_id"] == 16 and rec["map_index"] == 0 and rec["at"] == "t"
    # footprint spilling outside the reachable floor is kept (where the body was)
    x0, y0, m = reachmod.decode_mask(rec)
    assert (x0, y0, m.shape) == (0, 0, (8, 13))


def test_stale_when_the_map_was_redrawn() -> None:
    rec = {"segment_id": 16, "map_index": 0}
    assert not reachmod.is_stale(rec, 16, 0)
    assert not reachmod.is_stale(rec, "16", None)  # unknown map index never stales
    assert reachmod.is_stale(rec, 17, 0)  # new segment id = map deleted and redrawn
    assert reachmod.is_stale(rec, None, 0)  # room gone
    assert reachmod.is_stale(rec, 16, 1)  # other map slot


def test_union_of_rooms() -> None:
    a = reachmod.encode_cells(_cells(0, 0, 2, 2))
    b = reachmod.encode_cells(_cells(5, 3, 1, 1))
    x0, y0, m = reachmod.union_mask([a, b, {"bad": 1}])
    assert (x0, y0, m.shape, int(m.sum())) == (0, 0, (4, 6), 5)
    assert reachmod.union_mask([]) is None


def test_render_lands_on_the_map_canvas_through_the_affine() -> None:
    # 50 mm per px, origin shifted: cell (ix, iy) -> px (ix - 100, iy - 200)
    aff = (1 / 50, 0.0, -100.0, 0.0, 1 / 50, -200.0)
    m = np.zeros((2, 3), dtype=bool)
    m[1, 2] = True  # cell (102, 201)
    rgba = reachmod.render_rgba(100, 200, m, aff, 10, 10)
    hit = np.argwhere(rgba[..., 3] > 0)
    assert hit.tolist() == [[1, 2]]
    assert tuple(rgba[1, 2]) == reachmod.REACH_COLOR


def test_render_follows_a_flipped_axis() -> None:
    # y flipped (Roborock maps grow upwards): px v = 9 - (iy - 200)
    aff = (1 / 50, 0.0, -100.0, 0.0, -1 / 50, 209.0 + 1.0)
    m = np.zeros((1, 1), dtype=bool)
    m[0, 0] = True  # cell (100, 200) -> mm y in [10000, 10050) -> v in (9, 10]
    rgba = reachmod.render_rgba(100, 200, m, aff, 4, 12)
    assert np.argwhere(rgba[..., 3] > 0).tolist() == [[9, 0]]


def test_render_nothing_or_degenerate() -> None:
    m = np.ones((2, 2), dtype=bool)
    assert reachmod.render_rgba(0, 0, m, (1 / 50, 0, -1000, 0, 1 / 50, -1000), 10, 10) is None
    assert reachmod.render_rgba(0, 0, m, (0, 0, 0, 0, 0, 0), 10, 10) is None


def test_png_crop() -> None:
    rgba = np.zeros((20, 30, 4), dtype=np.uint8)
    rgba[5, 6] = reachmod.REACH_COLOR
    with Image.open(io.BytesIO(reachmod.png_bytes(rgba, (4, 3, 14, 13)))) as im:
        assert im.size == (10, 10)
        assert im.getpixel((2, 2))[3] == 255


# ── coordinator ─────────────────────────────────────────────────────────────


def _coord() -> AnyVacCoordinator:
    c = object.__new__(AnyVacCoordinator)
    c._reach_ready()  # partial coordinator: no store
    c.async_update_listeners = lambda: None  # type: ignore[method-assign]
    return c


def _device(duid: str = "s8", rooms=None, map_index=0, calib=True):
    data = {
        "rooms": rooms if rooms is not None else [{"name": "Kitchen", "segment_id": 16}, {"name": "Hall", "segment_id": 17}],
        "debug_map": {"map_index": map_index},
    }
    if calib:
        # vacuum mm -> px: 50 mm per px, same as test above
        data["calibration_points"] = [
            {"vacuum": {"x": 0, "y": 0}, "map": {"x": 0, "y": 0}},
            {"vacuum": {"x": 5000, "y": 0}, "map": {"x": 100, "y": 0}},
            {"vacuum": {"x": 0, "y": 5000}, "map": {"x": 0, "y": 100}},
        ]
        data["image_dims"] = {"width": 100, "height": 100, "scale": 1}
    return SimpleNamespace(duid=duid, name="S8", data=data)


def _run(rooms: dict[str, set[int]], kind: str = "dry") -> covmod.RunTracker:
    run = covmod.RunTracker()
    for nm, cells in rooms.items():
        kr = run._kind(nm, kind)
        kr.cells |= cells
        run.activated.append(nm)
    return run


def _geo() -> covmod.Geometry:
    return covmod.Geometry({
        "Kitchen": covmod.RoomGeom("Kitchen", 16, frozenset(_cells(10, 10, 10, 10))),
        "Hall": covmod.RoomGeom("Hall", 17, frozenset(_cells(30, 10, 10, 10))),
    })


def test_finished_run_replaces_only_rooms_it_swept_enough_of() -> None:
    c, dev, geo = _coord(), _device(), _geo()
    assert c._record_reach(dev, _run({"Kitchen": _cells(10, 10, 10, 10), "Hall": _cells(30, 10, 10, 9)}), geo)
    first = dict(c._reach_masks["s8"])
    assert set(first) == {"Kitchen", "Hall"}
    # a cancelled clean of the kitchen (half the floor) keeps the good mask …
    assert not c._record_reach(dev, _run({"Kitchen": _cells(10, 10, 10, 5)}), geo)
    assert c._reach_masks["s8"]["Kitchen"] == first["Kitchen"]
    # … a complete one with furniture moved replaces it, the hall stays as it was
    moved = _cells(10, 10, 10, 10) - _cells(12, 12, 2, 1)
    assert c._record_reach(dev, _run({"Kitchen": moved}), geo)
    assert c._reach_masks["s8"]["Kitchen"] != first["Kitchen"]
    assert c._reach_masks["s8"]["Hall"] == first["Hall"]
    assert c._reach_rev["s8"] == 2


def test_wet_only_robot_records_its_mop_footprint() -> None:
    c = _coord()
    assert c._record_reach(_device(), _run({"Kitchen": _cells(10, 10, 10, 10)}, kind="wet"), _geo())
    assert c._reach_masks["s8"]["Kitchen"]["kinds"] == ["wet"]


def test_records_of_a_redrawn_map_are_stale_and_not_drawn() -> None:
    c, geo = _coord(), _geo()
    c._record_reach(_device(), _run({"Kitchen": _cells(10, 10, 10, 10), "Hall": _cells(30, 10, 10, 10)}), geo)
    c.data = {"s8": _device(rooms=[{"name": "Kitchen", "segment_id": 21}, {"name": "Hall", "segment_id": 17}])}
    live, meta = c._reach_current(c.data["s8"])
    assert meta["Kitchen"]["stale"] is True and meta["Hall"]["stale"] is False
    assert len(live) == 1
    x0, y0, mask, aff, w, h = c.reach_render_args("s8")
    assert (x0, y0, int(mask.sum()), w, h) == (30, 10, 100, 100, 100)


def test_render_args_need_calibration_and_records() -> None:
    c = _coord()
    c.data = {"s8": _device(calib=False)}
    c._record_reach(c.data["s8"], _run({"Kitchen": _cells(10, 10, 10, 10)}), _geo())
    assert c.reach_render_args("s8") is None
    c.data = {"s8": _device()}
    assert c.reach_render_args("s8") is not None
    assert c.reach_render_args("nope") is None


def test_reset_reach_by_room_and_all() -> None:
    c, geo = _coord(), _geo()
    c._record_reach(_device(), _run({"Kitchen": _cells(10, 10, 10, 10), "Hall": _cells(30, 10, 10, 10)}), geo)
    c._record_reach(_device("s6"), _run({"Kitchen": _cells(10, 10, 10, 10)}), geo)
    c.reset_reach(duid="s8", room="Kitchen")
    assert set(c._reach_masks["s8"]) == {"Hall"} and "s6" in c._reach_masks
    c.reset_reach()
    assert c._reach_masks == {}


@pytest.mark.asyncio
async def test_publish_renders_once_per_change(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _coord()
    dev = _device()
    c.data = {"s8": dev}
    tasks: list = []

    async def _exec(fn, *a):
        return fn(*a)

    c.hass = SimpleNamespace(async_add_executor_job=_exec, async_create_task=lambda coro: tasks.append(coro))
    monkeypatch.setattr(c, "_reach_entity_id", lambda duid: "image.s8_reach_mask")
    c._reach_publish(dev)
    assert dev.data["reach"] == {"entity_id": "image.s8_reach_mask", "updated": None, "rooms": {}}
    assert tasks == []  # nothing recorded yet
    c._record_reach(dev, _run({"Kitchen": _cells(10, 10, 10, 10)}), _geo())
    c._reach_publish(dev)
    assert len(tasks) == 1
    await tasks.pop()
    png = c.reach_png("s8")
    with Image.open(io.BytesIO(png)) as im:
        assert im.size == (100, 100)
        assert im.getpixel((15, 15))[3] == 255 and im.getpixel((50, 50))[3] == 0
    assert c.reach_updated("s8") is not None
    c._reach_publish(dev)
    assert tasks == []  # unchanged inputs -> no re-render
    assert dev.data["reach"]["rooms"]["Kitchen"]["floor_pct"] == 100


def test_reach_meta_is_unrecorded() -> None:
    assert "reach" in AnyVacMapSensor._unrecorded_attributes


@pytest.mark.asyncio
async def test_image_entity_serves_the_coordinators_png(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.anyvac.image import AnyVacReachImage

    c = _coord()
    c._reach_png["s8"] = b"png"
    c.hass = SimpleNamespace()
    # ImageEntity only needs hass for its (unused here) URL client
    monkeypatch.setattr("homeassistant.components.image.entity.get_async_client", lambda *a, **k: None)
    ent = AnyVacReachImage(c, "s8", "S8")
    assert ent.unique_id == "s8_anyvac_reach"
    assert ent.name == "S8 reach mask"
    assert await ent.async_image() == b"png"
