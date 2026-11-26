"""A fake fleet that behaves like the firmware measured in docs/52 §2/§6–§8.

Used by the job runner tests (docs/52 D4). What it models:

* ``app_segment_clean`` is IGNORED while the robot cleans or washes (the service
  still succeeds); a free robot (docked, idle, paused, or on its way home)
  takes it.
* Taking a task resets the robot to its default settings (S8, §8.1): fan
  ``balanced``, water ``mild``. The runner must apply them again.
* ``pause`` → ``paused``; ``return_to_base`` → ``returning_home``;
  ``vacuum.start`` from ``paused`` continues the old task.
* A test ends a room with `finish_room()` (the robot turns home and the
  coordinator reports the task) and docks it with `dock()`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import custom_components.anyvac.jobrunner as jr

UTC = timezone.utc


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now


class FakeBus:
    def __init__(self, hass: FakeHass) -> None:
        self.hass = hass
        self._listeners: dict[str, list[Any]] = {}
        self.fired: list[tuple[str, dict[str, Any]]] = []

    def async_listen(self, event_type: str, handler: Any):
        self._listeners.setdefault(event_type, []).append(handler)

        def _unsub() -> None:
            if handler in self._listeners.get(event_type, []):
                self._listeners[event_type].remove(handler)

        return _unsub

    def async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        self.fired.append((event_type, dict(data)))
        for handler in list(self._listeners.get(event_type, [])):
            self.hass.async_create_task(handler(SimpleNamespace(data=data)))

    def names(self) -> list[str]:
        return [n for n, _ in self.fired]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [d for n, d in self.fired if n == name]


class Robot:
    def __init__(self, duid: str, entity: str, battery: int = 100, wet: bool = False) -> None:
        self.duid = duid
        self.entity = entity
        self.wet = wet
        self.data: dict[str, Any] = {
            "in_cleaning": False,
            "status_state": "charging",
            "target_segments": [],
            "battery": battery,
            "repeat": None,
            "mop_signal": {"fan_speed_name": "balanced"},
            "dock_status": {},
            "rooms_progress": {},
        }
        self.ignored = 0
        self.taken: list[list[int]] = []

    @property
    def busy(self) -> bool:
        st = self.data["status_state"]
        return st in jr.ACTIVE_STATES or st in jr.WASH_STATES

    def command(self, segments: list[int], repeat: int) -> None:
        if self.busy:
            self.ignored += 1
            return
        self.taken.append(list(segments))
        self.data.update(in_cleaning=True, status_state="segment_cleaning",
                         target_segments=sorted(segments), repeat=repeat)
        self.data["mop_signal"] = {"fan_speed_name": "balanced"}  # firmware default


class FakeServices:
    def __init__(self, hass: FakeHass) -> None:
        self.hass = hass
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.states: dict[str, str] = {}

    async def async_call(self, domain, service, data, blocking=True, context=None):
        self.calls.append((domain, service, dict(data)))
        ent = data.get("entity_id")
        robot = self.hass.robot_of(ent) if isinstance(ent, str) else None
        if domain == "vacuum" and robot is not None:
            if service == "send_command" and data.get("command") == "app_segment_clean":
                p = data["params"][0]
                robot.command(p["segments"], p.get("repeat", 1))
            elif service == "send_command" and data.get("command") == "app_start_wash":
                robot.data["status_state"] = "washing_the_mop"
            elif service == "pause":
                robot.data["status_state"] = "paused"
            elif service == "return_to_base":
                robot.data["status_state"] = "returning_home"
            elif service == "set_fan_speed":
                robot.data["mop_signal"] = {"fan_speed_name": data["fan_speed"]}
        elif domain == "select" and service == "select_option":
            self.states[ent] = data["option"]

    def sent(self, entity: str | None = None) -> list[list[int]]:
        return [
            d["params"][0]["segments"]
            for dom, svc, d in self.calls
            if dom == "vacuum" and svc == "send_command" and d.get("command") == "app_segment_clean"
            and (entity is None or d["entity_id"] == entity)
        ]

    def count(self, domain: str, service: str, entity: str | None = None) -> int:
        return sum(
            1 for dom, svc, d in self.calls
            if dom == domain and svc == service and (entity is None or d.get("entity_id") == entity)
        )


class FakeHass:
    def __init__(self) -> None:
        self.bus = FakeBus(self)
        self.services = FakeServices(self)
        self.states = SimpleNamespace(get=self._state)
        self.data: dict[str, Any] = {}
        self.robots: dict[str, Robot] = {}
        self.tasks: set[asyncio.Task] = set()

    def _state(self, entity_id: str):
        val = self.services.states.get(entity_id)
        return SimpleNamespace(state=val) if val is not None else None

    def robot_of(self, entity: str) -> Robot | None:
        return next((r for r in self.robots.values() if r.entity == entity), None)

    def async_create_task(self, coro, *a, **k):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


class FakeCoord:
    def __init__(self, hass: FakeHass) -> None:
        self.hass = hass
        self.job_rooms: dict[str, set[str]] = {}
        self.task_item: dict[str, set[str] | None] = {}
        self.discarded: list[str] = []
        self.job_state: Any = None
        self.saved: list[Any] = []
        self.warnings: list[str] = []
        self.listeners: list[Any] = []

    @property
    def data(self) -> dict[str, Any]:
        return {d: SimpleNamespace(data=r.data) for d, r in self.hass.robots.items()}

    def set_job_rooms(self, duid, rooms, keep_id=False):
        if rooms is None:
            self.job_rooms.pop(duid, None)
        else:
            self.job_rooms[duid] = set(rooms)

    def set_task_item(self, duid, rooms):
        self.task_item[duid] = rooms

    def discard_task(self, duid):
        self.discarded.append(duid)

    def set_job_state(self, state):
        self.job_state = state

    def save_job(self, snap):
        import copy

        self.saved.append(copy.deepcopy(snap))

    def add_job_warning(self, text):
        self.warnings.append(text)

    def async_add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb) if cb in self.listeners else None

    async def async_request_refresh(self):
        return None

    def update(self) -> None:
        for cb in list(self.listeners):
            cb()


class Rig:
    """hass + coordinator + robots + clock, with `jobrunner` patched to them."""

    def __init__(self, monkeypatch) -> None:
        self.clock = Clock()
        self.hass = FakeHass()
        self.coord = FakeCoord(self.hass)
        self.timers: list[tuple[float, Any]] = []
        monkeypatch.setattr(jr.dt_util, "utcnow", lambda: self.clock.now)
        monkeypatch.setattr(jr, "_coordinators", lambda hass: [self.coord])
        monkeypatch.setattr(jr, "async_call_later", self._call_later)
        monkeypatch.setattr(jr, "REAPPLY_DELAY_S", 0)
        monkeypatch.setattr(jr, "VERIFY_DELAY_S", 0)

    def _call_later(self, hass, delay, cb):
        entry = (delay, cb)
        self.timers.append(entry)
        return lambda: self.timers.remove(entry) if entry in self.timers else None

    def add(self, duid: str, entity: str, **kw) -> Robot:
        robot = Robot(duid, entity, **kw)
        self.hass.robots[duid] = robot
        return robot

    async def drain(self) -> None:
        for _ in range(50):
            await asyncio.sleep(0)
            pending = [t for t in self.hass.tasks if not t.done()]
            if not pending:
                await asyncio.sleep(0)
                if not [t for t in self.hass.tasks if not t.done()]:
                    return
            else:
                await asyncio.gather(*pending, return_exceptions=True)

    async def poll(self) -> None:
        """A coordinator update: the runner looks at the robots again."""
        self.coord.update()
        await self.drain()

    async def finish_room(self, robot: Robot, rooms: list[str], cleaned: bool = True, **extra) -> None:
        """The robot ends its task and turns home (docs/52 §2): the coordinator
        reports the task with what it cleaned."""
        robot.data.update(status_state="returning_home")
        self.hass.bus.async_fire("anyvac_clean_finished", {
            "duid": robot.duid, "vacuum": robot.entity,
            "rooms_result": [
                {"room": r, "cleaned": cleaned, "active_min": 5.0, "passes": robot.data.get("repeat"),
                 "dry": 100 if cleaned else None}
                for r in rooms
            ],
            **extra,
        })
        await self.drain()

    async def dock(self, robot: Robot, washed: bool = False) -> None:
        if washed:
            robot.data.update(status_state="washing_the_mop")
            await self.poll()
        robot.data.update(in_cleaning=False, status_state="charging", target_segments=[], repeat=None)
        await self.poll()


def item(duid: str, entity: str, kind: str, room: str, seg: int, *, est: float = 5.0, repeat: int = 1, **kw) -> dict[str, Any]:
    return {
        "id": f"{kind}-{duid}-{room}",
        "vacuum": entity, "duid": duid, "kind": kind, "batch": kw.get("batch", False),
        "room": room, "rooms": [room], "segments": [seg],
        "selects": kw.get("selects", []), "fan_speed": kw.get("fan_speed"),
        "repeat": repeat, "expect": {"fan_speed": kw.get("fan_speed"), "mop_mode": None, "water_off": False},
        "est_min": est,
    }
