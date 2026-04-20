"""
CFN custom resource for the AMG workspace.

Runs on Create/Update:
  1. waits for the workspace to reach ACTIVE
  2. mints a short-lived ADMIN service-account token (grafana control plane)
  3. installs the listed plugins via /api/plugins/{id}/install
  4. optionally assigns an IAM Identity Center group as workspace ADMIN
  5. deletes the service account before returning

Delete is a no-op: the workspace itself is going away.

Deps: boto3 + stdlib only. Keeps the zip tiny, no layers needed.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

grafana = boto3.client("grafana")

SA_NAME = "cfn-bootstrap"
TOKEN_TTL = 900  # 15 min; plenty for plugin install + perm update.


def _respond(event, status, data=None, reason=""):
    body = json.dumps({
        "Status": status,
        "Reason": reason or "see cloudwatch logs",
        "PhysicalResourceId": event.get("PhysicalResourceId") or event["LogicalResourceId"],
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }).encode()
    req = urllib.request.Request(
        event["ResponseURL"], data=body, method="PUT",
        headers={"content-type": "", "content-length": str(len(body))},
    )
    urllib.request.urlopen(req, timeout=30)


class GrafanaAPI:
    def __init__(self, endpoint, token):
        self.base = endpoint if endpoint.startswith("http") else f"https://{endpoint}"
        self.hdr = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _call(self, method, path, body=None, tolerate=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=self.hdr)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if not raw:
                    return {}
                parsed = json.loads(raw)
                # Plugin install can return `null`; normalise so callers can
                # always `.get()` safely.
                return parsed if isinstance(parsed, dict) else {"_body": parsed}
        except urllib.error.HTTPError as e:
            if tolerate and e.code in tolerate:
                log.info("tolerated %s %s -> %s", method, path, e.code)
                return {"status": e.code}
            raise RuntimeError(f"grafana {method} {path} -> {e.code}: {e.read()!r}") from e

    def whoami(self):
        # /api/org is the cheapest authenticated GET; if the SA token is not
        # yet usable this 401s and we abort before making changes.
        return self._call("GET", "/api/org")

    def install_plugin(self, plugin_id, version=None):
        # 409 = already installed at/above this version (idempotent).
        # 404 = plugin id does not exist on the catalog (typo / deprecated /
        #       merged into core). Log-and-continue so one bad id does not
        #       rollback the whole workspace.
        body = {"version": version} if version else {}
        return self._call(
            "POST", f"/api/plugins/{plugin_id}/install", body=body,
            tolerate={409, 404},
        )


def _mint_token(workspace_id):
    sa = grafana.create_workspace_service_account(
        workspaceId=workspace_id, name=SA_NAME, grafanaRole="ADMIN",
    )
    tok = grafana.create_workspace_service_account_token(
        workspaceId=workspace_id,
        serviceAccountId=sa["id"],
        name=f"cfn-{int(time.time())}",
        secondsToLive=TOKEN_TTL,
    )
    return sa["id"], tok["serviceAccountToken"]["key"]


def _drop_sa(workspace_id, sa_id):
    try:
        grafana.delete_workspace_service_account(
            workspaceId=workspace_id, serviceAccountId=sa_id,
        )
    except Exception:
        # Non-fatal; token expires in 15min anyway.
        log.exception("could not delete SA %s", sa_id)


def _assign_admin_group(workspace_id, group_id, attempts=12, delay=10):
    # After workspace creation with AWS_SSO auth, AMG asynchronously registers
    # a managed application in Identity Center. UpdatePermissions fails with
    # AccessDeniedException (Unable to update users in managed application)
    # until that registration completes. Retry for up to ~2 min.
    last = None
    for i in range(attempts):
        try:
            grafana.update_permissions(
                workspaceId=workspace_id,
                updateInstructionBatch=[{
                    "action": "ADD",
                    "role": "ADMIN",
                    "users": [{"id": group_id, "type": "SSO_GROUP"}],
                }],
            )
            return
        except grafana.exceptions.AccessDeniedException as e:
            last = e
            log.info("UpdatePermissions not ready yet (attempt %d/%d): %s", i + 1, attempts, e)
            time.sleep(delay)
    raise RuntimeError(f"UpdatePermissions did not become available: {last}")


def _wait_active(workspace_id, attempts=30, delay=10):
    for _ in range(attempts):
        w = grafana.describe_workspace(workspaceId=workspace_id)["workspace"]
        if w["status"] == "ACTIVE":
            return
        time.sleep(delay)
    raise TimeoutError(f"workspace {workspace_id} never reached ACTIVE")


def handler(event, context):
    log.info("event: %s", json.dumps({k: v for k, v in event.items() if k != "ResponseURL"}))
    rtype = event["RequestType"]
    props = event.get("ResourceProperties", {})

    if rtype == "Delete":
        _respond(event, "SUCCESS", reason="delete no-op")
        return

    try:
        workspace_id = props["WorkspaceId"]
        endpoint = props["WorkspaceEndpoint"]
        plugins = [p.strip() for p in props.get("Plugins", []) if p and p.strip()]
        assign_admin = str(props.get("AssignAdmin", "false")).lower() == "true"
        admin_group_id = props.get("AdminGroupId") or ""

        _wait_active(workspace_id)

        sa_id, token = _mint_token(workspace_id)
        # AMG workspace is "ACTIVE" a few seconds before the Grafana process
        # will accept service-account tokens. Retry /api/org briefly before
        # the actual work.
        try:
            g = GrafanaAPI(endpoint, token)
            last = None
            for i in range(6):
                try:
                    g.whoami()
                    break
                except Exception as e:
                    last = e
                    time.sleep(5)
            else:
                raise RuntimeError(f"token not usable after 30s: {last}")

            installed = []
            skipped = []
            for pid in plugins:
                try:
                    log.info("installing plugin %s", pid)
                    r = g.install_plugin(pid)
                    if r.get("status") == 404:
                        log.warning("plugin %s not found on catalog, skipping", pid)
                        skipped.append(pid)
                    else:
                        installed.append(pid)
                except Exception as pe:
                    # Anything else (5xx, timeout): log + continue, do not
                    # fail the stack over a transient plugin issue.
                    log.exception("plugin %s install failed: %s", pid, pe)
                    skipped.append(pid)

            sso_status = "skipped"
            if assign_admin and admin_group_id:
                log.info("assigning SSO group %s as ADMIN", admin_group_id)
                try:
                    _assign_admin_group(workspace_id, admin_group_id)
                    sso_status = "assigned"
                except Exception as ae:
                    # Do not block the stack on a transient AMG/IdC race.
                    # The user can re-run `UpdatePermissions` or assign in
                    # the console; the workspace is otherwise healthy.
                    log.exception("SSO admin assignment failed, continuing: %s", ae)
                    sso_status = f"failed: {str(ae)[:200]}"
        finally:
            _drop_sa(workspace_id, sa_id)

        _respond(event, "SUCCESS", data={
            "InstalledPlugins": ",".join(installed),
            "SkippedPlugins": ",".join(skipped),
            "AdminGroupAssignment": sso_status,
        })
    except Exception as e:
        log.exception("bootstrap failed")
        # Truncate for CFN reason field (max ~1024 chars).
        _respond(event, "FAILED", reason=str(e)[:1024])
