"""Static least-privilege regression for the AgentCore execution role (#482).

Rebuilds the authorization expectation from the reviewed callers upward,
applying the IAM least-privilege best practice
(https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html#grant-least-privilege).
The role must:

* attach NO account-wide AWS managed read policies (ManagedPolicyArns);
* contain no ``*`` action, no ``cloudformation:*`` / ``cloudcontrol:*`` surface,
  and no log-CONTENT read action anywhere (no runtime path reads log content);
* keep log reads off an account-wide ``log-group:*`` resource except a single
  metadata-only ``logs:DescribeLogGroups`` allowance;
* pull ECR images only from the runtime repository, never DescribeRepositories/
  ListImages;
* use ``Resource: '*'`` only for an explicit allowlist of actions with no
  scopable resource type, a global endpoint, or dynamic account-owned names;
* carry no provider-WRITE verb except the scoped AgentCore memory/event writes,
  telemetry writes, and the DynamoDB snapshot PutItem; and
* grant EXACTLY the reviewed inventory below — every entry carries a caller
  comment, so any newly added action fails this test until it is reviewed.

The template is parsed with the repository's intrinsic-tolerant safe loader so
``!Sub``/``!GetAtt`` collapse to plain strings. Every granted action maps to a
concrete reviewed caller (see SECURITY.md, "AgentCore Execution Role (Least
Privilege)").
"""

# Standard library
import json
import pathlib
import sys

# Third-party packages
import pytest

# Local modules
from _cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit

PROJECT_ROOT = pathlib.Path(__file__).parents[3]
BASE_TEMPLATE = PROJECT_ROOT / "infrastructure/cloudformation/01-base-infrastructure.yaml"

# Inline-deployed templates (aws cloudformation deploy --template-file, no S3)
# must stay under the 51,200-byte inline cap.
INLINE_TEMPLATE_BYTE_LIMIT = 51_200

# The complete reviewed action set for AgentCoreExecutionRole. Each action maps
# to a concrete caller (see SECURITY.md). Adding a provider action without a
# reviewed caller must break this test.
REVIEWED_ACTIONS: dict[str, str] = {
    # Strands Bedrock model (models/cached_bedrock.py)
    "bedrock:InvokeModel": "Strands Bedrock model invocation",
    "bedrock:InvokeModelWithResponseStream": "Strands Bedrock streaming invocation",
    # kb_tools.py bedrock-agent-runtime.retrieve()
    "bedrock:Retrieve": "kb_projection direct Knowledge Base retrieve",
    # model Converse guardrail config (cached_bedrock.py)
    "bedrock:ApplyGuardrail": "Guardrails applied on model invocation",
    # optimized_prompts.py bedrock-agent.get_prompt()
    "bedrock:GetPrompt": "Bedrock Prompt Management GetPrompt",
    # Strands AgentCoreMemorySessionManager + semantic_memory.py
    "bedrock-agentcore:CreateEvent": "STM session event write (session manager)",
    "bedrock-agentcore:GetEvent": "STM session event read (session manager)",
    "bedrock-agentcore:ListEvents": "STM session event list (session manager)",
    "bedrock-agentcore:DeleteEvent": "STM session event prune (session manager)",
    "bedrock-agentcore:RetrieveMemoryRecords": "LTM retrieve (retrieve_customer_context)",
    "bedrock-agentcore:BatchCreateMemoryRecords": "LTM write (semantic_memory.py)",
    # cost_report.py owned deterministic report + Billing MCP forecast tool
    "ce:GetCostAndUsage": "Owned deterministic Cost Explorer report",
    "ce:GetCostForecast": "Billing MCP cost-explorer forecast",
    # Dependent action every Cost Explorer read requires (SAR operation table)
    "aws-portal:ViewBilling": "Cost Explorer dependent action (report + forecasts)",
    # Billing MCP cost-optimization tool
    "cost-optimization-hub:ListRecommendationSummaries": "Billing MCP cost-optimization",
    "cost-optimization-hub:ListRecommendations": "Billing MCP cost-optimization",
    "cost-optimization-hub:GetRecommendation": "Billing MCP cost-optimization",
    # Billing MCP compute-optimizer tool (reviewed EC2 + Auto Scaling rightsizing)
    "compute-optimizer:GetEC2InstanceRecommendations": "Billing MCP compute-optimizer (EC2)",
    "compute-optimizer:GetAutoScalingGroupRecommendations": "Billing MCP compute-optimizer (Auto Scaling)",
    # Compute Optimizer dependent reads (SAR table + Compute Optimizer IAM guide)
    "ec2:DescribeInstances": "Compute Optimizer EC2 recommendation dependent read",
    "autoscaling:DescribeAutoScalingGroups": "Compute Optimizer Auto Scaling recommendation dependent read",
    "autoscaling:DescribeAutoScalingInstances": "Compute Optimizer Auto Scaling recommendation dependent read",
    # gamelift_specialist.py — exactly the ten reviewed operations
    "gamelift:ListFleets": "GameLift specialist fleet discovery",
    "gamelift:DescribeFleetAttributes": "GameLift specialist classic fleet attributes",
    "gamelift:ListContainerFleets": "GameLift specialist container fleet discovery",
    "gamelift:ListContainerGroupDefinitions": "GameLift specialist container group discovery",
    "gamelift:DescribeContainerFleet": "GameLift specialist container fleet detail",
    "gamelift:DescribeContainerGroupDefinition": "GameLift specialist container group detail",
    "gamelift:ListFleetDeployments": "GameLift specialist deployment status",
    "gamelift:DescribeFleetUtilization": "GameLift specialist utilization",
    "gamelift:DescribeFleetCapacity": "GameLift specialist capacity",
    "gamelift:DescribeScalingPolicies": "GameLift specialist scaling policies",
    # aws-api-mcp call_aws eks verbs + eks-mcp DescribeCluster
    "eks:ListClusters": "aws-api-mcp call_aws eks list-clusters",
    "eks:DescribeCluster": "eks-mcp k8s client cache + describe-cluster",
    "eks:ListNodegroups": "aws-api-mcp call_aws eks list-nodegroups",
    "eks:DescribeNodegroup": "aws-api-mcp call_aws eks describe-nodegroup",
    "eks:ListFargateProfiles": "aws-api-mcp call_aws eks list-fargate-profiles",
    "eks:DescribeFargateProfile": "aws-api-mcp call_aws eks describe-fargate-profile",
    "eks:ListAddons": "aws-api-mcp call_aws eks list-addons",
    "eks:DescribeAddon": "aws-api-mcp call_aws eks describe-addon",
    # eks-mcp get_eks_vpc_config is wholly non-functional (ec2:DescribeRouteTables
    # was never granted and shares its error path), so its DescribeVpcs/
    # DescribeSubnets reads are a pre-existing gap and are NOT granted.
    # eks-mcp CloudWatchHandler.get_cloudwatch_metrics
    "cloudwatch:GetMetricData": "eks-mcp get_cloudwatch_metrics",
    # container-mode image pull (toolkit reference execution_role_policy.json.j2)
    "ecr:BatchGetImage": "container-mode runtime image pull",
    "ecr:GetDownloadUrlForLayer": "container-mode runtime layer pull",
    "ecr:BatchCheckLayerAvailability": "container-mode runtime layer check",
    "ecr:GetAuthorizationToken": "container-mode registry auth",
    # runtime log emission (toolkit reference)
    "logs:CreateLogGroup": "runtime log group creation",
    "logs:CreateLogStream": "runtime log stream creation",
    "logs:PutLogEvents": "runtime log emission",
    "logs:DescribeLogStreams": "runtime log stream metadata",
    "logs:DescribeLogGroups": "runtime log group metadata (metadata only)",
    # ADOT / X-Ray tracing (toolkit reference)
    "xray:PutTraceSegments": "X-Ray trace segment write",
    "xray:PutTelemetryRecords": "X-Ray telemetry write",
    "xray:GetSamplingRules": "X-Ray sampling rules",
    "xray:GetSamplingTargets": "X-Ray sampling targets",
    # cost_snapshot_dynamodb.py shared snapshot store (#365)
    "dynamodb:GetItem": "cost-report snapshot read",
    "dynamodb:PutItem": "cost-report snapshot write",
}

