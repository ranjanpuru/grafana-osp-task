SHELL := /bin/bash

AWS_REGION       ?= us-east-1
ENV              ?= dev
ARTIFACTS_BUCKET ?= $(USER)-grafana-osp-artifacts
ALERTS_EMAIL     ?=
ADMIN_GROUP_ID   ?=
STACK_OBS        := osp-observability
STACK_GRAFANA    := osp-grafana

PARAMS := cloudformation/parameters/$(ENV).json
LAMBDA_KEY := grafana-osp/local/grafana_custom_resource.zip

.PHONY: help bucket package deploy-obs deploy-grafana deploy sync outputs destroy lint

help:
	@echo "env knobs: AWS_REGION ARTIFACTS_BUCKET ALERTS_EMAIL ADMIN_GROUP_ID ENV"
	@echo "targets:"
	@echo "  make bucket          - create the artifacts bucket if missing"
	@echo "  make package         - zip the custom resource lambda + upload"
	@echo "  make deploy-obs      - deploy SNS + CW dashboard + demo workload"
	@echo "  make deploy-grafana  - deploy AMG workspace + bootstrap"
	@echo "  make deploy          - deploy-obs then deploy-grafana"
	@echo "  make sync            - push dashboards/alerts from ./grafana"
	@echo "  make outputs         - print stack outputs"
	@echo "  make destroy         - tear both stacks down"
	@echo "  make lint            - cfn-lint + yamllint"

bucket:
	aws s3api head-bucket --bucket $(ARTIFACTS_BUCKET) 2>/dev/null || \
	aws s3 mb s3://$(ARTIFACTS_BUCKET) --region $(AWS_REGION)

package: bucket
	rm -rf build && mkdir build
	cp lambda/grafana_custom_resource/index.py build/
	cd build && zip -9 -q grafana_custom_resource.zip index.py
	aws s3 cp build/grafana_custom_resource.zip s3://$(ARTIFACTS_BUCKET)/$(LAMBDA_KEY)

deploy-obs:
	aws cloudformation deploy \
		--stack-name $(STACK_OBS) \
		--template-file cloudformation/observability.yaml \
		--capabilities CAPABILITY_NAMED_IAM \
		--no-fail-on-empty-changeset \
		--parameter-overrides \
			Namespace=$$(jq -r .observability.Namespace $(PARAMS)) \
			AlertsEmail=$(ALERTS_EMAIL)

deploy-grafana: package
	$(eval SNS_ARN := $(shell aws cloudformation describe-stacks --stack-name $(STACK_OBS) --query "Stacks[0].Outputs[?OutputKey=='SnsTopicArn'].OutputValue" --output text))
	aws cloudformation deploy \
		--stack-name $(STACK_GRAFANA) \
		--template-file cloudformation/grafana-workspace.yaml \
		--capabilities CAPABILITY_NAMED_IAM \
		--no-fail-on-empty-changeset \
		--parameter-overrides \
			WorkspaceName=$$(jq -r .grafana.WorkspaceName $(PARAMS)) \
			GrafanaVersion=$$(jq -r .grafana.GrafanaVersion $(PARAMS)) \
			AdminGroupId=$(ADMIN_GROUP_ID) \
			PluginsToInstall=$$(jq -r .grafana.PluginsToInstall $(PARAMS)) \
			SnsTopicArn=$(SNS_ARN) \
			CustomResourceBucket=$(ARTIFACTS_BUCKET) \
			CustomResourceKey=$(LAMBDA_KEY)

deploy: deploy-obs deploy-grafana outputs

sync:
	$(eval WS  := $(shell aws cloudformation describe-stacks --stack-name $(STACK_GRAFANA) --query "Stacks[0].Outputs[?OutputKey=='WorkspaceId'].OutputValue" --output text))
	$(eval SNS := $(shell aws cloudformation describe-stacks --stack-name $(STACK_OBS)     --query "Stacks[0].Outputs[?OutputKey=='SnsTopicArn'].OutputValue" --output text))
	AWS_REGION=$(AWS_REGION) GRAFANA_WORKSPACE_ID=$(WS) SNS_TOPIC_ARN=$(SNS) \
		python3 scripts/sync_grafana.py

outputs:
	@echo "----- $(STACK_OBS) -----"
	@aws cloudformation describe-stacks --stack-name $(STACK_OBS)     --query "Stacks[0].Outputs" --output table
	@echo "----- $(STACK_GRAFANA) -----"
	@aws cloudformation describe-stacks --stack-name $(STACK_GRAFANA) --query "Stacks[0].Outputs" --output table

destroy:
	-aws cloudformation delete-stack --stack-name $(STACK_GRAFANA)
	aws cloudformation wait stack-delete-complete --stack-name $(STACK_GRAFANA) || true
	-aws cloudformation delete-stack --stack-name $(STACK_OBS)
	aws cloudformation wait stack-delete-complete --stack-name $(STACK_OBS) || true

lint:
	cfn-lint cloudformation/*.yaml
	yamllint -d "{extends: default, rules: {line-length: disable, truthy: disable, indentation: {spaces: 2}}}" \
		cloudformation/ grafana/ .github/
