"""Trace explorer backed by Langfuse."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from arc_tower.services import access, traces
from arc_tower.services.access import Scope

from ..deps import TenantContext, current_scope, get_db, tenant_context
from ..templating import render

router = APIRouter()


@router.get("/traces")
def trace_list(request: Request, product: int | None = None, cursor: str = "", user: str = "", session_id: str = "",
               db: Session = Depends(get_db), scope: Scope = Depends(current_scope),
               ctx: TenantContext = Depends(tenant_context)):
    choices = [p for t in ctx.active for p in t.products]
    selected = access.get_product(db, scope, product) if product else (choices[0] if choices else None)
    result = traces.list_traces(selected, cursor=cursor or None, user_id=user or None,
                                session_id=session_id or None) if selected else None
    return render(request, "traces.html", ctx=ctx, products=choices, selected=selected, result=result,
                  user=user, session_id=session_id, first_page=not cursor)