# Every reviewed API operation mapped to the FULL set of IAM actions its
# Service Authorization Reference operation table requires at call time,
# INCLUDING dependent actions in other services. Each required action must be
# granted by the role, except those excluded below because they apply to a
# feature the runtime does not use (each named with its reason). This mapping is
# the regression that catches a missing dependent action after the account-wide
# AWS managed read policies are dropped: removing AWSBillingReadOnlyAccess took
# away aws-portal:ViewBilling (a dependent of every Cost Explorer read), and
# removing CloudWatchReadOnlyAccess took away the Compute Optimizer dependent
# EC2/Auto Scaling describes, so each must now be granted explicitly.
#
# Source: the Service Authorization Reference operation tables for each service,
# plus the Compute Optimizer IAM user guide for the Auto Scaling describe that
# the SAR table does not enumerate
# (https://docs.aws.amazon.com/compute-optimizer/latest/ug/security-iam.html).
SAR_REQUIRED_ACTIONS: dict[str, set[str]] = {
    # bedrock:InvokeModel* also list ApplyGuardrail (granted) plus
    # CallWithBearerToken and InvokeTool — bearer-token credential forwarding and
    # the Bedrock tool-invocation feature, neither of which this runtime uses.
    "bedrock:InvokeModel": {"bedrock:InvokeModel", "bedrock:ApplyGuardrail"},
    "bedrock:InvokeModelWithResponseStream": {
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:InvokeModel",
        "bedrock:ApplyGuardrail",
    },
    "bedrock:Retrieve": {"bedrock:Retrieve"},
    # ApplyGuardrail also lists CallWithBearerToken and
    # InvokeAutomatedReasoningPolicy — bearer-token forwarding and Automated
    # Reasoning checks, neither configured on this guardrail.
    "bedrock:ApplyGuardrail": {"bedrock:ApplyGuardrail"},
    "bedrock:GetPrompt": {"bedrock:GetPrompt"},
    "bedrock-agentcore:CreateEvent": {"bedrock-agentcore:CreateEvent"},
    "bedrock-agentcore:GetEvent": {"bedrock-agentcore:GetEvent"},
    "bedrock-agentcore:ListEvents": {"bedrock-agentcore:ListEvents"},
    "bedrock-agentcore:DeleteEvent": {"bedrock-agentcore:DeleteEvent"},
    "bedrock-agentcore:RetrieveMemoryRecords": {"bedrock-agentcore:RetrieveMemoryRecords"},
    "bedrock-agentcore:BatchCreateMemoryRecords": {"bedrock-agentcore:BatchCreateMemoryRecords"},
    # Every Cost Explorer read additionally requires aws-portal:ViewBilling.
    "ce:GetCostAndUsage": {"ce:GetCostAndUsage", "aws-portal:ViewBilling"},
    "ce:GetCostForecast": {"ce:GetCostForecast", "aws-portal:ViewBilling"},
    "cost-optimization-hub:ListRecommendationSummaries": {"cost-optimization-hub:ListRecommendationSummaries"},
    "cost-optimization-hub:ListRecommendations": {"cost-optimization-hub:ListRecommendations"},
    "cost-optimization-hub:GetRecommendation": {"cost-optimization-hub:GetRecommendation"},
    # Compute Optimizer recommendations require describing the underlying
    # resource. Only the EC2 and Auto Scaling reads are on the reviewed surface;
    # the SAR table does not enumerate the Auto Scaling describe, so it comes
    # from the Compute Optimizer IAM user guide.
    "compute-optimizer:GetEC2InstanceRecommendations": {
        "compute-optimizer:GetEC2InstanceRecommendations",
        "ec2:DescribeInstances",
    },
    "compute-optimizer:GetAutoScalingGroupRecommendations": {
        "compute-optimizer:GetAutoScalingGroupRecommendations",
        "autoscaling:DescribeAutoScalingGroups",
        "autoscaling:DescribeAutoScalingInstances",
    },
    "gamelift:ListFleets": {"gamelift:ListFleets"},
    "gamelift:DescribeFleetAttributes": {"gamelift:DescribeFleetAttributes"},
    "gamelift:ListContainerFleets": {"gamelift:ListContainerFleets"},
    "gamelift:ListContainerGroupDefinitions": {"gamelift:ListContainerGroupDefinitions"},
    "gamelift:DescribeContainerFleet": {"gamelift:DescribeContainerFleet"},
    "gamelift:DescribeContainerGroupDefinition": {"gamelift:DescribeContainerGroupDefinition"},
    "gamelift:ListFleetDeployments": {"gamelift:ListFleetDeployments"},
    "gamelift:DescribeFleetUtilization": {"gamelift:DescribeFleetUtilization"},
    "gamelift:DescribeFleetCapacity": {"gamelift:DescribeFleetCapacity"},
    "gamelift:DescribeScalingPolicies": {"gamelift:DescribeScalingPolicies"},
    "eks:ListClusters": {"eks:ListClusters"},
    "eks:DescribeCluster": {"eks:DescribeCluster"},
    "eks:ListNodegroups": {"eks:ListNodegroups"},
    "eks:DescribeNodegroup": {"eks:DescribeNodegroup"},
    "eks:ListFargateProfiles": {"eks:ListFargateProfiles"},
    "eks:DescribeFargateProfile": {"eks:DescribeFargateProfile"},
    "eks:ListAddons": {"eks:ListAddons"},
    "eks:DescribeAddon": {"eks:DescribeAddon"},
    "cloudwatch:GetMetricData": {"cloudwatch:GetMetricData"},
    # BatchGetImage also lists BatchImportUpstreamImage, CreateRepository, and
    # TagResource; GetDownloadUrlForLayer also lists BatchImportUpstreamImage —
    # all ECR pull-through-cache actions. The runtime only pulls an existing,
    # already-built image, so none of the pull-through-cache actions apply.
    "ecr:BatchGetImage": {"ecr:BatchGetImage"},
    "ecr:GetDownloadUrlForLayer": {"ecr:GetDownloadUrlForLayer"},
    "ecr:BatchCheckLayerAvailability": {"ecr:BatchCheckLayerAvailability"},
    "ecr:GetAuthorizationToken": {"ecr:GetAuthorizationToken"},
    # CreateLogGroup also lists logs:TagLogGroup and logs:TagResource — tag-on-
    # create, which the runtime does not use (it creates untagged groups).
    "logs:CreateLogGroup": {"logs:CreateLogGroup"},
    "logs:CreateLogStream": {"logs:CreateLogStream"},
    "logs:PutLogEvents": {"logs:PutLogEvents"},
    "logs:DescribeLogStreams": {"logs:DescribeLogStreams"},
    "logs:DescribeLogGroups": {"logs:DescribeLogGroups"},
    "xray:PutTraceSegments": {"xray:PutTraceSegments"},
    "xray:PutTelemetryRecords": {"xray:PutTelemetryRecords"},
    "xray:GetSamplingRules": {"xray:GetSamplingRules"},
    "xray:GetSamplingTargets": {"xray:GetSamplingTargets"},
    # GetItem also lists dynamodb:ReadDataForReplication and PutItem also lists
    # dynamodb:WriteDataForReplication — DynamoDB global-tables replication,
    # which the single-Region snapshot table does not use.
    "dynamodb:GetItem": {"dynamodb:GetItem"},
    "dynamodb:PutItem": {"dynamodb:PutItem"},
}

