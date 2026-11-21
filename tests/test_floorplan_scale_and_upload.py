"""docs/51 §1.3/§1.4 — floorplan scale for centimetre sizes, and furniture upload."""

from __future__ import annotations

import base64
import io
import math
import os

import pytest
from PIL import Image

from custom_components.anyvac.services import (
    FLOORPLAN_SCALE_SCHEMA,
    UPLOAD_ASSET_SCHEMA,
    UPLOAD_MAX_BYTES,
    _floorplan_mm_per_unit,
    _safe_asset_name,
    _similarity_scale,
    _store_asset,
)

FRAME = {"cell_mm": 50.0, "scale": 4.0}


def test_crop_from_a_vacuum_map() -> None:
    # 400 px wide crop at 0.08 px/mm -> 5000 mm -> 50 mm per unit
    mm, src = _floorplan_mm_per_unit({"entity": "vacuum.s8", "x0": 100, "y0": 0, "x1": 500, "y1": 9}, None, None,
                                     vacuum_px_per_mm=0.08, frame=None)
    assert (mm, src) == (50.0, "crop_vacuum")


def test_crop_from_a_home_frame() -> None:
    # home px = 4 per 50 mm -> 800 px = 10 m -> 100 mm per unit
    mm, src = _floorplan_mm_per_unit({"frame_id": "f", "x0": 0, "y0": 0, "x1": 800, "y1": 1}, None, None,
                                     vacuum_px_per_mm=None, frame=FRAME)
    assert (mm, src) == (100.0, "crop_frame")


def test_home_anchors_similarity_scale() -> None:
    # floorplan 2:1; home px = 3 x unit, rotated 30°, shifted
    aspect = 2.0
    th = math.radians(30)
    floor = [(10, 20), (90, 30), (50, 80), (20, 70)]
    anchors = []
    for fx, fy in floor:
        u, v = fx, fy / aspect
        hx = 3 * (u * math.cos(th) - v * math.sin(th)) + 40
        hy = 3 * (u * math.sin(th) + v * math.cos(th)) + 7
        anchors.append({"home_px": {"x": hx, "y": hy}, "floor_pct": {"x": fx, "y": fy}})
    mm, src = _floorplan_mm_per_unit(None, anchors, aspect, vacuum_px_per_mm=None, frame=FRAME)
    assert src == "home_anchors"
    assert mm == pytest.approx(3 * 50 / 4)  # 3 home px per unit, 12.5 mm per home px


def test_mirrored_anchors_never_give_a_negative_or_zero_scale() -> None:
    src = [(0, 0), (10, 0), (0, 10)]
    dst = [(0, 0), (-10, 0), (0, 10)]  # mirror: best non-mirroring fit has a smaller scale, still > 0
    s = _similarity_scale(src, dst)
    assert s is None or s > 0
    assert _similarity_scale([(1, 1), (1, 1)], [(0, 0), (5, 5)]) is None


def test_no_scale_without_usable_input() -> None:
    assert _floorplan_mm_per_unit(None, None, None, vacuum_px_per_mm=None, frame=None) == (None, None)
    # entity crop without calibration, anchors without aspect
    assert _floorplan_mm_per_unit({"entity": "v", "x0": 0, "y0": 0, "x1": 10, "y1": 1}, None, None,
                                  vacuum_px_per_mm=None, frame=None) == (None, None)
    a = [{"home_px": {"x": 0, "y": 0}, "floor_pct": {"x": 0, "y": 0}}, {"home_px": {"x": 1, "y": 0}, "floor_pct": {"x": 1, "y": 0}}]
    assert _floorplan_mm_per_unit(None, a, None, vacuum_px_per_mm=None, frame=FRAME) == (None, None)
    assert FLOORPLAN_SCALE_SCHEMA({"aspect": 2})["aspect"] == 2.0


def _png_b64(w: int = 20, h: int = 10) -> str:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (1, 2, 3, 200)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_upload_writes_into_the_furniture_folder(tmp_path) -> None:
    out = _store_asset(str(tmp_path), "My Sofa.png", _png_b64(), False)
    assert out["name"] == "My_Sofa.png"
    assert out["url"] == "/local/anyvac/furniture/My_Sofa.png"
    assert (out["width"], out["height"]) == (20, 10)
    assert (tmp_path / "anyvac" / "furniture" / "My_Sofa.png").is_file()
    # same name again -> suffix, not overwritten
    assert _store_asset(str(tmp_path), "My Sofa.png", _png_b64(5, 5), False)["name"] == "My_Sofa_2.png"
    assert _store_asset(str(tmp_path), "My Sofa.png", _png_b64(6, 6), True)["width"] == 6


def test_upload_accepts_a_data_url_and_svg(tmp_path) -> None:
    out = _store_asset(str(tmp_path), "a.png", "data:image/png;base64," + _png_b64(), False)
    assert out["width"] == 20
    svg = base64.b64encode(b"<?xml version='1.0'?><svg xmlns='http://www.w3.org/2000/svg'/>").decode()
    assert _store_asset(str(tmp_path), "plant.svg", svg, False)["width"] is None


@pytest.mark.parametrize(
    "name,data,msg",
    [
        ("evil.exe", "AAAA", "only"),
        ("x.png", "not base64!!", "base64"),
        ("x.png", base64.b64encode(b"hello").decode(), "not a readable image"),
        ("x.svg", base64.b64encode(b"<html></html>").decode(), "not an SVG"),
        ("x.png", "", "empty"),
    ],
)
def test_upload_rejects_bad_input(tmp_path, name: str, data: str, msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        _store_asset(str(tmp_path), name, data, False)
    assert not any((tmp_path / "anyvac" / "furniture").glob("*")) if (tmp_path / "anyvac").exists() else True


def test_upload_size_limit(tmp_path) -> None:
    big = base64.b64encode(b"\0" * (UPLOAD_MAX_BYTES + 1)).decode()
    with pytest.raises(ValueError, match="larger"):
        _store_asset(str(tmp_path), "big.png", big, False)


def test_safe_names_never_leave_the_folder() -> None:
    assert _safe_asset_name("../../.storage/x.png") == "x.png"
    assert _safe_asset_name("C:\\\\Users\\\\a\\\\Gauč velký.WEBP") == "Gau_velk.webp"
    with pytest.raises(ValueError):
        _safe_asset_name(".png")
    assert UPLOAD_ASSET_SCHEMA({"name": "a.png", "data": "x"})["overwrite"] is False
