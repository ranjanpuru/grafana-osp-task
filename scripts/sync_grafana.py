#!/usr/bin/env python3
# Pushes grafana/** into the AMG workspace via provisioning API.
# Runs from GH Actions on push to main; also works locally via `make sync`.
#
# env: AWS_REGION, GRAFANA_WORKSPACE_ID, SNS_TOPIC_ARN
# opt: GRAFANA_ENDPOINT, REPO_ROOT

import json
import logging
import os
import pathlib
import re
import string
import sys
import time
import urllib.error
import urllib.request

import boto3
import yaml

log = logging.getLogger("sync-grafana")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = pathlib.Path(os.environ.get("REPO_ROOT", pathlib.Path(__file__).resolve().parent.parent))
REGION = os.environ["AWS_REGION"]
WORKSPACE_ID = os.environ["GRAFANA_WORKSPACE_ID"]
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]

DS_NAME = "CloudWatch"
DS_UID = "cloudwatch-default"
FOLDER_TITLE = "OSP"


# Stdlib http wrapper; not worth pulling requests for 4 verbs.
class Api:
    def __init__(self, endpoint, token):
        self.base = endpoint if endpoint.startswith("http") else f"https://{endpoint}"
        self.hdr = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _do(self, method, path, body=None, ok=(200, 201, 202), extra_headers=None):
        headers = {**self.hdr, **(extra_headers or {})}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.status not in ok:
                    raise RuntimeError(f"{method} {path} -> {r.status}")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            # Allow callers to treat e.g. 404 as a non-error via the ok tuple.
            if e.code in ok:
                return {"_status": e.code}
            body_txt = e.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} -> {e.code} {body_txt}") from e

    def get(self, path, ok=(200, 404)):
        return self._do("GET", path, ok=ok)

    def post(self, path, body, ok=(200, 201, 202), extra_headers=None):
        return self._do("POST", path, body=body, ok=ok, extra_headers=extra_headers)

    def put(self, path, body, ok=(200, 202), extra_headers=None):
        return self._do("PUT", path, body=body, ok=ok, extra_headers=extra_headers)

    def delete(self, path, ok=(200, 202, 204, 404)):
        return self._do("DELETE", path, ok=ok)


# Always recreate so we don't leave long-lived admin creds lying around.
def mint_token():
    g = boto3.client("grafana", region_name=REGION)

    # Clean up leftovers from a prior failed run.
    for sa in g.list_workspace_service_accounts(workspaceId=WORKSPACE_ID).get("serviceAccounts", []):
        if sa["name"] == "gha-sync":
            try:
                g.delete_workspace_service_account(workspaceId=WORKSPACE_ID, serviceAccountId=sa["id"])
            except Exception as e:
                log.warning("could not clean old SA %s: %s", sa["id"], e)

    sa = g.create_workspace_service_account(
        workspaceId=WORKSPACE_ID, name="gha-sync", grafanaRole="ADMIN",
    )
    tok = g.create_workspace_service_account_token(
        workspaceId=WORKSPACE_ID, serviceAccountId=sa["id"],
        name=f"run-{int(time.time())}", secondsToLive=900,
    )

    endpoint = os.environ.get("GRAFANA_ENDPOINT") or \
        g.describe_workspace(workspaceId=WORKSPACE_ID)["workspace"]["endpoint"]

    return endpoint, tok["serviceAccountToken"]["key"], sa["id"]


def drop_token(sa_id):
    g = boto3.client("grafana", region_name=REGION)
    try:
        g.delete_workspace_service_account(workspaceId=WORKSPACE_ID, serviceAccountId=sa_id)
    except Exception:
        log.exception("could not delete sync SA")


def upsert_datasource(api):
    body = {
        "name": DS_NAME,
        "uid": DS_UID,
        "type": "cloudwatch",
        "access": "proxy",
        "jsonData": {
            "authType": "default",
            "defaultRegion": REGION,
        },
    }
    existing = api.get(f"/api/datasources/uid/{DS_UID}")
    if existing and "id" in existing:
        body["id"] = existing["id"]
        api.put(f"/api/datasources/uid/{DS_UID}", body)
        log.info("datasource %s updated", DS_UID)
    else:
        api.post("/api/datasources", body)
        log.info("datasource %s created", DS_UID)


def ensure_folder(api, title):
    for f in api.get("/api/folders") or []:
        if f.get("title") == title:
            return f["uid"]
    r = api.post("/api/folders", {"title": title})
    return r["uid"]


def render_vars(raw, mapping):
    # Cheap ${VAR} substitution on YAML source so it can reference runtime
    # values (SNS topic ARN, region, datasource uid). Not jinja-grade, fine.
    return string.Template(raw).safe_substitute(mapping)


def load_yaml_with_vars(path, mapping):
    raw = path.read_text()
    return yaml.safe_load(render_vars(raw, mapping))