# Actions permitted to use Resource: '*'. Each has no scopable resource type, a
# global endpoint, or dynamic account-owned names (region-conditioned instead).
WILDCARD_RESOURCE_ALLOWLIST = frozenset(
    {
        "ce:GetCostAndUsage",
        "ce:GetCostForecast",
        "aws-portal:ViewBilling",
        "cost-optimization-hub:ListRecommendationSummaries",
        "cost-optimization-hub:ListRecommendations",
        "cost-optimization-hub:GetRecommendation",
        "compute-optimizer:GetEC2InstanceRecommendations",
        "compute-optimizer:GetAutoScalingGroupRecommendations",
        "ec2:DescribeInstances",
        "autoscaling:DescribeAutoScalingGroups",
        "autoscaling:DescribeAutoScalingInstances",
        "gamelift:ListFleets",
        "gamelift:DescribeFleetAttributes",
        "gamelift:ListContainerFleets",
        "gamelift:ListContainerGroupDefinitions",
        "gamelift:DescribeContainerFleet",
        "gamelift:DescribeContainerGroupDefinition",
        "gamelift:ListFleetDeployments",
        "gamelift:DescribeFleetUtilization",
        "gamelift:DescribeFleetCapacity",
        "gamelift:DescribeScalingPolicies",
        "eks:ListClusters",
        "eks:DescribeCluster",
        "eks:ListNodegroups",
        "eks:DescribeNodegroup",
        "eks:ListFargateProfiles",
        "eks:DescribeFargateProfile",
        "eks:ListAddons",
        "eks:DescribeAddon",
        "cloudwatch:GetMetricData",
        "ecr:GetAuthorizationToken",
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets",
    }
)

# Provider-WRITE verbs that are allowed because they are scoped and reviewed:
# AgentCore STM event writes, LTM record creation, telemetry writes, and the
# DynamoDB snapshot PutItem. No other write verb may appear.
ALLOWED_WRITE_ACTIONS = frozenset(
    {
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:DeleteEvent",
        "bedrock-agentcore:BatchCreateMemoryRecords",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "dynamodb:PutItem",
    }
)

# Log-CONTENT read actions that must never appear (no runtime reads log content).
FORBIDDEN_LOG_CONTENT_ACTIONS = (
    "logs:GetLogEvents",
    "logs:FilterLogEvents",
    "logs:StartQuery",
    "logs:GetQueryResults",
    "logs:StartLiveTail",
)

# Prefixes that identify a provider state-changing (write/mutate) verb. Model
# inference (bedrock:InvokeModel*) and runtime invocation are request/response
# calls, not provider-resource mutations, so "Invoke" is deliberately excluded.
_WRITE_PREFIXES = (
    "Create",
    "Update",
    "Delete",
    "Put",
    "Batch",
    "Register",
    "Deregister",
    "Attach",
    "Detach",
    "Modify",
    "Terminate",
    "Associate",
    "Disassociate",
    "Tag",
    "Untag",
    "Enable",
    "Disable",
    "Remove",
)


# The set of keys any statement in this role may carry. A statement with an
# extra key (NotAction, NotResource, Principal, ...) is forbidden: it is either a
# deny/not-form that this allowlist-only role must never use, or an unexpected
# shape the other tests do not model.
ALLOWED_STATEMENT_KEYS = frozenset({"Sid", "Effect", "Action", "Resource", "Condition"})

# The exact region condition every region-scoped statement must carry. After the
# !Sub/!Ref collapse, !Ref AWS::Region renders as the scalar "AWS::Region".
EXPECTED_REGION_CONDITION = {"StringEquals": {"aws:RequestedRegion": "AWS::Region"}}

# Sids that MUST carry EXPECTED_REGION_CONDITION (regional reads with no
# scopable resource type or dynamic account-owned names) and no other condition.
REGION_SCOPED_SIDS = frozenset(
    {
        "ComputeOptimizerReadAccess",
        "AutoScalingReadAccess",
        "GameLiftReadAccess",
        "EKSReadAccess",
        "EC2ReadAccess",
        "ECRTokenAccess",
        "CloudWatchMetricsRead",
    }
)

