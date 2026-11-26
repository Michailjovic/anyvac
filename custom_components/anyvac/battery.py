"""Battery model of a robot (docs/53 §5).

Learned from ordinary cleans, per robot:
- drain in %/min while cleaning, per kind ("dry"/"wet"), also per fan speed
  ("dry@max", "wet@balanced") when it is known;
- charging speed in %/min below 80 % ("lo") and from 80 % up ("hi", slower).

The rate is measured between EDGES — the moments the whole-number battery
level changes — not between the ends of a segment: a segment starts and ends
anywhere inside a 1 % step, which is up to ~1.7 min of error at each end at
0.6 %/min; edge to edge leaves only the delay with which the change reached us
(the coordinator records the level on every Roborock push, not only on map
snapshots). Each accepted segment is also kept as a sample (date, kind,
setting, rate) — the later battery-health estimate (docs/53 §5 "později")
needs that history.

Pure functions and a small tracker: no Home Assistant here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Firmware sends the robot home at 20 %; a clean can be forced from ~30 %.
FIRMWARE_LOW = 20.0
# docs/53 §5: a room is only sent when the battery stays at or above this after it.
RESERVE = 25.0
# docs/53 P1: charge to 20 % + what the rest of the queue needs + 3 %.
CHARGE_MARGIN = 3.0
# Starting values until the robot has been measured.
DEFAULT_DRAIN = {"dry": 0.55, "wet": 0.65}
DEFAULT_CHARGE = {"lo": 0.8, "hi": 0.3}
SAMPLES_MAX = 300

MIN_SEGMENT_MIN = 3.0
MIN_SEGMENT_PCT = 2.0
MAX_GAP_S = 300.0


@dataclass
class Segment:
    mode: str
    setting: str | None
    t0: datetime
    b0: float
    t_last: datetime
    b_last: float
    # (time, new level) of every level change seen inside the segment.
    edges: list[tuple[datetime, float]] = field(default_factory=list)

    def edge(self, at: datetime, level: float) -> None:
        if self.edges and self.edges[-1][1] == level:
            return
        if not self.edges and level == self.b0:
            return
        self.edges.append((at, level))


@dataclass
class Model:
    """What the planner needs: rates with defaults filled in."""

    drain: dict[str, float] = field(default_factory=dict)
    charge: dict[str, float] = field(default_factory=dict)

    def drain_rate(self, kind: str | None, setting: str | None = None) -> float:
        k = kind if kind in ("dry", "wet") else "dry"
        if setting and f"{k}@{setting}" in self.drain:
            return self.drain[f"{k}@{setting}"]
        return self.drain.get(k, DEFAULT_DRAIN[k])

    def charge_minutes(self, frm: float, to: float) -> float:
        """Minutes to charge from `frm` to `to` %."""
        lo = self.charge.get("lo", DEFAULT_CHARGE["lo"])
        hi = self.charge.get("hi", DEFAULT_CHARGE["hi"])
        frm, to = max(0.0, frm), min(100.0, to)
        if to <= frm:
            return 0.0
        below = max(0.0, min(to, 80.0) - frm)
        above = max(0.0, to - max(frm, 80.0))
        return below / lo + above / hi


def model_from(learned: dict[str, Any] | None) -> Model:
    learned = learned or {}
    return Model(
        drain={k: float(v) for k, v in (learned.get("drain") or {}).items() if isinstance(v, (int, float))},
        charge={k: float(v) for k, v in (learned.get("charge") or {}).items() if isinstance(v, (int, float))},
    )


def charge_target(rest_need: float) -> float:
    """docs/53 P1: firmware low + what is left to do + a margin, at most 100."""
    return min(100.0, FIRMWARE_LOW + max(0.0, rest_need) + CHARGE_MARGIN)


def activity(data: dict[str, Any], cleaning_states: set[str]) -> tuple[str | None, str | None]:
    """What the robot is doing for the battery model: ("dry"|"wet", fan) while
    it cleans, ("charge_lo"|"charge_hi", None) while it charges, else None."""
    state = data.get("status_state")
    battery = data.get("battery")
    if data.get("in_cleaning") and state in cleaning_states and not data.get("transit"):
        wet = data.get("clean_type") == "wet" and not data.get("water_off")
        fan = (data.get("mop_signal") or {}).get("fan_speed_name")
        return ("wet" if wet else "dry"), (str(fan).lower() if fan else None)
    if state == "charging" and isinstance(battery, (int, float)) and battery < 100:
        return ("charge_hi" if battery >= 80 else "charge_lo"), None
    return None, None


def close(seg: Segment) -> tuple[str, float] | None:
    """(key, %/min) of a finished segment from its first to its last level
    change, or None when that span is too short."""
    if len(seg.edges) < 2:
        return None
    (ta, ba), (tb, bb) = seg.edges[0], seg.edges[-1]
    minutes = (tb - ta).total_seconds() / 60
    delta = bb - ba
    if minutes < MIN_SEGMENT_MIN:
        return None
    if seg.mode.startswith("charge"):
        if delta < MIN_SEGMENT_PCT:
            return None
        rate = delta / minutes
        return (seg.mode.split("_")[1], round(rate, 3)) if 0.02 <= rate <= 5 else None
    if -delta < MIN_SEGMENT_PCT:
        return None
    rate = -delta / minutes
    return (seg.mode, round(rate, 3)) if 0.02 <= rate <= 5 else None


def learn(learned: dict[str, Any], key: str, rate: float, setting: str | None, at: datetime) -> None:
    """Fold one measured rate into the robot's record (rolling average)."""
    is_charge = key in ("lo", "hi")
    table = learned.setdefault("charge" if is_charge else "drain", {})
    keys = [key] if is_charge or not setting else [key, f"{key}@{setting}"]
    for k in keys:
        old = table.get(k)
        table[k] = round(rate if old is None else 0.7 * old + 0.3 * rate, 3)
    samples = learned.setdefault("samples", [])
    samples.append({"at": at.isoformat(timespec="seconds"), "kind": key, "setting": setting, "rate": rate})
    del samples[:-SAMPLES_MAX]
