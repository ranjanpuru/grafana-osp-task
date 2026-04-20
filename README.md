# grafana-osp-task

AWS Managed Grafana + CloudWatch observability, wired end-to-end via
CloudFormation, CFN custom resources, and GitHub Actions.

## What this does

1. **AWS Managed Grafana workspace** provisioned in CloudFormation with
   - `AWS_SSO` auth (IAM Identity Center, org-level)
   - CloudWatch as a managed data source
   - SNS as a notification destination
   - `PluginAdminEnabled: true` so plugins can be installed over the API
2. **CFN custom resource (Lambda)** that on every stack create/update:
   - waits for the workspace to reach ACTIVE
   - mints a short-lived ADMIN service-account token via the `grafana` API
   - installs the plugins listed in `PluginsToInstall` over the Grafana HTTP API
   - optionally assigns an IAM Identity Center group as workspace ADMIN
     (with retries while AMG finishes registering its managed application)
   - tears the service account down before returning
3. **CloudWatch observability**: a Lambda emits `osp/Demo` custom metrics on
   a 1-min schedule (latency/requests/errors per service), plus a CloudWatch
   dashboard reading those metrics.
4. **Grafana content as code** under `grafana/`
   - dashboard JSON
   - alert rules, SNS contact point, notification policy, notification templates
5. **GitHub Actions**
   - `deploy-infra.yml`  - CloudFormation for both stacks + Lambda packaging/upload
   - `sync-grafana.yml`  - pushes `grafana/**` into the workspace via HTTP API

```
             +------------------+        +-------------------+
 users +---->|  IAM Identity    |------->|  AWS Managed       |
             |  Center (SSO)    |        |  Grafana           |
             +------------------+        |  + plugins (CR)    |
                                         +---------+----------+
                                                   |
                                                   | CloudWatch DS
                                                   v
                       +-----------------+    +----+------+
                       |  osp-workload   |--> |CloudWatch |
                       |  Lambda (metrics)|    +-----+-----+
                       +-----------------+          |
                                                    | Grafana alert eval
                                                    v
                                              +-----+-----+
                                              |   SNS     |---> email / Slack / Lambda
                                              +-----------+
```

## Prerequisites

- An AWS account you own. Not a shared account - the stacks create a
  workspace + 1-min scheduled Lambda.
- IAM Identity Center at org level (AWS Organizations required). Create a
  group you want to grant workspace ADMIN - the group ID goes in
  `ADMIN_GROUP_ID` below.
- AWS CLI + creds for that account.
- An S3 bucket for the Lambda zip (`make bucket` creates one).
- Python 3.12 locally if you want to run `sync_grafana.py` or `make sync`.

## Configuration model

The repo only commits **non-sensitive defaults**. Account-specific values
(your IdC group id, your alert email) are not in git - they live in GitHub
Actions variables/secrets and are injected at deploy time.

In `cloudformation/parameters/dev.json`:
- `observability.Namespace`
- `grafana.WorkspaceName`, `grafana.GrafanaVersion`, `grafana.PluginsToInstall`

In GitHub repo variables (public to actions, not to clone-ers):
- `AWS_REGION`
- `ARTIFACTS_BUCKET`    - S3 bucket for the Lambda zip
- `ALERTS_EMAIL`        - subscribed to the SNS topic
- `ADMIN_GROUP_ID`      - IdC group assigned workspace ADMIN

In GitHub repo secrets:
- `AWS_DEPLOY_ROLE_ARN` - IAM role the workflow assumes via OIDC

## Deploy from GitHub Actions

1. Fork or clone this repo.
2. Create an IAM OIDC provider for `token.actions.githubusercontent.com`
   in your account.
3. Create a role trusting your repo with a subject condition like
   `repo:<owner>/<name>:ref:refs/heads/main`. Attach `PowerUserAccess` and
   `IAMFullAccess` for this sandbox; tighten for production.
4. Set the repo variables/secrets listed above.
5. Push to `main`. The two workflows trigger on:
   - `cloudformation/**` or `lambda/**`  -> `deploy-infra`
   - `grafana/**` or `scripts/sync_grafana.py`  -> `sync-grafana`

First full deploy is ~10 min (AMG workspace provisioning dominates).
Subsequent `sync-grafana` runs are ~30 sec.

## Deploy locally

```bash
export AWS_PROFILE=my-sandbox AWS_REGION=us-east-1
export ARTIFACTS_BUCKET=my-bucket
export ALERTS_EMAIL=me@example.com
export ADMIN_GROUP_ID=<idc group uuid>

make bucket
make deploy    # packages lambda, deploys both stacks
make sync      # pushes dashboards/alerts from ./grafana into the workspace
make outputs
```

## Adding a plugin

Add the Grafana plugin ID to `grafana.PluginsToInstall` in `dev.json` and
push. The custom resource re-runs on stack update. Already-installed
plugins return 409 (idempotent); unknown plugin IDs return 404 and are
logged-and-skipped so one typo does not roll back the stack.

## Adding a dashboard / alert rule

Drop the JSON / YAML under `grafana/` and push. The sync workflow is
idempotent: dashboards overwrite by UID, alert rules upsert by UID, contact
points are PUT-in-place so a policy referencing them stays valid.

## Grafana alert -> SNS

The SNS contact point uses the workspace's IAM role via SigV4 - no static
AWS creds in Grafana. The workspace role has `sns:Publish` scoped to the
topic ARN from the observability stack.

Subject and body are shared templates
(`grafana/provisioning/alerting/templates.yaml`). All rules emit consistent
bodies that downstream subscribers can parse.

## Tearing down

```bash
AWS_PROFILE=my-sandbox ./scripts/nuke.sh
```

Removes both CFN stacks, the Lambda log groups (CFN does not own them),
the artifacts bucket, the IdC group + user + instance, and the AWS
Organization. Safe to re-run.

## Known sharp edges

- **Plugin versions unpinned.** Custom resource installs the latest version
  for a given plugin ID. Pin by passing `version` in the request properties
  if needed - AMG ties plugin compatibility to the Grafana version, so
  leaving unpinned tracks AMG's channel.
- **Alert rule rename orphans.** If you rename an alert rule, either keep
  the UID or bump it and clean up the old one manually - `sync_grafana.py`
  does not prune rules it does not know about.
- **SNS resolve messages.** Grafana sends one SNS message on fire and one
  on resolve. If routing SNS -> Slack, use a transform lambda so Slack
  only gets the interesting transitions.
- **IdC SSO group assignment race.** After `AWS::Grafana::Workspace`
  reports ACTIVE, AMG asynchronously registers its Identity Center managed
  application. `grafana:UpdatePermissions` 403s with
  `Unable to update users in managed application` until that registration
  completes. The custom resource retries with backoff; the assignment is
  fail-soft so a slow registration does not fail the stack (log warns,
  you can re-run `UpdatePermissions` or assign in the console).
