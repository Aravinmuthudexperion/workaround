import pytest

from arc_tower.integrations.prometheus.promql import render, selector


def test_single_tenant_and_product():
    assert selector(["acme"], ["claims"]) == 'arc_tenant_id="acme",arc_product_id="claims"'


def test_multiple_values_use_regex():
    assert selector(["a", "b"]) == 'arc_tenant_id=~"a|b"'


def test_empty_scope_matches_nothing():
    # A user with no tenants must never get an unfiltered query.
    assert selector([]) == 'arc_tenant_id="__none__"'


def test_all_tenants_has_no_tenant_matcher():
    assert selector(None) == ""


def test_rejects_injection():
    with pytest.raises(ValueError):
        selector(['acme"} or vector(1) #'])


def test_render_fills_selector_and_window():
    expr = render('sum(increase(arc_calls_total{{sel}}[{window}]))', selector(["acme"]), "1h")
    assert expr == 'sum(increase(arc_calls_total{arc_tenant_id="acme"}[1h]))'
