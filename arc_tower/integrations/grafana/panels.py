"""Small builders for Grafana panel / variable JSON."""
from __future__ import annotations

PROM = {"type": "prometheus", "uid": "arc-prom"}
LOKI = {"type": "loki", "uid": "arc-loki"}


def _target(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict:
    target = {"refId": ref, "expr": expr, "legendFormat": legend or "__auto", "datasource": PROM}
    if instant:
        target.update(instant=True, range=False)
    return target


def stat(title: str, expr: str, unit: str = "short", grid: tuple[int, int, int, int] = (0, 0, 4, 4),
         decimals: int | None = None, thresholds: list[tuple[str, float | None]] | None = None,
         description: str = "", no_value: str = "0") -> dict:
    """no_value: shown when the query returns nothing (a count with no events is 0, not "No data")."""
    steps = [{"color": color, "value": value} for color, value in (thresholds or [("blue", None)])]
    defaults: dict = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps},
                      "color": {"mode": "thresholds"}, "noValue": no_value}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat", "title": title, "description": description, "datasource": PROM,
        "gridPos": _grid(grid), "targets": [_target(expr, instant=True)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": "value", "justifyMode": "auto"},
    }


def timeseries(title: str, targets: list[tuple[str, str]], unit: str = "short",
               grid: tuple[int, int, int, int] = (0, 0, 12, 8), stack: bool = False,
               draw: str = "line", description: str = "") -> dict:
    refs = "ABCDEFGH"
    return {
        "type": "timeseries", "title": title, "description": description, "datasource": PROM,
        "gridPos": _grid(grid),
        "targets": [_target(expr, legend, refs[i]) for i, (expr, legend) in enumerate(targets)],
        "fieldConfig": {"defaults": {
            "unit": unit,
            "custom": {"drawStyle": draw, "lineWidth": 2, "fillOpacity": 12 if not stack else 60,
                       "showPoints": "never", "spanNulls": True,
                       "stacking": {"mode": "normal" if stack else "none", "group": "A"}},
        }, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def bars(title: str, expr: str, legend: str, unit: str = "short",
         grid: tuple[int, int, int, int] = (0, 0, 12, 8), description: str = "") -> dict:
    """Horizontal bar gauge of an instant query: ranking by product / agent / model."""
    return {
        "type": "bargauge", "title": title, "description": description, "datasource": PROM,
        "gridPos": _grid(grid), "targets": [_target(expr, instant=True)],
        "fieldConfig": {"defaults": {"unit": unit, "min": 0, "color": {"mode": "palette-classic"}},
                        "overrides": []},
        "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "valueMode": "color", "namePlacement": "left", "sizing": "auto"},
    }


def table(title: str, values: list[tuple[str, str]], columns: dict[str, str], unit: str = "short",
          decimals: int | None = None, grid: tuple[int, int, int, int] = (0, 0, 12, 8), description: str = "") -> dict:
    """Instant queries as one table, a row per label set. values: [(expr, column header)], merged on the
    labels; columns maps label -> header (shown in this order, before the values). Sorted by the first
    value, high to low, with a total row."""
    refs = "ABCDEFGH"
    targets = []
    for i, (expr, _) in enumerate(values):
        target = _target(expr, ref=refs[i], instant=True)
        target["format"] = "table"
        targets.append(target)
    # A single query's value column is "Value"; merged queries get "Value #A", "Value #B", ...
    raw = ["Value"] if len(values) == 1 else [f"Value #{refs[i]}" for i in range(len(values))]
    headers = [header for _, header in values]
    value_props = [{"id": "unit", "value": unit}] + ([{"id": "decimals", "value": decimals}] if decimals is not None else [])
    transformations = [{"id": "merge", "options": {}}] if len(values) > 1 else []
    transformations += [
        {"id": "filterFieldsByName", "options": {"include": {"names": [*columns, *raw]}}},        # only these
        {"id": "organize", "options": {"indexByName": {name: i for i, name in enumerate([*columns, *raw])},
                                       "renameByName": {**columns, **dict(zip(raw, headers))}}}]
    return {
        "type": "table", "title": title, "description": description, "datasource": PROM, "gridPos": _grid(grid),
        "targets": targets, "transformations": transformations,
        "fieldConfig": {"defaults": {"unit": "short", "custom": {"align": "auto"}},
                        "overrides": [{"matcher": {"id": "byName", "options": h}, "properties": value_props}
                                      for h in headers]},
        "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": headers[0], "desc": True}],
                    "footer": {"show": True, "reducer": ["sum"], "fields": headers}},
    }


def pie(title: str, expr: str, legend: str, unit: str = "short",
        grid: tuple[int, int, int, int] = (0, 0, 8, 8)) -> dict:
    return {
        "type": "piechart", "title": title, "datasource": PROM, "gridPos": _grid(grid),
        "targets": [_target(expr, legend, instant=True)],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"pieType": "donut", "legend": {"displayMode": "table", "placement": "right",
                                                   "values": ["value", "percent"], "showLegend": True},
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
    }


def logs(title: str, expr: str, grid: tuple[int, int, int, int] = (0, 0, 24, 9)) -> dict:
    return {
        "type": "logs", "title": title, "datasource": LOKI, "gridPos": _grid(grid),
        "targets": [{"refId": "A", "expr": expr, "datasource": LOKI, "queryType": "range"}],
        "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                    "enableLogDetails": True, "dedupStrategy": "none"},
    }


def row(title: str, y: int) -> dict:
    return {"type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "panels": []}


def query_var(name: str, label: str, query: str, include_all: bool = True, multi: bool = True) -> dict:
    return {
        "name": name, "label": label, "type": "query", "datasource": PROM,
        "query": {"query": query, "refId": f"var-{name}", "qryType": 1}, "definition": query,
        "refresh": 2, "includeAll": include_all, "multi": multi, "allValue": ".*", "sort": 1,
        "current": {"text": ["All"], "value": ["$__all"]} if include_all else {},
    }


def _grid(grid: tuple[int, int, int, int]) -> dict:
    x, y, w, h = grid
    return {"x": x, "y": y, "w": w, "h": h}


def dashboard(uid: str, title: str, panels: list[dict], variables: list[dict], tags: list[str],
              description: str = "", time_from: str = "now-6h") -> dict:
    for i, panel in enumerate(panels, start=1):
        panel["id"] = i
    return {
        "uid": uid, "title": title, "description": description, "tags": tags, "editable": True,
        "schemaVersion": 39, "time": {"from": time_from, "to": "now"}, "refresh": "30s",
        "timezone": "browser", "graphTooltip": 1, "panels": panels,
        "templating": {"list": variables}, "annotations": {"list": []},
    }
