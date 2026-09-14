"""Tests for docs/40 §4.3 (Fáze 2.4) — cross-robot room pairing by
``home_room_id`` instead of by name.

The SAME physical room can have a different name in each robot's own app
(docs/30 §4b): today's ``assign()``/``build_tasks()`` match a `clean`/`plan`
room name against each robot's OWN name for that room only, so a robot that
calls it something else looks — wrongly — like it doesn't have the room at
all. Once a home frame has matched two robots' masks (Fáze 1), every
``rooms[]`` entry involved carries a shared ``home_room_id`` (docs/40 §4.1);
``CleanPlanner.__init__`` now also builds, alongside the existing per-duid
``segments`` map:

- ``home_room_owners``: ``{home_room_id: {duid: segment_id}}`` — every
  robot's OWN segment for the same physical room.
- ``home_room_id_by_name``: ``{name-as-typed-by-a-user: home_room_id}``.

``_duid_owns_room``/``_segment_for`` fall back to this pairing only when the
exact-name lookup misses, so a fleet with no home frame set up yet (no
``home_room_id`` anywhere) behaves byte-for-byte as before this change.

Like ``test_planner_pin.py``/``test_planner_pool_tasks.py``, most of these
build the planner via ``object.__new__`` to skip ``CleanPlanner.__init__``
(no real ``hass``/device registry needed) and set ``home_room_owners``/
``home_room_id_by_name`` directly — except the dedicated ``__init__`` tests
below, which construct a real ``CleanPlanner`` (with ``vacuum_entity_for_duid``
monkeypatched, since it needs a real entity registry) to lock down the
population logic itself.
"""

from __future__ import annotations

import pytest

from custom_components.anyvac.planner import CleanPlanner


class _FakeCoordinator:
    def __init__(self, room_sequence: dict | None = None, rooms_estimate: dict | None = None) -> None:
        self.room_sequence = room_sequence or {}
        self.rooms_estimate = rooms_estimate or {}
        self.data = None  # set by callers that go through real __init__


class _FakeDeviceData(dict):
    """`.data.get(...)` access, matching the real AnyVacDevice shape."""


class _FakeDevice:
    def __init__(self, rooms: list[dict] | None = None, mop_signal: dict | None = None) -> None:
        self.data = _FakeDeviceData(rooms=rooms or [], mop_signal=mop_signal)


def _planner(
    *,
    devices: dict[str, _FakeDevice],
    segments: dict[str, dict[str, int]],
    entity_of: dict[str, str],
    home_room_owners: dict[str, dict[str, int]] | None = None,
    home_room_id_by_name: dict[str, str] | None = None,
    rooms_estimate: dict | None = None,
) -> CleanPlanner:
    planner = object.__new__(CleanPlanner)
    planner.hass = None
    planner.coord = _FakeCoordinator(rooms_estimate=rooms_estimate or {})
    planner.devices = devices
    planner.segments = segments
    planner.entity_of = entity_of
    planner.duid_of_entity = {v: k for k, v in entity_of.items() if v}
    planner.home_room_owners = home_room_owners or {}
    planner.home_room_id_by_name = home_room_id_by_name or {}
    return planner


# ── CleanPlanner.__init__ population ─────────────────────────────────────────


def test_init_builds_home_room_owners_and_name_index_from_rooms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.anyvac.planner.vacuum_entity_for_duid",
        lambda hass, duid: f"vacuum.{duid}",
    )
    devices = {
        "d1": _FakeDevice(
            rooms=[{"name": "Bedroom", "segment_id": 1, "home_room_id": "R1"}]
        ),
        "w1": _FakeDevice(
            rooms=[{"name": "Loznice", "segment_id": 7, "home_room_id": "R1"}],
            mop_signal={"water_box_mode": 200},
        ),
    }
    coord = _FakeCoordinator()
    coord.data = devices
    planner = CleanPlanner(hass=None, coordinator=coord)

    assert planner.segments == {"d1": {"Bedroom": 1}, "w1": {"Loznice": 7}}
    assert planner.home_room_owners == {"R1": {"d1": 1, "w1": 7}}
    # First robot to publish a given name wins that name's entry — both names
    # here are distinct so both simply resolve to the same physical room.
    assert planner.home_room_id_by_name == {"Bedroom": "R1", "Loznice": "R1"}


