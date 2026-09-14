"""Shared helper for building synthetic raw Roborock map blobs (docs/40 §2)
in tests, WITHOUT a real device dump — used by any test that needs to drive
`homeframe.decode_grid`/`grid_self_test`/the coordinator's home-frame
pipeline end to end. Not a test file itself (leading underscore, no
``test_`` functions) so pytest never collects it.

The byte layout mirrors docs/40 §2 exactly (verified against real device
dumps in Fáze 0): a 20-byte main header (``map_header_length`` at 0x02,
``map_index``/``map_sequence`` at 0x0C/0x10) followed by one IMAGE block
(type 2) whose ``top``/``left``/``height``/``width`` live in the LAST 16
bytes of its own header, and whose body is a plain row-major byte grid where
``0xFF`` = unsegmented floor, ``0x01`` = wall, and ``(segment_id << 3) | 7``
= that segment's room cells.
"""

from __future__ import annotations

import struct


def make_raw_map(
    width: int,
    height: int,
    top: int,
    left: int,
    map_index: int = 1,
    map_sequence: int = 1,
    rooms: dict[int, tuple[int, int, int, int]] | None = None,
) -> bytes:
    """Build a minimal synthetic raw map blob. `rooms` is
    `{segment_id: (x0, y0, x1, y1)}` in the image's own (col, row) cell
    indices (half-open, like a numpy slice) — a bordering ring of wall cells
    is always painted so the grid isn't a degenerate solid block."""
    import numpy as np

    rooms = rooms or {}
    grid = np.zeros((height, width), dtype=np.uint8)
    grid[:, :] = 0xFF
    grid[0, :] = 0x01
    grid[-1, :] = 0x01
    grid[:, 0] = 0x01
    grid[:, -1] = 0x01
    for room_id, (x0, y0, x1, y1) in rooms.items():
        grid[y0:y1, x0:x1] = (room_id << 3) | 7
    image_bytes = grid.tobytes()

    image_header_len = 24
    image_header = bytearray(image_header_len)
    struct.pack_into("<H", image_header, 0x00, 2)  # block_type = IMAGE(2)
    struct.pack_into("<H", image_header, 0x02, image_header_len)
    struct.pack_into("<i", image_header, 0x04, len(image_bytes))
    struct.pack_into("<i", image_header, image_header_len - 16, top)
    struct.pack_into("<i", image_header, image_header_len - 12, left)
    struct.pack_into("<i", image_header, image_header_len - 8, height)
    struct.pack_into("<i", image_header, image_header_len - 4, width)

    main_header_len = 20
    main_header = bytearray(main_header_len)
    struct.pack_into("<H", main_header, 0x02, main_header_len)
    struct.pack_into("<H", main_header, 0x08, 1)
    struct.pack_into("<H", main_header, 0x0A, 0)
    struct.pack_into("<i", main_header, 0x0C, map_index)
    struct.pack_into("<i", main_header, 0x10, map_sequence)

    return bytes(main_header) + bytes(image_header) + image_bytes


class FakeLibRoom:
    """Minimal stand-in for `vacuum_map_parser_base`'s own `Room` — only the
    4 bbox attributes `grid_self_test` reads."""

    def __init__(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1


def lib_rooms_for(
    rooms_cells: dict[int, tuple[int, int, int, int]], top: int, left: int, cell_mm: int = 50
) -> dict[int, FakeLibRoom]:
    """Build the `lib_rooms` (`{segment_id: Room}`) `grid_self_test` expects,
    from the SAME `(x0, y0, x1, y1)` cell rects passed to `make_raw_map` —
    matching `cell_to_mm`'s own `(left + ix) * cell_mm` arithmetic exactly
    (inclusive of the last painted cell, per `grid_self_test`'s own
    `xs.max()`/`ys.max()`)."""
    out: dict[int, FakeLibRoom] = {}
    for seg_id, (x0, y0, x1, y1) in rooms_cells.items():
        gx0, gy0 = (left + x0) * cell_mm, (top + y0) * cell_mm
        gx1, gy1 = (left + x1 - 1) * cell_mm, (top + y1 - 1) * cell_mm
        out[seg_id] = FakeLibRoom(gx0, gy0, gx1, gy1)
    return out