def upsert_templates(api):
    path = ROOT / "grafana/provisioning/alerting/templates.yaml"
    doc = yaml.safe_load(path.read_text())
    for t in doc.get("templates", []):
        # PUT on /templates/{name} is upsert. X-Disable-Provenance keeps the
        # UI editable for break-glass edits.
        api.put(
            f"/api/v1/provisioning/templates/{t['name']}",
            {"name": t["name"], "template": t["template"]},
            extra_headers={"X-Disable-Provenance": "true"},
        )
        log.info("template %s upserted", t["name"])


def upsert_contact_points(api, vars_):
    # PUT by uid is upsert-in-place so we do not have to break the notification
    # policy reference to the contact point (DELETE would return 500 if any
    # policy still routes to it).
    path = ROOT / "grafana/provisioning/alerting/contact-points.yaml"
    doc = load_yaml_with_vars(path, vars_)
    existing_by_uid = {cp.get("uid"): cp for cp in (api.get("/api/v1/provisioning/contact-points") or []) if cp.get("uid")}

    for group in doc.get("contactPoints", []):
        for rx in group.get("receivers", []):
            uid = rx.get("uid") or f"{group['name']}-{rx['type']}"
            payload = {
                "uid": uid,
                "name": group["name"],
                "type": rx["type"],
                "settings": rx.get("settings", {}),
                "disableResolveMessage": rx.get("disableResolveMessage", False),
            }
            hdr = {"X-Disable-Provenance": "true"}
            if uid in existing_by_uid:
                api.put(f"/api/v1/provisioning/contact-points/{uid}", payload, extra_headers=hdr)
            else:
                api.post("/api/v1/provisioning/contact-points", payload, extra_headers=hdr)
            log.info("contact-point %s/%s upserted", group["name"], uid)


def upsert_policies(api):
    path = ROOT / "grafana/provisioning/alerting/notification-policies.yaml"
    doc = yaml.safe_load(path.read_text())
    # Whole policy tree goes in one PUT.
    tree = doc["policies"][0]
    api.put(
        "/api/v1/provisioning/policies", tree,
        extra_headers={"X-Disable-Provenance": "true"},
    )
    log.info("notification policies applied")


def upsert_rules(api, vars_, folder_uid):
    path = ROOT / "grafana/provisioning/alerting/rules.yaml"
    doc = load_yaml_with_vars(path, vars_)
    for group in doc.get("groups", []):
        for rule in group.get("rules", []):
            rule_body = {
                "uid": rule["uid"],
                "title": rule["title"],
                "condition": rule["condition"],
                "data": rule["data"],
                "folderUID": folder_uid,
                "ruleGroup": group["name"],
                "noDataState": rule.get("noDataState", "OK"),
                "execErrState": rule.get("execErrState", "Error"),
                "for": rule.get("for", "5m"),
                "annotations": rule.get("annotations", {}),
                "labels": rule.get("labels", {}),
                "orgID": 1,
            }
            existing = api.get(f"/api/v1/provisioning/alert-rules/{rule['uid']}")
            if existing and "uid" in existing:
                api.put(
                    f"/api/v1/provisioning/alert-rules/{rule['uid']}", rule_body,
                    extra_headers={"X-Disable-Provenance": "true"},
                )
            else:
                api.post(
                    "/api/v1/provisioning/alert-rules", rule_body,
                    extra_headers={"X-Disable-Provenance": "true"},
                )
            log.info("alert rule %s upserted", rule["uid"])

    # Set group eval interval (rule-level `for` stays per rule).
    for group in doc.get("groups", []):
        api.put(
            f"/api/v1/provisioning/folder/{folder_uid}/rule-groups/{group['name']}",
            {"title": group["name"], "interval": _to_seconds(group.get("interval", "1m"))},
            extra_headers={"X-Disable-Provenance": "true"},
        )


def _to_seconds(s):
    m = re.match(r"^(\d+)([smh])$", str(s))
    if not m:
        return int(s)
    n, u = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600}[u]


def upsert_dashboards(api, folder_uid):
    for fp in sorted((ROOT / "grafana/dashboards").glob("*.json")):
        dash = json.loads(fp.read_text())
        # Null out the numeric id so Grafana keys by uid on upsert.
        dash["id"] = None
        body = {
            "dashboard": dash,
            "folderUid": folder_uid,
            "overwrite": True,
            "message": "synced from github actions",
        }
        api.post("/api/dashboards/db", body)
        log.info("dashboard %s synced", dash.get("uid"))


# ---------------------------------------------------------------------------
def main():
    endpoint, token, sa_id = mint_token()
    try:
        api = Api(endpoint, token)

        vars_ = {
            "SNS_TOPIC_ARN": SNS_TOPIC_ARN,
            "AWS_REGION": REGION,
            "DS_CLOUDWATCH": DS_UID,
        }

        # Order matters: folder -> ds -> templates -> contacts -> policies
        # -> rules -> dashboards.
        folder_uid = ensure_folder(api, FOLDER_TITLE)
        upsert_datasource(api)
        upsert_templates(api)
        upsert_contact_points(api, vars_)
        upsert_policies(api)
        upsert_rules(api, vars_, folder_uid)
        upsert_dashboards(api, folder_uid)

        log.info("sync complete")
    finally:
        drop_token(sa_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("sync failed")
        sys.exit(1)
