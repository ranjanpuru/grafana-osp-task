#!/usr/bin/env bash
# Nuke every AWS resource this repo created. Safe to run repeatedly.
# Usage: AWS_PROFILE=adventure ./scripts/nuke.sh

set -u

: "${AWS_PROFILE:?AWS_PROFILE must be set}"
AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_GRAFANA="${STACK_GRAFANA:-osp-grafana}"
STACK_OBS="${STACK_OBS:-osp-observability}"
ARTIFACTS_BUCKET="${ARTIFACTS_BUCKET:-${USER}-grafana-osp-artifacts}"
IDC_GROUP_NAME="${IDC_GROUP_NAME:-GrafanaAdmins}"
IDC_USER_NAME="${IDC_USER_NAME:-puru}"

echo ">> account: $(aws sts get-caller-identity --query Account --output text)"
echo ">> region : $AWS_REGION"
echo ">> profile: $AWS_PROFILE"

# 1) Grafana workspace stack first (it depends on the SNS topic from obs).
if aws cloudformation describe-stacks --stack-name "$STACK_GRAFANA" --region "$AWS_REGION" >/dev/null 2>&1; then
    echo ">> deleting $STACK_GRAFANA"
    aws cloudformation delete-stack --stack-name "$STACK_GRAFANA" --region "$AWS_REGION"
    aws cloudformation wait stack-delete-complete --stack-name "$STACK_GRAFANA" --region "$AWS_REGION" || true
fi

# 2) Observability stack.
if aws cloudformation describe-stacks --stack-name "$STACK_OBS" --region "$AWS_REGION" >/dev/null 2>&1; then
    echo ">> deleting $STACK_OBS"
    aws cloudformation delete-stack --stack-name "$STACK_OBS" --region "$AWS_REGION"
    aws cloudformation wait stack-delete-complete --stack-name "$STACK_OBS" --region "$AWS_REGION" || true
fi

# 3) CloudWatch log groups the lambdas create lazily (CFN does not own them).
for lg in "/aws/lambda/osp-workload" "/aws/lambda/osp-grafana-custom-resource"; do
    if aws logs describe-log-groups --log-group-name-prefix "$lg" --region "$AWS_REGION" --query 'logGroups[0].logGroupName' --output text 2>/dev/null | grep -q "$lg"; then
        echo ">> deleting log group $lg"
        aws logs delete-log-group --log-group-name "$lg" --region "$AWS_REGION" || true
    fi
done

# 4) Artifacts bucket: empty all versions + delete markers, then drop.
if aws s3api head-bucket --bucket "$ARTIFACTS_BUCKET" 2>/dev/null; then
    echo ">> emptying s3://$ARTIFACTS_BUCKET"
    aws s3 rm "s3://$ARTIFACTS_BUCKET" --recursive >/dev/null || true
    # Versioned buckets need explicit version cleanup. Safe no-op if unversioned.
    aws s3api list-object-versions --bucket "$ARTIFACTS_BUCKET" --output json 2>/dev/null \
      | python3 -c '
import json, subprocess, sys
d = json.load(sys.stdin) or {}
for k in ("Versions","DeleteMarkers"):
    for o in d.get(k) or []:
        subprocess.run(["aws","s3api","delete-object","--bucket",sys.argv[1],"--key",o["Key"],"--version-id",o["VersionId"]], check=False, stdout=subprocess.DEVNULL)
' "$ARTIFACTS_BUCKET" || true
    aws s3api delete-bucket --bucket "$ARTIFACTS_BUCKET" --region "$AWS_REGION" || true
fi

# 5) IAM Identity Center group + user. Instance itself stays (free + the only
# way to disable it is via the console anyway). Re-running is idempotent.
IDSTORE=$(aws sso-admin list-instances --region "$AWS_REGION" --query 'Instances[0].IdentityStoreId' --output text 2>/dev/null || echo "")
if [[ -n "$IDSTORE" && "$IDSTORE" != "None" ]]; then
    GID=$(aws identitystore list-groups --identity-store-id "$IDSTORE" --region "$AWS_REGION" \
           --query "Groups[?DisplayName=='$IDC_GROUP_NAME'].GroupId" --output text 2>/dev/null || echo "")
    UID_=$(aws identitystore list-users  --identity-store-id "$IDSTORE" --region "$AWS_REGION" \
           --query "Users[?UserName=='$IDC_USER_NAME'].UserId" --output text 2>/dev/null || echo "")

    if [[ -n "$GID" && -n "$UID_" && "$GID" != "None" && "$UID_" != "None" ]]; then
        MID=$(aws identitystore get-group-membership-id --identity-store-id "$IDSTORE" \
               --group-id "$GID" --member-id "UserId=$UID_" --region "$AWS_REGION" \
               --query MembershipId --output text 2>/dev/null || echo "")
        if [[ -n "$MID" && "$MID" != "None" ]]; then
            echo ">> removing user $IDC_USER_NAME from $IDC_GROUP_NAME"
            aws identitystore delete-group-membership --identity-store-id "$IDSTORE" \
                --membership-id "$MID" --region "$AWS_REGION" || true
        fi
    fi

    if [[ -n "$GID" && "$GID" != "None" ]]; then
        echo ">> deleting group $IDC_GROUP_NAME"
        aws identitystore delete-group --identity-store-id "$IDSTORE" --group-id "$GID" --region "$AWS_REGION" || true
    fi
    if [[ -n "$UID_" && "$UID_" != "None" ]]; then
        echo ">> deleting user $IDC_USER_NAME"
        aws identitystore delete-user --identity-store-id "$IDSTORE" --user-id "$UID_" --region "$AWS_REGION" || true
    fi
fi

# 6) IAM Identity Center instance. Org-level instances can be deleted via the
#    management account (requires all assignments + permission sets gone first).
INSTANCE_ARN=$(aws sso-admin list-instances --region "$AWS_REGION" --query 'Instances[0].InstanceArn' --output text 2>/dev/null || echo "")
if [[ -n "$INSTANCE_ARN" && "$INSTANCE_ARN" != "None" ]]; then
    echo ">> attempting to delete IdC instance $INSTANCE_ARN (will fail if assignments remain)"
    aws sso-admin delete-instance --instance-arn "$INSTANCE_ARN" --region "$AWS_REGION" 2>/dev/null || \
      echo "   skipped - clean up permission sets / account assignments from console first"
fi

# 7) AWS Organization. Only works if this is a single-account org (no member
#    accounts). Harmless if there is no org.
if aws organizations describe-organization >/dev/null 2>&1; then
    echo ">> dissolving AWS Organization (single-account only)"
    aws organizations delete-organization 2>/dev/null || \
      echo "   skipped - remove member accounts first"
fi

echo ">> done. nothing billable should remain for this repo."
