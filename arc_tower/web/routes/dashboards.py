"""Embedded Grafana dashboards: standard (read-only) and the user's own (editable in place).

Standard dashboards are embedded in kiosk mode - they live in the ARC Standard folder, which
provisioning overwrites on every start, so edits there would silently vanish. "Customise" copies
one into My Dashboards instead; dashboards there are embedded with Grafana's full editor, so
users build and edit them without leaving ARC.
"""
from __future__ import annotations

import copy
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from arc_tower.integrations.grafana import GrafanaClient, GrafanaError
from arc_tower.integrations.grafana import dashboards as std

from .. import session as web_session
from ..deps import TenantContext, tenant_context
from ..grafana_proxy import grafana_url
from ..templating import render

log = logging.getLogger(__name__)
router = APIRouter()

PLATFORM_ORG = 1
# In-page views that aren't a dashboard: Grafana's own "new dashboard" and panel library screens.
_VIEWS = {"new", "library"}


def _org_for(ctx: TenantContext) -> int:
    if ctx.current and ctx.current.grafana_org_id:
        return ctx.current.grafana_org_id
    return PLATFORM_ORG if ctx.scope.all_tenants else (ctx.active[0].grafana_org_id if ctx.active else PLATFORM_ORG)


def _can_edit(ctx: TenantContext) -> bool:
    return ctx.can_edit or ctx.scope.is_platform_admin


def _standard(org: int) -> list[tuple[str, str]]:
    standard = [(std.OVERVIEW_UID, "Agent Overview"), (std.FINOPS_UID, "FinOps"),
                (std.QUALITY_UID, "Quality & Compliance"), (std.AZURE_UID, "Product Cost & Usage"),
                (std.LIBRARY_UID, "Panel Library")]
    if org == PLATFORM_ORG:
        standard.insert(0, (std.EXEC_UID, "Executive Overview"))
    return standard


@router.get("/dashboards")
def dashboards(request: Request, uid: str | None = None, product: str = "", view: str = "",
               ctx: TenantContext = Depends(tenant_context)):
    org = _org_for(ctx)
    standard = _standard(org)
    can_edit = _can_edit(ctx)
    mine: list[dict] = []
    try:
        mine = GrafanaClient().search_dashboards(org, [std.MINE_FOLDER_UID])
    except GrafanaError as exc:
        log.warning("dashboard search failed: %s", exc)
    view = view if view in _VIEWS and can_edit else ""
    selected = "" if view else (uid or standard[0][0])
    is_mine = selected in {d["uid"] for d in mine}
    product_var = {"var-product": product or None}
    if view == "new":
        embed = grafana_url("dashboard/new", org, folderUid=std.MINE_FOLDER_UID)
    elif view == "library":
        embed = grafana_url("library-panels", org)
    elif is_mine and can_edit:
        embed = grafana_url(f"d/{selected}", org, **product_var)          # full Grafana editor
    else:
        embed = grafana_url(f"d/{selected}", org, kiosk="", **product_var)  # read-only view
    return render(request, "dashboards.html", ctx=ctx, standard=standard, mine=mine, selected=selected,
                  view=view, embed=embed, editable=bool(view) or (is_mine and can_edit),
                  is_standard=selected in {u for u, _ in standard} and selected != std.LIBRARY_UID,
                  org=org, product=product, can_edit=can_edit)


def editable_copy(dashboard: dict, title: str, library_models: dict[str, dict]) -> dict:
    """A standalone copy of a (standard) dashboard for My Dashboards.

    Library panels are unlinked - replaced by their own panel model, keeping the position - so
    editing the copy can never write back to a shared ARC library panel (which provisioning
    would overwrite anyway). id/uid/version are dropped so Grafana creates a new dashboard."""
    model = copy.deepcopy(dashboard)
    for key in ("id", "uid", "version"):
        model.pop(key, None)
    model["title"] = title
    model["editable"] = True
    model["tags"] = [t for t in model.get("tags", []) if t != "standard"] + ["custom"]

    def unlink(panels: list[dict]) -> list[dict]:
        out = []
        for panel in panels:
            ref = panel.get("libraryPanel") or {}
            if ref.get("uid") in library_models:
                panel = {**copy.deepcopy(library_models[ref["uid"]]),
                         "id": panel.get("id"), "gridPos": panel.get("gridPos")}
                panel.pop("libraryPanel", None)
            if panel.get("panels"):  # collapsed rows hold their panels inside
                panel["panels"] = unlink(panel["panels"])
            out.append(panel)
        return out

    model["panels"] = unlink(model.get("panels", []))
    return model


@router.post("/dashboards/{uid}/customise", dependencies=[Depends(web_session.verify_csrf)])
def customise(request: Request, uid: str, product: str = "", ctx: TenantContext = Depends(tenant_context)):
    """Copy a dashboard into My Dashboards and open the copy in the in-page editor."""
    back = f"/dashboards?uid={uid}" + (f"&product={product}" if product else "")
    if not _can_edit(ctx):
        web_session.flash(request, "You need editor access to customise dashboards.", "error")
        return RedirectResponse(back, status_code=303)
    org = _org_for(ctx)
    grafana = GrafanaClient()
    try:
        source = grafana.get_dashboard(org, uid)
        if source is None:
            web_session.flash(request, "Dashboard not found.", "error")
            return RedirectResponse(back, status_code=303)
        refs = {p["libraryPanel"]["uid"] for p in _all_panels(source.get("panels", [])) if p.get("libraryPanel")}
        models = {ref: m for ref in refs if (m := grafana.get_library_panel_model(org, ref)) is not None}
        base = source.get("title", "Dashboard").replace("ARC · ", "")
        created = None
        for n in range(1, 21):  # first free title in the folder
            title = f"{base} (custom)" if n == 1 else f"{base} (custom {n})"
            created = grafana.create_dashboard(org, editable_copy(source, title, models), std.MINE_FOLDER_UID,
                                               f"customised copy of {uid}")
            if created:
                break
    except GrafanaError as exc:
        log.warning("customise %s failed: %s", uid, exc)
        web_session.flash(request, "Grafana is unavailable - try again in a moment.", "error")
        return RedirectResponse(back, status_code=303)
    if not created:
        web_session.flash(request, "Too many copies of this dashboard already - rename or delete one.", "error")
        return RedirectResponse(back, status_code=303)
    web_session.flash(request, f'Created "{title}" in My Dashboards - edit it below, then Save.')
    return RedirectResponse(f"/dashboards?uid={created['uid']}" + (f"&product={product}" if product else ""),
                            status_code=303)


def _all_panels(panels: list[dict]) -> list[dict]:
    out = []
    for panel in panels:
        out.append(panel)
        out.extend(_all_panels(panel.get("panels") or []))
    return out
