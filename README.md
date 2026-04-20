# grafana-osp-task

AWS Managed Grafana + CloudWatch observability, wired end-to-end via
CloudFormation, CFN custom resources, and GitHub Actions.

## What this does

1. **AWS Managed Grafana workspace** provisioned in CloudFormation with
   - `AWS_SSO` auth (IAM Identity Center)
   - CloudWatch as a managed data source
   - SNS as a notification destination
   - `PluginAdminEnabled: true` so plugins can be installed over the API
2. **CFN custom resource (Lambda)** that on every stack create/update:
   - mints a short-lived ADMIN service-account token via the `grafana` API
   - installs the plugins listed in `PluginsToInstall` over the Grafana HTTP API
   - (optional) assigns an IAM Identity Center group as workspace ADMIN
   - tears the service account down before returning
3. **CloudWatch observability**: a Lambda emits `osp/Demo` custom metrics on
   a 1-min schedule, plus a CloudWatch dashboard reading those metrics.
4. **Grafana content as code** under `grafana/`
   - dashboard JSON
   - alert rules, contact point (SNS), notification policy, templates
5. **GitHub Actions**
   - `deploy-infra.yml`  - CloudFormation for both stacks + Lambda packaging
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

- An AWS account you own (**do not deploy this into a shared/work account**).
  The infra is dirt-cheap but creates a workspace + a 1-min scheduled Lambda.
- AWS CLI + creds for that account.
- IAM Identity Center enabled in the same account + region, with at least
  one group you want to grant ADMIN (optional; you can skip SSO assignment
  and do it manually from the console).
- An S3 bucket for the Lambda zip (the Makefile creates one).
- Python 3.12 locally for running the sync script.

## One-time setup

```bash
cp cloudformation/parameters/dev.json cloudformation/parameters/dev.local.json
# edit dev.local.json:
#   grafana.AdminGroupId  - your Identity Center group ID (or leave "")
#   observability.AlertsEmail - email to subscribe to the SNS topic (optional)

export AWS_REGION=us-east-1
export ENV=dev
export ARTIFACTS_BUCKET=$USER-grafana-osp-artifacts
```

## Deploy (local)

```bash
make bucket
make deploy          # deploys observability stack, packages lambda, deploys grafana stack
make sync            # pushes dashboards/alerts from ./grafana into the workspace
make outputs
```

First deploy takes ~10 min (AMG workspace provisioning dominates).

## Deploy (GitHub Actions)

Set these on the repo:

- **Secrets**
  - `AWS_DEPLOY_ROLE_ARN` - IAM role ARN the workflow assumes via OIDC.
    Trust policy must allow `token.actions.githubusercontent.com` with
    `repo:<owner>/<repo>:ref:refs/heads/main`. Permissions:
    cloudformation:*, s3 on the artifacts bucket, iam pass-role on the roles
    the stacks create, grafana:* on the workspace, sts:AssumeRole.
- **Variables**
  - `AWS_REGION`
  - `ARTIFACTS_BUCKET`

Push to `main`:

- Changes under `cloudformation/**` or `lambda/**` trigger `deploy-infra`.
- Changes under `grafana/**` trigger `sync-grafana`.

## Adding a plugin

Add its Grafana plugin ID to `cloudformation/parameters/dev.json`
(`grafana.PluginsToInstall`) and push. The custom resource re-runs on stack
update and installs whatever is new. Already-installed plugins return 409
from the API, which the custom resource tolerates.

## Adding a dashboard / alert rule

Drop the JSON / YAML under `grafana/` and push. The sync workflow is
idempotent: dashboards overwrite by UID, alert rules upsert by UID, contact
points are cleaned + recreated so renames don't leave orphans.

## Grafana alert -> SNS

The SNS contact point uses the workspace's IAM role via SigV4, so there are
no static AWS creds inside Grafana. The role has `sns:Publish` scoped to
the topic ARN from the observability stack.

Subject and body are shared templates (`grafana/provisioning/alerting/templates.yaml`):
all rules emit consistent JSON bodies downstream subscribers can parse.

## Tearing down

```bash
make destroy
```

Order matters because the grafana stack references the SNS topic ARN as an
input parameter (not an import), so deleting in order grafana -> obs works.
If you imported instead, delete in the same order and wait for each.

## Known sharp edges

- **Plugin versions are not pinned.** The custom resource installs the
  latest version for a given plugin ID. Pin by passing `version` in the
  custom-resource request properties if this ever burns you (AMG ties
  plugin compatibility to the Grafana version). Left unpinned for now
  because this repo tracks AMG's supported Grafana channel.
- **IAM Identity Center Group ID is account-specific.** It's safe to commit
  because it's not a secret, but leave it blank in `dev.json` and set it in
  `dev.local.json` if you want to keep the repo template-able.
- **AMG provisioning API enforces uid uniqueness.** If you rename an alert
  rule, either keep the UID or bump the UID and the old one will be left
  orphaned in the folder (sync doesn't delete rules it doesn't know about).
  Clean up manually or add a prune step.
- **SNS contact point + resolve messages.** Grafana sends one SNS message
  on fire and one on resolve. If you're routing SNS -> Slack, use a
  transform lambda so Slack only gets the interesting transitions.