# Exact Resource lists (post-!Sub/!Ref collapse) for the ARN-scoped statements.
# ${AWS::Region}/${AWS::AccountId} stay as literal !Sub placeholders; !Ref
# collapses to the bare pseudo-parameter name. Widening any ARN (e.g. to *:* or
# an account-wide log-group:*) must fail these.
EXPECTED_RESOURCES: dict[str, list[str]] = {
    "AgentCoreMemoryAccess": [
        "arn:aws:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:memory/gameagent*",
    ],
    "ECRImageAccess": [
        "arn:aws:ecr:${AWS::Region}:${AWS::AccountId}:repository/bedrock-agentcore-gameagentruntime",
    ],
    "CloudWatchLogsWrite": [
        "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/bedrock-agentcore/*",
        "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/bedrock-agentcore/*:*",
        "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/application-signals/data:*",
        "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/spans/*",
        "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/spans/*:*",
    ],
    # DescribeLogStreams is scoped to THIS runtime's own group
    # (/aws/bedrock-agentcore/runtimes/gameagentruntime-<id>-DEFAULT), never the
    # account-wide /aws/bedrock-agentcore/* set.
    "CloudWatchLogsDescribeStreams": [
        "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/bedrock-agentcore/runtimes/gameagentruntime-*",
        "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/bedrock-agentcore/runtimes/gameagentruntime-*:*",
    ],
    # DescribeLogGroups returns metadata only and is the single read allowed on
    # the account-wide log-group:* resource.
    "CloudWatchLogsDescribeGroups": [
        "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:*",
    ],
}

