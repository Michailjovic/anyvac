"""docs/51 §1.4 — `anyvac.list_assets`: the studio's Furniture tool cannot list
config/www from the browser, so the integration does it."""

from __future__ import annotations

import os

from PIL import Image

from custom_components.anyvac.services import (
    FURNITURE_DIR,
    LIST_ASSETS_SCHEMA,
    _list_furniture_assets,
)


def _png(path: str, w: int, h: int) -> None:
    Image.new("RGBA", (w, h), (10, 20, 30, 128)).save(path)


def test_creates_the_folder_on_first_use(tmp_path) -> None:
    www = tmp_path / "www"
    www.mkdir()
    out = _list_furniture_assets(str(www))
    assert out == {"folder": "www/anyvac/furniture", "assets": []}
    assert (www / "anyvac" / "furniture").is_dir()


def test_lists_images_sorted_with_size_and_local_url(tmp_path) -> None:
    folder = tmp_path / "www" / os.path.join(*FURNITURE_DIR)
    folder.mkdir(parents=True)
    _png(str(folder / "sofa.png"), 120, 40)
    Image.new("RGB", (30, 60)).save(folder / "Bed.webp")
    (folder / "plant.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    out = _list_furniture_assets(str(tmp_path / "www"))
    names = [a["name"] for a in out["assets"]]
    assert names == ["Bed.webp", "plant.svg", "sofa.png"]  # case-insensitive order
    by = {a["name"]: a for a in out["assets"]}
    assert by["sofa.png"]["url"] == "/local/anyvac/furniture/sofa.png"
    assert (by["sofa.png"]["width"], by["sofa.png"]["height"]) == (120, 40)
    assert (by["Bed.webp"]["width"], by["Bed.webp"]["height"]) == (30, 60)
    assert by["plant.svg"]["width"] is None and by["plant.svg"]["height"] is None
    assert by["sofa.png"]["bytes"] > 0 and isinstance(by["sofa.png"]["mtime"], int)


def test_skips_non_images_hidden_files_and_subfolders(tmp_path) -> None:
    folder = tmp_path / "www" / "anyvac" / "furniture"
    (folder / "nested").mkdir(parents=True)
    _png(str(folder / "nested" / "deep.png"), 4, 4)
    (folder / "notes.txt").write_text("x")
    _png(str(folder / ".hidden.png"), 4, 4)
    os.mkdir(folder / "dir.png")  # a directory with an image extension
    assert _list_furniture_assets(str(tmp_path / "www"))["assets"] == []


def test_symlink_escaping_the_folder_is_skipped(tmp_path) -> None:
    folder = tmp_path / "www" / "anyvac" / "furniture"
    folder.mkdir(parents=True)
    secret = tmp_path / "secret.png"
    _png(str(secret), 4, 4)
    os.symlink(secret, folder / "link.png")
    assert _list_furniture_assets(str(tmp_path / "www"))["assets"] == []


def test_name_with_spaces_is_url_quoted(tmp_path) -> None:
    folder = tmp_path / "www" / "anyvac" / "furniture"
    folder.mkdir(parents=True)
    _png(str(folder / "big sofa.png"), 8, 4)
    (asset,) = _list_furniture_assets(str(tmp_path / "www"))["assets"]
    assert asset["url"] == "/local/anyvac/furniture/big%20sofa.png"


def test_unreadable_raster_is_listed_without_size(tmp_path) -> None:
    folder = tmp_path / "www" / "anyvac" / "furniture"
    folder.mkdir(parents=True)
    (folder / "broken.webp").write_bytes(b"not an image")
    (asset,) = _list_furniture_assets(str(tmp_path / "www"))["assets"]
    assert asset["name"] == "broken.webp" and asset["width"] is None


def test_schema_defaults_kind_to_furniture() -> None:
    assert LIST_ASSETS_SCHEMA({}) == {"kind": "furniture"}
