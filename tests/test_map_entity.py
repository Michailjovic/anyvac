"""Card 1.62.1: the integration names the image entity of the current map."""
from types import SimpleNamespace

from homeassistant.helpers import entity_registry as er

from custom_components.anyvac import coordinator as c


def _home(flag, names):
    return SimpleNamespace(
        current_map_data=SimpleNamespace(map_flag=flag),
        home_map_info={f: SimpleNamespace(name=n, map_flag=f) for f, n in names.items()},
    )


def test_names_the_entity_of_the_current_map(monkeypatch) -> None:
    seen = {}

    class Reg:
        def async_get_entity_id(self, domain, platform, uid):
            seen["args"] = (domain, platform, uid)
            return {"abc_map_Jirsikova": "image.s7_maxv_jirsikova", "abc_map_Map 1": "image.s7_maxv_map_1"}.get(uid)

    monkeypatch.setattr(er, "async_get", lambda hass: Reg())
    coord = SimpleNamespace(duid_slug="abc")
    home = _home(0, {0: "Jirsikova", 1: ""})
    assert c._current_map_entity(object(), coord, home) == "image.s7_maxv_jirsikova"
    assert seen["args"] == ("image", "roborock", "abc_map_Jirsikova")
    home = _home(1, {0: "Jirsikova", 1: ""})
    assert c._current_map_entity(object(), coord, home) == "image.s7_maxv_map_1"


def test_none_when_anything_is_missing() -> None:
    assert c._current_map_entity(None, SimpleNamespace(duid_slug="abc"), _home(0, {0: "A"})) is None
    assert c._current_map_entity(object(), SimpleNamespace(), _home(0, {0: "A"})) is None
    assert c._current_map_entity(object(), SimpleNamespace(duid_slug="a"), _home(5, {0: "A"})) is None
