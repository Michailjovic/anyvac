"""docs/50 (block G): translations stay in step with the source strings."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "anyvac"


def _load(name):
    return json.loads((ROOT / "translations" / name).read_text(encoding="utf-8"))


def _paths(d, prefix=()):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _paths(v, prefix + (k,))
        else:
            yield prefix + (k,)


def test_strings_json_is_the_english_translation():
    assert json.loads((ROOT / "strings.json").read_text(encoding="utf-8")) == _load("en.json")


def test_czech_covers_every_english_config_and_options_string():
    en, cs = _load("en.json"), _load("cs.json")
    for section in ("config", "options"):
        assert set(_paths(en[section])) == set(_paths(cs[section])), section


def test_czech_placeholders_match_english():
    en, cs = _load("en.json"), _load("cs.json")
    for section in ("config", "options"):
        for path in _paths(en[section]):
            a, b = en[section], cs[section]
            for p in path:
                a, b = a[p], b[p]
            assert sorted(re.findall(r"\{\w+\}", a)) == sorted(re.findall(r"\{\w+\}", b)), path


def test_czech_services_exist_and_fields_match_services_yaml():
    cs = _load("cs.json")["services"]
    yaml_text = (ROOT / "services.yaml").read_text(encoding="utf-8")
    for svc, body in cs.items():
        m = re.search(rf"^{svc}:\n(.*?)(?=^\S|\Z)", yaml_text, re.S | re.M)
        assert m, f"{svc} is not a service in services.yaml"
        yaml_fields = set(re.findall(r"^    (\w+):\n", m.group(1), re.M))
        assert set(body["fields"]) == yaml_fields, svc
        assert body["name"] and body["description"]
        for f in body["fields"].values():
            assert f["name"] and f["description"]