def test_init_without_home_room_id_leaves_new_maps_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a fleet with no home frame set up yet (today's/legacy
    shape, no ``home_room_id`` key on any room) must populate `segments`
    exactly as before and leave the new maps empty — never raise."""
    monkeypatch.setattr(
        "custom_components.anyvac.planner.vacuum_entity_for_duid",
        lambda hass, duid: f"vacuum.{duid}",
    )
    devices = {
        "d1": _FakeDevice(rooms=[{"name": "Bedroom", "segment_id": 1}]),
    }
    coord = _FakeCoordinator()
    coord.data = devices
    planner = CleanPlanner(hass=None, coordinator=coord)

    assert planner.segments == {"d1": {"Bedroom": 1}}
    assert planner.home_room_owners == {}
    assert planner.home_room_id_by_name == {}


# ── _duid_owns_room ───────────────────────────────────────────────────────────


def test_duid_owns_room_by_exact_name_match() -> None:
    planner = _planner(
        devices={"d1": _FakeDevice()},
        segments={"d1": {"Bedroom": 1}},
        entity_of={"d1": "vacuum.d1"},
    )
    assert planner._duid_owns_room("d1", "Bedroom") is True


def test_duid_owns_room_by_home_room_id_pairing_under_a_different_name() -> None:
    planner = _planner(
        devices={"w1": _FakeDevice()},
        segments={"w1": {"Loznice": 7}},
        entity_of={"w1": "vacuum.w1"},
        home_room_owners={"R1": {"d1": 1, "w1": 7}},
        home_room_id_by_name={"Bedroom": "R1", "Loznice": "R1"},
    )
    # w1 doesn't know a room called "Bedroom" by name, but it owns the same
    # physical room ("R1") that "Bedroom" resolves to, under its own name.
    assert planner._duid_owns_room("w1", "Bedroom") is True


def test_duid_owns_room_false_when_neither_name_nor_pairing_matches() -> None:
    planner = _planner(
        devices={"w1": _FakeDevice()},
        segments={"w1": {"Loznice": 7}},
        entity_of={"w1": "vacuum.w1"},
        home_room_owners={"R1": {"d1": 1, "w1": 7}},
        home_room_id_by_name={"Bedroom": "R1"},
    )
    assert planner._duid_owns_room("w1", "Kitchen") is False


def test_duid_owns_room_false_when_room_pairs_to_a_room_this_duid_does_not_own() -> None:
    planner = _planner(
        devices={"d1": _FakeDevice(), "w1": _FakeDevice()},
        segments={"d1": {"Bedroom": 1}, "w1": {"Loznice": 7}},
        entity_of={"d1": "vacuum.d1", "w1": "vacuum.w1"},
        home_room_owners={"R1": {"d1": 1, "w1": 7}, "R2": {"d1": 3}},
        home_room_id_by_name={"Bedroom": "R1", "Loznice": "R1", "Office": "R2"},
    )
    assert planner._duid_owns_room("w1", "Office") is False


# ── _segment_for ──────────────────────────────────────────────────────────────


def test_segment_for_exact_name_match() -> None:
    planner = _planner(
        devices={"d1": _FakeDevice()},
        segments={"d1": {"Bedroom": 1}},
        entity_of={"d1": "vacuum.d1"},
    )
    assert planner._segment_for("d1", "Bedroom") == 1


def test_segment_for_resolves_own_segment_via_home_room_id_pairing() -> None:
    planner = _planner(
        devices={"w1": _FakeDevice()},
        segments={"w1": {"Loznice": 7}},
        entity_of={"w1": "vacuum.w1"},
        home_room_owners={"R1": {"d1": 1, "w1": 7}},
        home_room_id_by_name={"Bedroom": "R1", "Loznice": "R1"},
    )
    # Asked for "Bedroom" (d1's own name for the room) but resolved to w1's
    # OWN segment id (7), not d1's (1) and not a KeyError.
    assert planner._segment_for("w1", "Bedroom") == 7


def test_segment_for_raises_keyerror_when_unresolvable() -> None:
    planner = _planner(
        devices={"w1": _FakeDevice()},
        segments={"w1": {"Loznice": 7}},
        entity_of={"w1": "vacuum.w1"},
    )
    with pytest.raises(KeyError):
        planner._segment_for("w1", "Kitchen")


# ── assign() end-to-end cross-robot pairing ──────────────────────────────────


def test_assign_finds_a_wet_only_robot_for_a_room_known_by_the_dry_robots_name() -> None:
    """The scenario docs/40 §4.3 exists for: a dry-only robot (d1) and a
    wet-only robot (w1) clean the SAME physical room under different names
    of their own. Requesting the room by d1's name for a WET pass must still
    find w1 — impossible before `home_room_id` pairing, since w1's own
    `segments` map has no "Bedroom" entry at all."""
    planner = _planner(
        devices={
            "d1": _FakeDevice(),
            "w1": _FakeDevice(mop_signal={"water_box_mode": 200}),
        },
        segments={"d1": {"Bedroom": 1}, "w1": {"Loznice": 7}},
        entity_of={"d1": "vacuum.d1", "w1": "vacuum.w1"},
        home_room_owners={"R1": {"d1": 1, "w1": 7}},
        home_room_id_by_name={"Bedroom": "R1", "Loznice": "R1"},
    )
    assigned, unassigned = planner.assign(["Bedroom"], "wet")
    assert unassigned == []
    assert assigned == {"w1": ["Bedroom"]}


def test_assign_without_home_room_id_leaves_room_unassigned_as_before() -> None:
    """Regression: with no pairing at all (today's/legacy behaviour), a room
    requested under a name a robot doesn't itself use is simply unassigned —
    same as before this change, no KeyError, no crash."""
    planner = _planner(
        devices={
            "d1": _FakeDevice(),
            "w1": _FakeDevice(mop_signal={"water_box_mode": 200}),
        },
        segments={"d1": {"Bedroom": 1}, "w1": {"Loznice": 7}},
        entity_of={"d1": "vacuum.d1", "w1": "vacuum.w1"},
    )
    assigned, unassigned = planner.assign(["Bedroom"], "wet")
    assert unassigned == ["Bedroom"]
    assert assigned == {}


# ── build_tasks() dispatches each robot's OWN segment id ─────────────────────


def test_build_tasks_dispatches_each_robots_own_segment_for_a_paired_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a `both` job for a room known to d1 as "Bedroom" and to
    w1 (paired via home_room_id) as "Loznice" must send d1 its segment (1)
    for the dry task and w1 its OWN segment (7) — not d1's — for the wet
    task."""
    monkeypatch.setattr(
        "custom_components.anyvac.planner.selects_for_duid",
        lambda hass, duid: {},
    )
    planner = _planner(
        devices={
            "d1": _FakeDevice(),
            "w1": _FakeDevice(mop_signal={"water_box_mode": 200}),
        },
        segments={"d1": {"Bedroom": 1}, "w1": {"Loznice": 7}},
        entity_of={"d1": "vacuum.d1", "w1": "vacuum.w1"},
        home_room_owners={"R1": {"d1": 1, "w1": 7}},
        home_room_id_by_name={"Bedroom": "R1", "Loznice": "R1"},
    )
    tasks, plan = planner.build_tasks(["Bedroom"], "both")

    assert not plan.get("unassigned")
    assert plan["dry"] == {"vacuum.d1": ["Bedroom"]}
    assert plan["wet"] == {"vacuum.w1": ["Bedroom"]}

    dry_task = next(t for t in tasks if t["duid"] == "d1")
    wet_task = next(t for t in tasks if t["duid"] == "w1")
    assert dry_task["service_data"]["params"][0]["segments"] == [1]
    assert wet_task["service_data"]["params"][0]["segments"] == [7]
