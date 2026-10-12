import pytest

from arc_tower.services import layouts
from arc_tower.services.errors import ValidationFailed

KPIS = {"llm_calls", "cost_usd", "error_rate"}


def test_validate_drops_unknown_and_fixes_unsupported_groups():
    clean = layouts.validate({
        "tiles": ["cost_usd", "nope", "cost_usd", "error_rate"],
        "charts": [
            {"metric": "pii_masked", "group": "model", "kind": "bar"},      # PII has no model split
            {"metric": "bogus", "group": "agent", "kind": "line"},          # unknown metric dropped
            {"metric": "cost", "group": "none", "kind": "pie", "title": "x" * 200},  # pie needs a split
        ],
    }, KPIS)
    assert clean["tiles"] == ["cost_usd", "error_rate"]
    assert [(c["metric"], c["group"], c["kind"]) for c in clean["charts"]] == [
        ("pii_masked", "none", "bar"), ("cost", "product", "pie")]
    assert clean["charts"][0]["title"] == "PII values masked"
    assert len(clean["charts"][1]["title"]) == 80


def test_empty_layout_rejected():
    with pytest.raises(ValidationFailed):
        layouts.validate({"tiles": [], "charts": []}, KPIS)


def test_every_default_layout_is_valid():
    for page, config in layouts.DEFAULTS.items():
        clean = layouts.validate(config, {t for t in config["tiles"]})
        assert len(clean["charts"]) == len(config["charts"]), page
