"""Grafana HTTP API client (server-admin credentials; org chosen per call with X-Grafana-Org-Id)."""
from __future__ import annotations

import httpx

from arc_tower.core.config import GRAFANA_URL, get_settings
from arc_tower.utils.http import client

API = "/grafana/api"


class GrafanaError(RuntimeError):
    pass


class GrafanaClient:
    def __init__(self, base_url: str = GRAFANA_URL):
        settings = get_settings()
        self._http = client(base_url, auth=(settings.grafana_admin_user, settings.grafana_admin_password))

    # --- plumbing -------------------------------------------------------------

    def _call(self, method: str, path: str, org_id: int | None = None, ok: tuple[int, ...] = (),
              **kwargs) -> dict | list:
        headers = kwargs.pop("headers", {})
        if org_id is not None:
            headers["X-Grafana-Org-Id"] = str(org_id)
        try:
            resp = self._http.request(method, API + path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise GrafanaError(f"Grafana unreachable: {exc}") from exc
        if resp.status_code >= 400 and resp.status_code not in ok:
            raise GrafanaError(f"Grafana {method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code in ok and resp.status_code >= 400:
            return {"_status": resp.status_code}
        return resp.json() if resp.content else {}

    def healthy(self) -> bool:
        try:
            return self._http.get(API + "/health").status_code == 200
        except httpx.HTTPError:
            return False

    # --- organizations --------------------------------------------------------

    def find_org(self, name: str) -> int | None:
        body = self._call("GET", f"/orgs/name/{name}", ok=(404,))
        return None if body.get("_status") == 404 else body["id"]

    def ensure_org(self, name: str) -> int:
        existing = self.find_org(name)
        if existing:
            return existing
        return self._call("POST", "/orgs", json={"name": name})["orgId"]

    def rename_org(self, org_id: int, name: str) -> None:
        self._call("PUT", f"/orgs/{org_id}", json={"name": name})

    # --- users ----------------------------------------------------------------

    def find_user(self, login: str) -> dict | None:
        body = self._call("GET", "/users/lookup", params={"loginOrEmail": login}, ok=(404,))
        return None if body.get("_status") == 404 else body

    def ensure_user(self, login: str, name: str, password: str) -> int:
        user = self.find_user(login)
        if user:
            return user["id"]
        return self._call("POST", "/admin/users", json={
            "login": login, "email": login, "name": name or login, "password": password, "OrgId": 1,
        })["id"]

    def set_org_role(self, org_id: int, login: str, role: str) -> None:
        body = self._call("POST", f"/orgs/{org_id}/users", json={"loginOrEmail": login, "role": role}, ok=(409,))
        if body.get("_status") == 409:  # already a member: update the role
            user = self.find_user(login)
            if user:
                self._call("PATCH", f"/orgs/{org_id}/users/{user['id']}", json={"role": role})

    def remove_from_org(self, org_id: int, login: str) -> None:
        user = self.find_user(login)
        if user:
            self._call("DELETE", f"/orgs/{org_id}/users/{user['id']}", ok=(404,))

    def set_user_home_org(self, login: str, org_id: int) -> None:
        user = self.find_user(login)
        if user:
            self._call("POST", f"/users/{user['id']}/using/{org_id}", ok=(401, 403))

    def set_server_admin(self, login: str, is_admin: bool) -> None:
        user = self.find_user(login)
        if user:
            self._call("PUT", f"/admin/users/{user['id']}/permissions", json={"isGrafanaAdmin": is_admin})

    # --- data sources ---------------------------------------------------------

    def upsert_datasource(self, org_id: int, ds: dict) -> None:
        existing = self._call("GET", f"/datasources/uid/{ds['uid']}", org_id=org_id, ok=(404,))
        if existing.get("_status") == 404:
            self._call("POST", "/datasources", org_id=org_id, json=ds)
        else:
            self._call("PUT", f"/datasources/uid/{ds['uid']}", org_id=org_id, json={**ds, "id": existing["id"]})

    # --- folders and dashboards -----------------------------------------------

    def ensure_folder(self, org_id: int, uid: str, title: str) -> str:
        existing = self._call("GET", f"/folders/{uid}", org_id=org_id, ok=(404,))
        if existing.get("_status") == 404:
            self._call("POST", "/folders", org_id=org_id, json={"uid": uid, "title": title})
        return uid

    def set_folder_permissions(self, org_id: int, uid: str, items: list[dict]) -> None:
        self._call("POST", f"/folders/{uid}/permissions", org_id=org_id, json={"items": items})

    def upsert_dashboard(self, org_id: int, dashboard: dict, folder_uid: str) -> dict:
        return self._call("POST", "/dashboards/db", org_id=org_id, json={
            "dashboard": {**dashboard, "id": None}, "folderUid": folder_uid, "overwrite": True,
            "message": "provisioned by ARC Tower",
        })

    def get_dashboard(self, org_id: int, uid: str) -> dict | None:
        """The dashboard JSON model, or None if it doesn't exist in this org."""
        body = self._call("GET", f"/dashboards/uid/{uid}", org_id=org_id, ok=(404,))
        return None if body.get("_status") == 404 else body["dashboard"]

    def create_dashboard(self, org_id: int, dashboard: dict, folder_uid: str, message: str) -> dict | None:
        """Create a new dashboard - never overwrites. None if the title is already taken in the folder."""
        body = self._call("POST", "/dashboards/db", org_id=org_id, ok=(412,), json={
            "dashboard": {**dashboard, "id": None}, "folderUid": folder_uid, "overwrite": False, "message": message,
        })
        return None if body.get("_status") == 412 else body

    def get_library_panel_model(self, org_id: int, uid: str) -> dict | None:
        body = self._call("GET", f"/library-elements/{uid}", org_id=org_id, ok=(404,))
        return None if body.get("_status") == 404 else body["result"]["model"]

    def search_dashboards(self, org_id: int, folder_uids: list[str] | None = None) -> list[dict]:
        params: list[tuple[str, str]] = [("type", "dash-db")]
        params += [("folderUIDs", uid) for uid in folder_uids or []]
        return self._call("GET", "/search", org_id=org_id, params=params)

    def upsert_library_panel(self, org_id: int, uid: str, name: str, model: dict, folder_uid: str) -> None:
        existing = self._call("GET", f"/library-elements/{uid}", org_id=org_id, ok=(404,))
        payload = {"uid": uid, "name": name, "model": model, "kind": 1, "folderUid": folder_uid}
        if existing.get("_status") == 404:
            self._call("POST", "/library-elements", org_id=org_id, json=payload)
        else:
            version = existing["result"]["version"]
            self._call("PATCH", f"/library-elements/{uid}", org_id=org_id, json={**payload, "version": version})

    # --- alerting -------------------------------------------------------------

    def upsert_contact_point(self, org_id: int, uid: str, name: str, cp_type: str, settings: dict) -> None:
        payload = {"uid": uid, "name": name, "type": cp_type, "settings": settings, "disableResolveMessage": False}
        existing = [cp for cp in self._call("GET", "/v1/provisioning/contact-points", org_id=org_id)
                    if cp.get("uid") == uid]
        headers = {"X-Disable-Provenance": "true"}
        if existing:
            self._call("PUT", f"/v1/provisioning/contact-points/{uid}", org_id=org_id, json=payload, headers=headers)
        else:
            self._call("POST", "/v1/provisioning/contact-points", org_id=org_id, json=payload, headers=headers)

    def set_default_receiver(self, org_id: int, receiver: str) -> None:
        self._call("PUT", "/v1/provisioning/policies", org_id=org_id, headers={"X-Disable-Provenance": "true"},
                   json={"receiver": receiver, "group_by": ["grafana_folder", "alertname"]})

    def upsert_alert_rule(self, org_id: int, rule: dict) -> None:
        headers = {"X-Disable-Provenance": "true"}
        existing = self._call("GET", f"/v1/provisioning/alert-rules/{rule['uid']}", org_id=org_id, ok=(404,))
        if existing.get("_status") == 404:
            self._call("POST", "/v1/provisioning/alert-rules", org_id=org_id, json=rule, headers=headers)
        else:
            self._call("PUT", f"/v1/provisioning/alert-rules/{rule['uid']}", org_id=org_id, json=rule,
                       headers=headers)

    def delete_alert_rule(self, org_id: int, uid: str) -> None:
        self._call("DELETE", f"/v1/provisioning/alert-rules/{uid}", org_id=org_id, ok=(404,),
                   headers={"X-Disable-Provenance": "true"})

    def firing_alerts(self, org_id: int) -> list[dict]:
        body = self._call("GET", "/prometheus/grafana/api/v1/alerts", org_id=org_id)
        return body.get("data", {}).get("alerts", [])
