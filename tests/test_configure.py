import json

import pytest

from tools.configure import PROVIDER, configure


def test_provider_switch_preserves_credentials_paths_and_instance_id(tmp_path):
    path = tmp_path / "config.json"
    source = {
        "plugins": {
            "instances": [
                {
                    "id": "default",
                    "point": "downloader",
                    "provider": "qbittorrent",
                    "options": {
                        "host": "qb:8080",
                        "password": "private-value",
                        "path": "/downloads",
                    },
                }
            ]
        },
        "other": {"untouched": True},
    }
    path.write_text(json.dumps(source))
    assert not configure(path, ["default"])["applied"]
    assert json.loads(path.read_text()) == source
    result = configure(path, ["default"], apply=True)
    assert json.loads(open(result["backup"]).read()) == source
    changed = json.loads(path.read_text())
    assert changed["plugins"]["instances"][0]["provider"] == PROVIDER
    changed["plugins"]["instances"][0]["provider"] = "qbittorrent"
    assert changed == source
    assert "private-value" not in json.dumps(result)
    configure(path, ["default"], apply=True, restore=True)
    assert json.loads(path.read_text()) == source


def test_missing_instance_refuses_without_modifying_config(tmp_path):
    path = tmp_path / "config.json"
    raw = '{"plugins":{"instances":[]}}'
    path.write_text(raw)
    with pytest.raises(ValueError):
        configure(path, ["default"], apply=True)
    assert path.read_text() == raw
