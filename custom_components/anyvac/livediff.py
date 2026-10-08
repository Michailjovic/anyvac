"""Live position from the robot's local ``get_dynamic_map_diff`` (docs/48).

Pure functions + a small per-robot state object, no Home Assistant imports, so
everything here is unit-tested against real answers recorded on 2026-10-08
(docs/47 §4.3).

The diff answer is JSON; its key ``"3"`` carries ``start``/``len`` and a base64
``data`` field holding standard Roborock map blocks (the same binary format the
full map uses, see vacuum_map_parser_roborock):

* type 8  ROBOT_POSITION — int32 x, y[, angle]
* type 3  PATH           — uint16 x,y pairs: the path points added since the
                           previous diff, starting at global index ``start``
* type 18 MOP_PATH       — one flag byte per new point (non-zero = mop down,
                           exactly how the parser builds ``mop_path``)
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field
from typing import Any

BLOCK_ROBOT_POSITION = 8
BLOCK_PATH = 3
BLOCK_MOP_PATH = 18


def decode_blocks(raw: bytes) -> dict[str, Any]:
    """Decode the map blocks inside one diff ``data`` field.

    Returns ``{"pos": {"x", "y"[, "a"]} | None, "points": [(x, y)], "flags": [int]}``.
    Malformed or truncated blocks end the walk; whatever was decoded before
    them is kept.
    """
    out: dict[str, Any] = {"pos": None, "points": [], "flags": []}
    off = 0
    while off + 8 <= len(raw):
        btype, hlen, dlen = struct.unpack_from("<HHI", raw, off)
        if hlen < 8 or off + hlen + dlen > len(raw):
            break
        header = raw[off : off + hlen]
        data = raw[off + hlen : off + hlen + dlen]
        if btype == BLOCK_ROBOT_POSITION and dlen >= 8:
            x, y = struct.unpack_from("<ii", data, 0)
            pos: dict[str, Any] = {"x": x, "y": y}
            if dlen >= 12:
                a = struct.unpack_from("<i", data, 8)[0]
                if a > 0xFF:  # same wrap as the library parser
                    a = (a & 0xFF) - 256
                pos["a"] = a
            out["pos"] = pos
        elif btype == BLOCK_PATH:
            n = dlen // 4
            out["points"] = [struct.unpack_from("<HH", data, i * 4) for i in range(n)]
        elif btype == BLOCK_MOP_PATH:
            out["flags"] = list(data)
        off += hlen + dlen
    return out


def parse_diff(answer: Any) -> dict[str, Any] | None:
    """One ``get_dynamic_map_diff`` answer → ``{"start", "pos", "points",
    "flags"}`` or None when it is not a diff answer at all. ``start`` is None
    and the lists are empty when nothing moved since the previous request."""
    if not isinstance(answer, dict):
        return None
    out: dict[str, Any] = {"start": None, "pos": None, "points": [], "flags": []}
    if not isinstance(answer.get("diff"), dict):
        # `{"nonce": 0, "result": 2}` with no `diff` = a valid answer with
        # nothing in it (S7 MaxV while docked, 2026-10-08) — not a failure.
        return out if "result" in answer else None
    block = answer["diff"].get("3")
    if not isinstance(block, dict):
        return out
    data = block.get("data")
    if isinstance(data, str) and data:
        try:
            out.update(decode_blocks(base64.b64decode(data)))
        except (ValueError, struct.error):
            return out
        start = block.get("start")
        out["start"] = int(start) if isinstance(start, (int, float)) else None
    flags = out["flags"]
    if len(flags) < len(out["points"]):  # no/short mop block: treat as dry
        out["flags"] = flags + [0] * (len(out["points"]) - len(flags))
    return out


@dataclass
class LiveTrail:
    """The live extension of ONE robot's last full-map snapshot (docs/48 §1.3)."""

    base: int  # path points in the snapshot this extends
    next: int = 0  # global index of the next expected path point
    points: list[tuple[int, int]] = field(default_factory=list)
    flags: list[int] = field(default_factory=list)
    pos: dict[str, Any] | None = None
    broken: bool = False
    seq: int = 0

    def __post_init__(self) -> None:
        if not self.next:
            self.next = self.base

    def apply(self, parsed: dict[str, Any]) -> bool:
        """Merge one parsed diff. Returns True when something visible changed."""
        changed = False
        if parsed.get("pos") is not None:
            self.pos = parsed["pos"]
            changed = True
        pts = parsed.get("points") or []
        start = parsed.get("start")
        if pts and start is not None and not self.broken:
            flags = parsed.get("flags") or [0] * len(pts)
            if start > self.next:
                self.broken = True  # someone else consumed part of the diff
            else:
                skip = self.next - start
                if skip < len(pts):
                    self.points.extend(pts[skip:])
                    self.flags.extend(flags[skip:])
                    self.next = start + len(pts)
                    changed = True
        if changed:
            self.seq += 1
        return changed

    def dry_segment(self) -> list[dict[str, float]]:
        """Every new point (the dry trace is the whole trajectory while the
        snapshot's dry gate is open — docs/14 §3.9)."""
        return [{"x": x, "y": y} for x, y in self.points]

    def wet_segments(self) -> list[list[dict[str, float]]]:
        """Runs of mop-down points (non-zero flag), split where the flag drops."""
        segs: list[list[dict[str, float]]] = []
        cur: list[dict[str, float]] = []
        for (x, y), f in zip(self.points, self.flags):
            if f:
                cur.append({"x": x, "y": y})
            elif cur:
                segs.append(cur)
                cur = []
        if cur:
            segs.append(cur)
        return segs

    def wet_starts_on_first_point(self) -> bool:
        return bool(self.flags) and bool(self.flags[0])