# The complete, exact statement contract for AgentCoreExecutionRole, keyed by
# Sid. Every statement in the role must appear here with its EXACT action set,
# EXACT Resource list, and EXACT Condition (or None for no condition). This is
# the single structural source of truth: because each granted action is assigned
# to exactly one Sid here, a feature re-granted through a NEW statement (new Sid
# or no Sid), a widened ARN, or a dropped/loosened region condition cannot hide
# behind an action-only or Sid-keyed check — the statement set no longer matches
# this contract. "${...}" placeholders are the literal !Sub strings left by the
# intrinsic-tolerant loader; "AWS::Region" is the collapsed !Ref AWS::Region.
_WILDCARD = ["*"]
EXPECTED_STATEMENTS: dict[str, dict] = {
    "BedrockModelInvocation": {
        "actions": {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"},
        "resources": [
            "arn:aws:bedrock:${AWS::Region}::foundation-model/*",
            "arn:aws:bedrock:::foundation-model/*",
            "arn:aws:bedrock:${AWS::Region}::inference-profile/*",
            "arn:aws:bedrock:${AWS::Region}:${AWS::AccountId}:inference-profile/*",
            "arn:aws:bedrock:${AWS::Region}:${AWS::AccountId}:application-inference-profile/*",
        ],
        "condition": None,
    },
    "BedrockKnowledgeBaseAccess": {
        "actions": {"bedrock:Retrieve"},
        "resources": ["arn:aws:bedrock:${AWS::Region}:${AWS::AccountId}:knowledge-base/*"],
        "condition": None,
    },
    "BedrockGuardrailAccess": {
        "actions": {"bedrock:ApplyGuardrail"},
        "resources": ["arn:aws:bedrock:${AWS::Region}:${AWS::AccountId}:guardrail/*"],
        "condition": None,
    },
    "BedrockPromptManagementAccess": {
        "actions": {"bedrock:GetPrompt"},
        "resources": ["arn:aws:bedrock:${AWS::Region}:${AWS::AccountId}:prompt/*"],
        "condition": None,
    },
    "AgentCoreMemoryAccess": {
        "actions": {
            "bedrock-agentcore:CreateEvent",
            "bedrock-agentcore:GetEvent",
            "bedrock-agentcore:ListEvents",
            "bedrock-agentcore:DeleteEvent",
            "bedrock-agentcore:RetrieveMemoryRecords",
            "bedrock-agentcore:BatchCreateMemoryRecords",
        },
        "resources": ["arn:aws:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:memory/gameagent*"],
        "condition": None,
    },
    "CostExplorerReadAccess": {
        "actions": {"ce:GetCostAndUsage", "ce:GetCostForecast", "aws-portal:ViewBilling"},
        "resources": _WILDCARD,
        "condition": None,
    },
    "CostOptimizationHubReadAccess": {
        "actions": {
            "cost-optimization-hub:ListRecommendationSummaries",
            "cost-optimization-hub:ListRecommendations",
            "cost-optimization-hub:GetRecommendation",
        },
        "resources": _WILDCARD,
        "condition": None,
    },
    "ComputeOptimizerReadAccess": {
        "actions": {
            "compute-optimizer:GetEC2InstanceRecommendations",
            "compute-optimizer:GetAutoScalingGroupRecommendations",
        },
        "resources": _WILDCARD,
        "condition": EXPECTED_REGION_CONDITION,
    },
    "AutoScalingReadAccess": {
        "actions": {"autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeAutoScalingInstances"},
        "resources": _WILDCARD,
        "condition": EXPECTED_REGION_CONDITION,
    },
    "GameLiftReadAccess": {
        "actions": {
            "gamelift:ListFleets",
            "gamelift:DescribeFleetAttributes",
            "gamelift:ListContainerFleets",
            "gamelift:ListContainerGroupDefinitions",
            "gamelift:DescribeContainerFleet",
            "gamelift:DescribeContainerGroupDefinition",
            "gamelift:ListFleetDeployments",
            "gamelift:DescribeFleetUtilization",
            "gamelift:DescribeFleetCapacity",
            "gamelift:DescribeScalingPolicies",
        },
        "resources": _WILDCARD,
        "condition": EXPECTED_REGION_CONDITION,
    },
    "EKSReadAccess": {
        "actions": {
            "eks:ListClusters",
            "eks:DescribeCluster",
            "eks:ListNodegroups",
            "eks:DescribeNodegroup",
            "eks:ListFargateProfiles",
            "eks:DescribeFargateProfile",
            "eks:ListAddons",
            "eks:DescribeAddon",
        },
        "resources": _WILDCARD,
        "condition": EXPECTED_REGION_CONDITION,
    },
    "EC2ReadAccess": {
        "actions": {"ec2:DescribeInstances"},
        "resources": _WILDCARD,
        "condition": EXPECTED_REGION_CONDITION,
    },
    "ECRImageAccess": {
        "actions": {"ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"},
        "resources": ["arn:aws:ecr:${AWS::Region}:${AWS::AccountId}:repository/bedrock-agentcore-gameagentruntime"],
        "condition": None,
    },
    "ECRTokenAccess": {
        "actions": {"ecr:GetAuthorizationToken"},
        "resources": _WILDCARD,
        "condition": EXPECTED_REGION_CONDITION,
    },
    "CloudWatchLogsWrite": {
        "actions": {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"},
        "resources": [
            "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/bedrock-agentcore/*",
            "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/bedrock-agentcore/*:*",
            "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/application-signals/data:*",
            "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/spans/*",
            "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/spans/*:*",
        ],
        "condition": None,
    },
    "CloudWatchLogsDescribeStreams": {
        "actions": {"logs:DescribeLogStreams"},
        "resources": [
            "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/bedrock-agentcore/runtimes/gameagentruntime-*",
            "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/bedrock-agentcore/runtimes/gameagentruntime-*:*",
        ],
        "condition": None,
    },
    "CloudWatchLogsDescribeGroups": {
        "actions": {"logs:DescribeLogGroups"},
        "resources": ["arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:*"],
        "condition": None,
    },
    "CloudWatchMetricsRead": {
        "actions": {"cloudwatch:GetMetricData"},
        "resources": _WILDCARD,
        "condition": EXPECTED_REGION_CONDITION,
    },
    "XRayTracing": {
        "actions": {
            "xray:PutTraceSegments",
            "xray:PutTelemetryRecords",
            "xray:GetSamplingRules",
            "xray:GetSamplingTargets",
        },
        "resources": _WILDCARD,
        "condition": None,
    },
    "CostReportSnapshotReadWrite": {
        "actions": {"dynamodb:GetItem", "dynamodb:PutItem"},
        "resources": ["CostReportSnapshotTable.Arn"],
        "condition": None,
    },
}

# Templates whose IAM Policy/ManagedPolicy/RolePolicy resources must never
# reference the AgentCore execution role out of band of its inline Properties.
INFRASTRUCTURE_DIR = PROJECT_ROOT / "infrastructure"
AGENTCORE_ROLE_LOGICAL_ID = "AgentCoreExecutionRole"
AGENTCORE_ROLE_NAME_FRAGMENT = "agentcore-execution-role"


@pytest.fixture(scope="module")
def role() -> dict:
    template = load_cfn_template(BASE_TEMPLATE.read_text(encoding="utf-8"))
    properties: dict = template["Resources"]["AgentCoreExecutionRole"]["Properties"]
    return properties


def _statements(role: dict) -> list[dict]:
    out: list[dict] = []
    for policy in role.get("Policies", []):
        out.extend(policy["PolicyDocument"]["Statement"])
    return out


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


def _resources(statement: dict) -> list[str]:
    resource = statement.get("Resource", [])
    return [resource] if isinstance(resource, str) else list(resource)


def _all_actions(role: dict) -> set[str]:
    found: set[str] = set()
    for statement in _statements(role):
        found.update(_actions(statement))
    return found


def test_no_managed_policy_arns(role):
    """No account-wide AWS managed read policies may be attached."""
    assert role.get("ManagedPolicyArns", []) == []


def test_action_set_equals_reviewed_inventory(role):
    """The role grants EXACTLY the reviewed caller inventory — nothing more."""
    actual = _all_actions(role)
    reviewed = set(REVIEWED_ACTIONS)
    assert actual == reviewed, {
        "unreviewed_extra": sorted(actual - reviewed),
        "missing_expected": sorted(reviewed - actual),
    }


def test_sar_required_dependent_actions_are_granted(role):
    """Every reviewed API operation needs its FULL Service Authorization
    Reference action set, including dependent actions in other services. Dropping
    the account-wide AWS managed read policies removed implicit grants (notably
    aws-portal:ViewBilling for the Cost Explorer report/forecasts and the Compute
    Optimizer EC2/Auto Scaling describes), so this asserts each required action
    is granted explicitly. Actions excluded in SAR_REQUIRED_ACTIONS apply only to
    features the runtime does not use and are documented there."""
    granted = _all_actions(role)
    missing: dict[str, list[str]] = {}
    for operation, required in SAR_REQUIRED_ACTIONS.items():
        absent = sorted(required - granted)
        if absent:
            missing[operation] = absent
    assert not missing, {"operation_missing_required_actions": missing}


def test_sar_mapping_covers_every_granted_action(role):
    """Every granted action must appear as a key in SAR_REQUIRED_ACTIONS (as a
    reviewed operation) or as a dependent action of one, so the SAR mapping stays
    the single source of truth for what each caller requires."""
    keys = set(SAR_REQUIRED_ACTIONS)
    dependents: set[str] = set()
    for required in SAR_REQUIRED_ACTIONS.values():
        dependents.update(required)
    covered = keys | dependents
    uncovered = sorted(_all_actions(role) - covered)
    assert not uncovered, {"granted_actions_absent_from_sar_mapping": uncovered}


def test_no_wildcard_or_cfn_or_cloudcontrol_actions(role):
    for action in _all_actions(role):
        assert action != "*", "bare '*' action is forbidden"
        assert not action.endswith(":*"), f"service wildcard action forbidden: {action}"
        assert not action.startswith("cloudformation:"), f"CloudFormation read forbidden: {action}"
        assert not action.startswith("cloudcontrol:"), f"CloudControl read forbidden: {action}"


def test_no_log_content_read_actions(role):
    actions = _all_actions(role)
    for forbidden in FORBIDDEN_LOG_CONTENT_ACTIONS:
        assert forbidden not in actions, f"log-content read forbidden: {forbidden}"
    # No blanket logs:Get* either.
    for action in actions:
        assert not action.startswith("logs:Get"), f"log-content read forbidden: {action}"


def test_log_reads_not_on_account_wide_log_group(role):
    """Only metadata-only logs:DescribeLogGroups may touch log-group:*; a log
    read scoped to the account-wide wildcard is otherwise forbidden."""
    for statement in _statements(role):
        actions = _actions(statement)
        log_reads = [a for a in actions if a in ("logs:DescribeLogGroups", "logs:DescribeLogStreams")]
        if not log_reads:
            continue
        for resource in _resources(statement):
            if resource.endswith(":log-group:*"):
                assert actions == ["logs:DescribeLogGroups"], (
                    "account-wide log-group:* read allowed only for metadata-only "
                    f"DescribeLogGroups, found: {actions}"
                )


def test_ecr_pull_scoped_to_runtime_repository(role):
    for statement in _statements(role):
        actions = _actions(statement)
        pull_actions = [a for a in actions if a.startswith("ecr:") and a != "ecr:GetAuthorizationToken"]
        if not pull_actions:
            continue
        assert "ecr:DescribeRepositories" not in actions
        assert "ecr:ListImages" not in actions
        for resource in _resources(statement):
            assert (
                "repository/bedrock-agentcore-gameagentruntime" in resource
            ), f"ECR pull must target the runtime repository, found: {resource}"


def test_wildcard_resource_only_for_allowlisted_actions(role):
    for statement in _statements(role):
        resources = _resources(statement)
        if "*" not in resources:
            continue
        for action in _actions(statement):
            assert action in WILDCARD_RESOURCE_ALLOWLIST, (
                f"action {action} uses Resource '*' but is not in the allowlist of actions with "
                "no scopable resource type, a global endpoint, or dynamic account-owned names"
            )


def test_no_unreviewed_provider_write_verbs(role):
    # "Batch" alone is ambiguous (ecr:BatchGetImage / BatchCheckLayerAvailability
    # are reads), so a Batch* action counts as a write only when it batches a
    # mutating verb.
    batch_write_prefixes = ("BatchCreate", "BatchDelete", "BatchUpdate", "BatchPut", "BatchWrite")
    for action in _all_actions(role):
        service, _, verb = action.partition(":")
        is_write = verb.startswith(_WRITE_PREFIXES) and not (
            verb.startswith("Batch") and not verb.startswith(batch_write_prefixes)
        )
        if is_write and action not in ALLOWED_WRITE_ACTIONS:
            raise AssertionError(f"unreviewed provider-write verb: {action}")


def test_memory_scoped_to_project_namespace(role):
    for statement in _statements(role):
        if any(a.startswith("bedrock-agentcore:") for a in _actions(statement)):
            for resource in _resources(statement):
                assert (
                    "memory/gameagent" in resource
                ), f"AgentCore memory must be scoped to memory/gameagent*, found: {resource}"


def test_no_memory_record_delete(role):
    actions = _all_actions(role)
    assert "bedrock-agentcore:DeleteMemoryRecord" not in actions
    assert "bedrock-agentcore:BatchDeleteMemoryRecords" not in actions


def test_every_statement_is_an_allow_action_resource_statement(role):
    """Each statement's keys are a subset of {Sid, Effect, Action, Resource,
    Condition} and Effect is Allow. This rejects NotAction / NotResource (which
    would otherwise grant broadly while slipping past the Action/Resource-based
    checks) and any Deny or Principal shape this allowlist-only role must not
    use. Rejects an Allow NotAction statement and a Resource replaced by
    NotResource."""
    for statement in _statements(role):
        extra = set(statement) - ALLOWED_STATEMENT_KEYS
        assert not extra, f"statement {statement.get('Sid')!r} has forbidden keys: {sorted(extra)}"
        assert "Action" in statement, f"statement {statement.get('Sid')!r} has no Action key"
        assert "Resource" in statement, f"statement {statement.get('Sid')!r} has no Resource key"
        assert statement.get("Effect") == "Allow", f"statement {statement.get('Sid')!r} is not an Allow"


def test_no_iam_actions(role):
    """No statement may grant any iam: action (defense against privilege
    escalation via policy/role manipulation)."""
    for action in _all_actions(role):
        assert not action.startswith("iam:"), f"iam: action forbidden: {action}"


def test_region_scoped_statements_carry_exact_region_condition(role):
    """Every region-scoped read with no scopable resource type or dynamic
    account-owned names must carry EXACTLY
    StringEquals {aws:RequestedRegion: !Ref AWS::Region} and no other condition.
    Dropping or altering it on GameLift, Compute Optimizer, or Auto Scaling must
    fail here."""
    seen: set[str] = set()
    for statement in _statements(role):
        sid = statement.get("Sid")
        if sid not in REGION_SCOPED_SIDS:
            continue
        seen.add(sid)
        assert statement.get("Condition") == EXPECTED_REGION_CONDITION, (
            f"region-scoped statement {sid!r} must carry exactly "
            f"{EXPECTED_REGION_CONDITION}, found: {statement.get('Condition')}"
        )
    missing = REGION_SCOPED_SIDS - seen
    assert not missing, f"expected region-scoped statements are absent: {sorted(missing)}"


def test_arn_scoped_statements_have_exact_resource_lists(role):
    """Pin the exact post-!Sub resource list for the memory, ECR image, and all
    log statements. Widening an ARN (memory or ECR to any region/account, or
    DescribeLogStreams/Write to an account-wide log-group:*) must fail here."""
    seen: set[str] = set()
    for statement in _statements(role):
        sid = statement.get("Sid")
        if sid not in EXPECTED_RESOURCES:
            continue
        seen.add(sid)
        assert _resources(statement) == EXPECTED_RESOURCES[sid], (
            f"statement {sid!r} resources changed; expected {EXPECTED_RESOURCES[sid]}, "
            f"found {_resources(statement)}"
        )
    missing = set(EXPECTED_RESOURCES) - seen
    assert not missing, f"expected ARN-scoped statements are absent: {sorted(missing)}"


def test_every_statement_has_a_unique_sid(role):
    """Every statement must carry a Sid, and no Sid may repeat. A Sid-less or
    duplicate-Sid statement would let a feature be re-granted while a Sid-keyed
    check skips it, so this is the precondition for the exact-contract test
    below."""
    sids: list[str] = []
    for statement in _statements(role):
        sid = statement.get("Sid")
        assert sid, f"statement without a Sid: {statement}"
        sids.append(sid)
    duplicates = sorted({sid for sid in sids if sids.count(sid) > 1})
    assert not duplicates, f"duplicate Sid(s): {duplicates}"


def test_statement_set_matches_exact_contract(role):
    """The set of statement Sids must equal EXPECTED_STATEMENTS exactly, and
    every statement's action set, Resource list, and Condition (or its absence)
    must match its contract entry. Because each granted action is assigned to
    exactly one Sid here, a reviewed feature re-granted through a NEW statement
    (new Sid or no Sid), a widened ARN, or a dropped or loosened region
    condition changes the statement set and fails this test — the gap a
    Sid-keyed or action-only check would miss."""
    statements = _statements(role)
    actual_sids = {s.get("Sid") for s in statements}
    expected_sids = set(EXPECTED_STATEMENTS)
    assert actual_sids == expected_sids, {
        "unexpected_statements": sorted(actual_sids - expected_sids),
        "missing_statements": sorted(expected_sids - actual_sids),
    }

    # Each action must be granted by exactly one statement, and that statement
    # must be the one the contract assigns it to.
    action_to_sid: dict[str, str] = {}
    for sid, spec in EXPECTED_STATEMENTS.items():
        for action in spec["actions"]:
            assert action not in action_to_sid, f"action {action} is assigned to more than one expected Sid"
            action_to_sid[action] = sid

    for statement in statements:
        sid = statement.get("Sid")
        spec = EXPECTED_STATEMENTS[sid]
        actual_actions = set(_actions(statement))
        assert actual_actions == spec["actions"], {
            "sid": sid,
            "unexpected_actions": sorted(actual_actions - spec["actions"]),
            "missing_actions": sorted(spec["actions"] - actual_actions),
        }
        for action in actual_actions:
            assert (
                action_to_sid[action] == sid
            ), f"action {action} appears in {sid!r}, expected {action_to_sid[action]!r}"
        assert (
            _resources(statement) == spec["resources"]
        ), f"statement {sid!r} resources changed; expected {spec['resources']}, found {_resources(statement)}"
        assert (
            statement.get("Condition") == spec["condition"]
        ), f"statement {sid!r} condition changed; expected {spec['condition']}, found {statement.get('Condition')}"


def _looks_like_cfn_template(text: str) -> bool:
    """True if the raw text declares a top-level ``AWSTemplateFormatVersion``
    key, or a top-level ``Resources:`` key alone on its line or followed only by
    a YAML comment. This is a text-only check that matches block-style keys at
    column 0 only (it does not recognise quoted or indented JSON keys), used
    solely to decide whether a parse failure, or a parsed document without a
    ``Resources`` mapping, is fatal (a real but malformed template that must
    raise) rather than a non-template file that is safely skipped. It is never
    used to decide which parseable templates get scanned."""
    for line in text.splitlines():
        if line.startswith("AWSTemplateFormatVersion"):
            return True
        if line.startswith("Resources:"):
            remainder = line[len("Resources:") :].strip()
            if remainder == "" or remainder.startswith("#"):
                return True
    return False


def _iter_infrastructure_templates(root=None):
    """Yield ``(path, parsed)`` for every CloudFormation template under ``root``
    (defaults to the repository ``infrastructure/`` directory).

    Every YAML file is parsed first with the repository loader, and on a loader
    failure the text is retried with ``json.loads`` -- the same JSON-before-YAML
    order ``aws cloudformation deploy`` uses -- so a template written in JSON
    syntax (including one the YAML loader rejects, such as tab-indented JSON) is
    still parsed. Any parsed document that has a ``Resources`` mapping is
    scanned, regardless of whether it carries an ``AWSTemplateFormatVersion``
    header or uses JSON syntax. The text check is consulted only to decide
    whether a *failure* is fatal: a file that looks like a template (a header,
    or a top-level ``Resources:`` key) but neither parser can read, or that
    parses without a ``Resources`` mapping, raises so a malformed template
    cannot slip past the out-of-band attachment scan.

    A non-template YAML file is skipped by one of two paths. A file both parsers
    reject and that does not look like a template is skipped on the parse
    failure -- for example ``game-agent-rbac.yaml``, a multi-document manifest
    the single-document loader rejects. A file that parses to something without
    a ``Resources`` mapping and does not look like a template is skipped after
    parsing -- for example ``game-agent-monitoring-rbac.yaml``, a single
    ClusterRole document."""
    scan_root = INFRASTRUCTURE_DIR if root is None else root
    for path in sorted(scan_root.rglob("*.yaml")) + sorted(scan_root.rglob("*.yml")):
        text = path.read_text(encoding="utf-8")
        try:
            parsed = load_cfn_template(text)
        except Exception as exc:  # noqa: BLE001 — a template that cannot parse must fail, not skip
            try:
                parsed = json.loads(text)
            except ValueError:
                if _looks_like_cfn_template(text):
                    raise AssertionError(f"{path.name} looks like a template but failed to parse: {exc}") from exc
                continue
        if isinstance(parsed, dict) and isinstance(parsed.get("Resources"), dict):
            yield path, parsed
        elif _looks_like_cfn_template(text):
            raise AssertionError(f"{path.name} looks like a template but has no Resources mapping")


def _scan_for_execution_role_attachments(root=None):
    """Return ``(offenders, scanned)`` for the out-of-band attachment scan over
    ``root`` (defaults to the repository ``infrastructure/`` directory).
    ``offenders`` lists ``file:logicalId`` for any standalone policy resource
    that attaches to the AgentCore execution role; ``scanned`` is the set of
    template file names that were parsed and inspected."""
    attach_types = {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::RolePolicy"}
    offenders: list[str] = []
    scanned: set[str] = set()
    for path, template in _iter_infrastructure_templates(root):
        scanned.add(path.name)
        for logical_id, resource in template["Resources"].items():
            if not isinstance(resource, dict) or resource.get("Type") not in attach_types:
                continue
            properties = resource.get("Properties", {})
            targets: list = []
            targets.extend(properties.get("Roles", []) or [])
            role_name = properties.get("RoleName")
            if role_name is not None:
                targets.append(role_name)
            rendered = " ".join(str(target) for target in targets)
            if AGENTCORE_ROLE_LOGICAL_ID in rendered or AGENTCORE_ROLE_NAME_FRAGMENT in rendered:
                offenders.append(f"{path.name}:{logical_id}")
    return offenders, scanned


def test_no_out_of_band_policy_attaches_to_execution_role():
    """No template under infrastructure/ may attach a standalone
    AWS::IAM::Policy, AWS::IAM::ManagedPolicy, or AWS::IAM::RolePolicy to the
    AgentCore execution role (by logical ID or by role-name fragment). Such a
    resource would grant permissions outside the reviewed inline policies and
    slip past the role-Properties checks."""
    offenders, scanned = _scan_for_execution_role_attachments()
    assert not offenders, f"out-of-band policy attached to the execution role: {offenders}"
    assert (
        BASE_TEMPLATE.name in scanned
    ), f"base template {BASE_TEMPLATE.name} was not scanned (scanned: {sorted(scanned)})"


# The reviewed infrastructure/ templates all carry an AWSTemplateFormatVersion
# header, but CloudFormation treats that header as optional and accepts JSON
# syntax, so the scan must recognise a template from its parsed structure rather
# than from the raw text. These fixtures reproduce an out-of-band RolePolicy that
# attaches to the execution role in two header-free shapes.
_OUT_OF_BAND_ROLE_POLICY_BODY = (
    "  ExtraRolePolicy:\n"
    "    Type: AWS::IAM::RolePolicy\n"
    "    Properties:\n"
    "      PolicyName: x\n"
    "      RoleName: !Select [1, !Split ['/', !ImportValue game-agent-AgentCoreExecutionRoleArn]]\n"
    "      PolicyDocument: {Version: '2012-10-17', Statement: "
    "[{Effect: Allow, Action: ['logs:*'], Resource: '*'}]}\n"
)
# Header-less YAML whose top-level ``Resources:`` key carries a trailing comment.
_HEADERLESS_RESOURCES_WITH_COMMENT = (
    "Description: extra stack\nResources:  # out-of-band policy\n" + _OUT_OF_BAND_ROLE_POLICY_BODY
)
# JSON document (valid YAML) saved with a .yaml extension, no header.
_JSON_SYNTAX_YAML = (
    '{"Resources": {"ExtraRolePolicy": {"Type": "AWS::IAM::RolePolicy", "Properties": '
    '{"PolicyName": "x", "RoleName": {"Fn::ImportValue": "game-agent-AgentCoreExecutionRoleArn"}, '
    '"PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
    '"Action": ["logs:*"], "Resource": "*"}]}}}}}\n'
)
# Tab-indented JSON saved with a .yaml extension: valid JSON that
# ``aws cloudformation deploy`` reads with json.loads first, but the YAML loader
# rejects (tabs are not valid YAML indentation). The JSON fallback must parse it
# so the out-of-band attachment is still detected.
_TAB_INDENTED_JSON_YAML = json.dumps(
    {
        "Resources": {
            "ExtraRolePolicy": {
                "Type": "AWS::IAM::RolePolicy",
                "Properties": {
                    "PolicyName": "x",
                    "RoleName": {"Fn::ImportValue": "game-agent-AgentCoreExecutionRoleArn"},
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [{"Effect": "Allow", "Action": ["logs:*"], "Resource": "*"}],
                    },
                },
            }
        }
    },
    indent="\t",
)


def _infra_root_with_extra(tmp_path, filename, body):
    """Build an infrastructure/ tree with the base template plus one extra file,
    so the scan has a real template to find (base-scanned) alongside the extra
    file under test."""
    cloudformation = tmp_path / "infrastructure" / "cloudformation"
    cloudformation.mkdir(parents=True)
    (cloudformation / BASE_TEMPLATE.name).write_text(BASE_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    (cloudformation / filename).write_text(body, encoding="utf-8")
    return tmp_path / "infrastructure"


@pytest.mark.parametrize(
    "label,body",
    [
        ("header-less Resources: with a trailing comment", _HEADERLESS_RESOURCES_WITH_COMMENT),
        ("JSON-syntax template saved as .yaml", _JSON_SYNTAX_YAML),
        ("tab-indented JSON template saved as .yaml", _TAB_INDENTED_JSON_YAML),
    ],
    ids=["headerless_resources_comment", "json_syntax_yaml", "tab_indented_json_yaml"],
)
def test_scan_detects_header_free_out_of_band_attachment(tmp_path, label, body):
    """A parseable template that lacks an AWSTemplateFormatVersion header must
    still be scanned, so an out-of-band policy attached to the execution role in
    a header-less (comment-trailed Resources:) or JSON-syntax .yaml file is
    detected rather than silently skipped. The JSON-syntax cases also cover
    tab-indented JSON, which the YAML loader rejects but aws cloudformation
    deploy reads with json.loads, so the scan's JSON fallback must parse it."""
    root = _infra_root_with_extra(tmp_path, "99-extra.yaml", body)
    offenders, scanned = _scan_for_execution_role_attachments(root)
    assert offenders, f"out-of-band attachment via {label} was not detected (scanned: {sorted(scanned)})"
    assert "99-extra.yaml" in scanned


def test_scan_raises_on_header_bearing_unparseable_template(tmp_path, monkeypatch):
    """A file that looks like a template (header present) but the loader rejects
    must fail the scan rather than be skipped. Covers both a simulated loader
    rejection and a real YAML syntax error."""
    cloudformation = tmp_path / "infrastructure" / "cloudformation"
    cloudformation.mkdir(parents=True)
    (cloudformation / BASE_TEMPLATE.name).write_text(BASE_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    root = tmp_path / "infrastructure"

    # (a) real YAML syntax error, with a header: unbalanced flow mapping.
    broken = (
        "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  ExtraRolePolicy:\n"
        "    Type: AWS::IAM::RolePolicy\n    Properties:\n      PolicyName: x\n"
        "      RoleName: !ImportValue game-agent-AgentCoreExecutionRoleArn\n"
        "      PolicyDocument: {Version: '2012-10-17', Statement: "
        "[{Effect: Allow, Action: ['logs:*'], Resource: '*'}]\n"
    )
    (cloudformation / "99-broken.yaml").write_text(broken, encoding="utf-8")
    with pytest.raises(AssertionError, match="failed to parse"):
        _scan_for_execution_role_attachments(root)

    # (b) simulated loader rejection on an otherwise header-bearing template.
    (cloudformation / "99-broken.yaml").write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n" + _OUT_OF_BAND_ROLE_POLICY_BODY,
        encoding="utf-8",
    )
    real_loader = load_cfn_template

    def _rejecting_loader(text):
        if "ExtraRolePolicy" in text:
            raise ValueError("simulated loader rejection")
        return real_loader(text)

    monkeypatch.setattr(sys.modules[__name__], "load_cfn_template", _rejecting_loader)
    with pytest.raises(AssertionError, match="failed to parse"):
        _scan_for_execution_role_attachments(root)


# Three template-like files that must each fail the scan, pinning a distinct
# branch of the fatality decision:
#   * an unparseable, header-less file whose only template signal is a
#     comment-trailed ``Resources:`` line (the comment-acceptance branch of the
#     text check);
#   * a header-bearing file that parses to a non-mapping ``Resources`` (the
#     "parsed but no Resources mapping" raise); and
#   * an unparseable, header-bearing file whose ``Resources`` key is not alone on
#     its line, so only the header line marks it as template-like (the
#     AWSTemplateFormatVersion branch of the text check).
# Each shape fails if its branch is removed, so none of the three fatality
# branches can be deleted without a regression.
_UNPARSEABLE_HEADERLESS_COMMENT_RESOURCES = "Resources:  # out-of-band policy\n  Bad: {X: [\n"
_HEADER_WITH_NON_MAPPING_RESOURCES = "AWSTemplateFormatVersion: '2010-09-09'\nResources: []\n"
_UNPARSEABLE_HEADER_NON_BARE_RESOURCES = "AWSTemplateFormatVersion: '2010-09-09'\nResources: {X: [\n"


@pytest.mark.parametrize(
    "label,body",
    [
        (
            "unparseable header-less file with a comment-trailed Resources: line",
            _UNPARSEABLE_HEADERLESS_COMMENT_RESOURCES,
        ),
        ("header-bearing file whose Resources is not a mapping", _HEADER_WITH_NON_MAPPING_RESOURCES),
        ("unparseable header-bearing file with Resources not on its own line", _UNPARSEABLE_HEADER_NON_BARE_RESOURCES),
    ],
    ids=[
        "unparseable_headerless_comment_resources",
        "header_non_mapping_resources",
        "unparseable_header_non_bare_resources",
    ],
)
def test_scan_raises_on_template_like_file_that_cannot_be_scanned(tmp_path, label, body):
    """A file that looks like a template -- by its header or by a top-level
    ``Resources:`` line -- but that neither parser can read, or that parses
    without a ``Resources`` mapping, must fail the scan rather than be skipped.
    Each case marks itself template-like through a different signal, so the scan
    cannot drop any one fatality branch without a regression."""
    root = _infra_root_with_extra(tmp_path, "99-extra.yaml", body)
    with pytest.raises(AssertionError, match="looks like a template"):
        _scan_for_execution_role_attachments(root)


def test_template_stays_under_inline_byte_limit():
    size = len(BASE_TEMPLATE.read_text(encoding="utf-8").encode("utf-8"))
    assert size < INLINE_TEMPLATE_BYTE_LIMIT, f"template is {size} bytes (limit {INLINE_TEMPLATE_BYTE_LIMIT})"
