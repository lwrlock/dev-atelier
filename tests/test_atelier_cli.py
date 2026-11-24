"""TDD tests for unified dev-atelier CLI & workspace registry."""

import json
from dev_atelier.cli import APP_REGISTRY, main


def test_app_registry_contains_three_studios():
    assert set(APP_REGISTRY.keys()) == {"flowforge", "cortexscope", "hexward"}
    assert APP_REGISTRY["flowforge"]["default_port"] == 11440
    assert APP_REGISTRY["cortexscope"]["default_port"] == 11435
    assert APP_REGISTRY["hexward"]["default_port"] == 11460


def test_cli_info_outputs_json_inventory(capsys):
    rc = main(["info", "--json"])
    assert rc == 0
    captured = json.loads(capsys.readouterr().out)
    assert len(captured["apps"]) == 3
    names = {a["name"] for a in captured["apps"]}
    assert names == {"flowforge", "cortexscope", "hexward"}
