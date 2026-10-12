"""Registry services against a real (temporary) SQLite database. Grafana is not needed:
provisioning is best-effort and failures are only logged."""
from arc_tower.core.constants import Role
from arc_tower.db.session import session_scope
from arc_tower.otlp.enrich import EnrichStats
from arc_tower.services import access, coverage, products, tenants, users
from arc_tower.utils.timeutil import utcnow


def test_onboarding_flow_and_key_resolution():
    with session_scope() as db:
        tenant = tenants.create_tenant(db, "test", "Test Bank", "test-bank")
        product = products.create_product(db, "test", tenant, "Loan Bot", "otel_custom")
        issued = products.issue_ingest_key(db, "test", product)
        assert products.product_for_key(db, issued.key).id == product.id
        assert f"x-arc-key={issued.key}" in products.env_block(product, issued.key)
        rotated = products.issue_ingest_key(db, "test", product, revoke_existing=True)
        assert products.product_for_key(db, issued.key) is None      # old key revoked
        assert products.product_for_key(db, rotated.key).id == product.id


def test_access_scope_isolates_tenants():
    with session_scope() as db:
        a = tenants.create_tenant(db, "test", "Alpha", "alpha")
        tenants.create_tenant(db, "test", "Beta", "beta")
        viewer = users.create_user(db, "test", "viewer@alpha.test", password="x")
        users.set_membership(db, "test", viewer, a, Role.VIEWER)
        scope = access.scope_for(viewer)
        visible = [t.slug for t in access.visible_tenants(db, scope)]
        assert visible == ["alpha"]
        assert scope.can_view(a.id) and not scope.can_edit(a.id)


def test_coverage_report_scores_fields():
    with session_scope() as db:
        tenant = tenants.create_tenant(db, "test", "Cov", "cov")
        product = products.create_product(db, "test", tenant, "Cov Bot", "otel_custom")
        stats = EnrichStats(spans=4, llm_spans=3)
        stats.span_classes.update({"model_call": 2, "genai": 3, "root": 1})
        stats.field_counts.update({"gen_ai.request.model": 2, "gen_ai.usage.input_tokens": 1})
        coverage.merge(product, stats)
        products.mark_seen(product, utcnow())
        rows, score = coverage.report(product)
        by_field = {r.field: r for r in rows}
        assert by_field["gen_ai.request.model"].percent == 100.0
        assert by_field["gen_ai.usage.input_tokens"].status == "partial"
        assert 0 < score < 100
        assert product.status == "live"
