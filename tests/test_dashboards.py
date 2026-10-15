from arc_tower.web.routes.dashboards import editable_copy

LIB = {"arc-lib-cost": {"type": "bargauge", "title": "Cost by agent", "targets": [{"expr": "cost"}],
                        "gridPos": {"x": 0, "y": 0, "w": 1, "h": 1}}}


def _standard():
    return {
        "id": 42, "uid": "arc-overview", "version": 7, "title": "ARC · Agent Overview",
        "tags": ["arc", "standard"], "editable": False,
        "panels": [
            {"id": 1, "type": "stat", "title": "Calls", "gridPos": {"x": 0, "y": 0, "w": 6, "h": 4}},
            {"id": 2, "gridPos": {"x": 6, "y": 0, "w": 6, "h": 4},
             "libraryPanel": {"uid": "arc-lib-cost", "name": "Cost by agent"}},
            {"id": 3, "type": "row", "collapsed": True, "gridPos": {"x": 0, "y": 4, "w": 24, "h": 1},
             "panels": [{"id": 4, "gridPos": {"x": 0, "y": 5, "w": 12, "h": 8},
                         "libraryPanel": {"uid": "arc-lib-cost", "name": "Cost by agent"}}]},
        ],
    }


def test_copy_is_a_new_editable_custom_dashboard():
    source = _standard()
    copy = editable_copy(source, "Agent Overview (custom)", LIB)
    assert "id" not in copy and "uid" not in copy and "version" not in copy
    assert copy["title"] == "Agent Overview (custom)"
    assert copy["editable"] is True
    assert copy["tags"] == ["arc", "custom"]
    assert source["uid"] == "arc-overview" and source["tags"] == ["arc", "standard"]  # source untouched


def test_library_panels_are_unlinked_in_place_including_inside_rows():
    copy = editable_copy(_standard(), "x", LIB)
    top = copy["panels"][1]
    assert "libraryPanel" not in top
    assert top["type"] == "bargauge" and top["targets"] == [{"expr": "cost"}]
    assert top["id"] == 2 and top["gridPos"] == {"x": 6, "y": 0, "w": 6, "h": 4}  # keeps its position
    nested = copy["panels"][2]["panels"][0]
    assert "libraryPanel" not in nested and nested["gridPos"]["y"] == 5
    assert LIB["arc-lib-cost"].get("id") is None  # library model not mutated


def test_unknown_library_panels_stay_linked():
    copy = editable_copy(_standard(), "x", {})
    assert copy["panels"][1]["libraryPanel"]["uid"] == "arc-lib-cost"


def test_product_cost_dashboard_is_provisioned_for_every_tenant_and_filters_by_product():
    from arc_tower.integrations.grafana import dashboards as std

    board = next(d for d in std.tenant_dashboards() if d["uid"] == std.AZURE_UID)
    targets = [t for panel in board["panels"] for t in panel.get("targets", [])]
    assert targets and all('arc_product_id=~"$product"' in t["expr"] for t in targets)
    assert all(t["expr"].count("(") == t["expr"].count(")") and t["expr"].count("{") == t["expr"].count("}")
               for t in targets)
    titles = {panel["title"] for panel in board["panels"]}
    assert {"Estimated cost (live)", "Billed by Azure", "Cost by service", "LLM calls", "Traces: every LLM call"} <= titles
    assert [v["name"] for v in board["templating"]["list"]] == ["product", "resource"]


def test_counts_are_exact_not_extrapolated():
    from arc_tower.integrations.grafana import queries as q

    expr = q.llm_calls('arc_product_id="p"', "6h")
    assert expr.startswith('sum((increase(arc_calls_total{arc_product_id="p",gen_ai_request_model!=""}[6h] anchored)')
    assert "first_over_time(" in expr                             # a series' first batch is counted too
    assert "or" in q.error_rate('arc_product_id="p"')            # 0 % rather than no data without errors
