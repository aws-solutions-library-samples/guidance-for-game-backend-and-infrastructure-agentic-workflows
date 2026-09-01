#!/bin/bash
# Enroll an EKS cluster with Game Agent AgentCore Runtime.
# Configures a dedicated IAM principal for read-only Kubernetes monitoring.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITORING_ROLE_FILE="${SCRIPT_DIR}/game-agent-monitoring-rbac.yaml"
DEFAULT_IAM_ROLE_NAME="game-agent-agentcore-execution-role"
IAM_ROLE_NAME="${DEFAULT_IAM_ROLE_NAME}"
LEGACY_K8S_USERNAME="game-agent-agentcore-user"
LEGACY_K8S_GROUP="game-agent-monitoring-group"
CLUSTER_ROLE_NAME="game-agent-monitoring-role"
MANAGED_TAG_KEY="GameAgentManaged"
MANAGED_LABEL_VALUE="game-agent-enrollment"
ACCESS_ENTRY_POLL_ATTEMPTS=${EKS_ACCESS_ENTRY_POLL_ATTEMPTS:-12}
ACCESS_ENTRY_POLL_DELAY_SECONDS=${EKS_ACCESS_ENTRY_POLL_DELAY_SECONDS:-5}

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

usage() {
    echo -e "${RED}Usage: $0 <cluster-name> <region> [--role-name ROLE] [--kube-role-arn ARN] [--enable-audit-logs] [--log-retention-days N]${NC}"
    echo ""
    echo "Examples:"
    echo "  $0 my-cluster us-west-2"
    echo "  $0 my-cluster us-west-2 --kube-role-arn arn:aws:iam::123456789012:role/eks-admin"
    echo "  $0 my-cluster us-west-2 --enable-audit-logs"
    echo "  $0 my-cluster us-west-2 --role-name my-custom-role --enable-audit-logs"
}

fail() {
    echo -e "${RED}❌ $*${NC}" >&2
    exit 1
}

require_option_value() {
    local option=$1
    local count=$2
    local value=${3:-}

    if [ "$count" -lt 2 ] || [ -z "$value" ]; then
        fail "${option} requires a value."
    fi
}

is_access_entry_not_found() {
    local error_text=$1
    grep -q 'ResourceNotFoundException' <<< "$error_text"
}

is_kubernetes_not_found() {
    local error_text=$1
    grep -Eqi '\(NotFound\)|not found' <<< "$error_text"
}

parse_aws_auth_entries() {
    local yaml=$1
    local map_roles

    if ! map_roles=$(printf '%s\n' "$yaml" | yq -r '.data.mapRoles // ""' 2>&1); then
        echo "$map_roles" >&2
        return 1
    fi
    if [ -z "$map_roles" ]; then
        printf '[]\n'
        return 0
    fi
    printf '%s\n' "$map_roles" | yq -p=yaml -o=json '.'
}

aws_auth_has_role() {
    local yaml=$1
    local entries

    if ! entries=$(parse_aws_auth_entries "$yaml"); then
        return 2
    fi
    jq -e --arg arn "$IAM_ROLE_ARN" 'any(.[]?; .rolearn == $arn)' <<< "$entries" > /dev/null
}

aws_auth_has_expected_mapping() {
    local yaml=$1
    local entries

    if ! entries=$(parse_aws_auth_entries "$yaml"); then
        return 2
    fi
    jq -e \
        --arg arn "$IAM_ROLE_ARN" \
        --arg username "$K8S_USERNAME" \
        --arg group "$K8S_GROUP" '
        any(.[]?;
          .rolearn == $arn and
          .username == $username and
          ((.groups // []) == [$group]))
        ' <<< "$entries" > /dev/null
}

checked_can_i() {
    local output
    local status

    if output=$(kubectl auth can-i "$@" 2>&1); then
        status=0
    else
        status=$?
    fi

    if [ "$status" -eq 0 ] && [ "$output" = "yes" ]; then
        printf 'yes'
        return 0
    fi
    if [ "$status" -eq 1 ] && [ "$output" = "no" ]; then
        printf 'no'
        return 0
    fi

    echo "$output" >&2
    return 1
}

principal_fingerprint() {
    local digest

    if command -v sha256sum > /dev/null 2>&1; then
        digest=$(printf '%s' "$IAM_ROLE_ARN" | sha256sum)
    elif command -v shasum > /dev/null 2>&1; then
        digest=$(printf '%s' "$IAM_ROLE_ARN" | shasum -a 256)
    else
        fail "sha256sum or shasum is required to derive principal-specific RBAC names."
    fi
    digest=${digest%% *}
    printf '%s' "${digest:0:16}"
}

print_manual_auth_cleanup() {
    if [ "$ACCESS_ENTRY_CREATED_BY_RUN" = true ]; then
        echo "Manual cleanup: aws eks delete-access-entry --cluster-name ${CLUSTER_NAME} --principal-arn ${IAM_ROLE_ARN} --region ${REGION}" >&2
    elif [ "$ACCESS_GROUP_ADDED_BY_RUN" = true ]; then
        echo "Manual cleanup: restore kubernetesGroups from ${ACCESS_ENTRY_BACKUP_FILE:-the pre-change access entry backup}." >&2
    elif [ "$IDENTITY_MAPPING_CREATED_BY_RUN" = true ]; then
        echo "Manual cleanup: eksctl delete iamidentitymapping --cluster ${CLUSTER_NAME} --region ${REGION} --arn ${IAM_ROLE_ARN}" >&2
    fi
}

rollback_authentication_change() {
    local rollback_failed=false
    local current_aws_auth

    if [ "$ACCESS_ENTRY_CREATED_BY_RUN" = true ]; then
        if ! aws eks delete-access-entry \
            --cluster-name "$CLUSTER_NAME" \
            --principal-arn "$IAM_ROLE_ARN" \
            --region "$REGION" > /dev/null 2>&1 || \
            ! wait_for_access_group_removal; then
            rollback_failed=true
        fi
    elif [ "$ACCESS_GROUP_ADDED_BY_RUN" = true ]; then
        if ! aws eks update-access-entry \
            --cluster-name "$CLUSTER_NAME" \
            --principal-arn "$IAM_ROLE_ARN" \
            --kubernetes-groups "$ORIGINAL_GROUPS_JSON" \
            --region "$REGION" > /dev/null 2>&1 || \
            ! wait_for_access_group_removal; then
            rollback_failed=true
        fi
    elif [ "$IDENTITY_MAPPING_CREATED_BY_RUN" = true ]; then
        if ! eksctl delete iamidentitymapping \
            --cluster "$CLUSTER_NAME" \
            --region "$REGION" \
            --arn "$IAM_ROLE_ARN" > /dev/null 2>&1; then
            rollback_failed=true
        elif current_aws_auth=$(kubectl get configmap aws-auth -n kube-system -o yaml 2>&1); then
            if aws_auth_has_role "$current_aws_auth"; then
                rollback_failed=true
            fi
        elif ! is_kubernetes_not_found "$current_aws_auth"; then
            rollback_failed=true
        fi
    fi

    if [ "$rollback_failed" = true ]; then
        echo -e "${RED}❌ Authentication rollback failed; residual access may remain.${NC}" >&2
        print_manual_auth_cleanup
        return 1
    fi
    return 0
}

rollback_rbac_change() {
    local rollback_failed=false

    if [ "$BINDING_CREATED_BY_RUN" = true ]; then
        if ! kubectl delete clusterrolebinding "$CLUSTER_ROLE_BINDING_NAME" --ignore-not-found > /dev/null 2>&1; then
            rollback_failed=true
        fi
    fi
    if [ "$ROLE_CREATED_BY_RUN" = true ]; then
        if ! kubectl delete clusterrole "$CLUSTER_ROLE_NAME" --ignore-not-found > /dev/null 2>&1; then
            rollback_failed=true
        fi
    fi

    if [ "$rollback_failed" = true ]; then
        echo -e "${RED}❌ RBAC rollback failed; inspect ${CLUSTER_ROLE_NAME} and ${CLUSTER_ROLE_BINDING_NAME}.${NC}" >&2
        return 1
    fi
    return 0
}

rollback_after_failure() {
    local rollback_failed=false

    if ! rollback_rbac_change; then
        rollback_failed=true
    fi
    if ! rollback_authentication_change; then
        rollback_failed=true
    fi
    if [ "$rollback_failed" = false ]; then
        echo -e "${YELLOW}The authentication and RBAC changes made by this run were rolled back.${NC}" >&2
    fi
}

wait_for_access_group() {
    local attempt
    local current_entry

    for ((attempt = 1; attempt <= ACCESS_ENTRY_POLL_ATTEMPTS; attempt++)); do
        if current_entry=$(aws eks describe-access-entry \
            --cluster-name "$CLUSTER_NAME" \
            --principal-arn "$IAM_ROLE_ARN" \
            --region "$REGION" \
            --output json 2>/dev/null) && \
            jq -e --arg group "$K8S_GROUP" \
                '(.accessEntry.kubernetesGroups // []) | index($group) != null' \
                <<< "$current_entry" > /dev/null; then
            return 0
        fi

        if [ "$attempt" -lt "$ACCESS_ENTRY_POLL_ATTEMPTS" ]; then
            sleep "$ACCESS_ENTRY_POLL_DELAY_SECONDS"
        fi
    done

    return 1
}

wait_for_access_group_removal() {
    local attempt
    local current_entry

    for ((attempt = 1; attempt <= ACCESS_ENTRY_POLL_ATTEMPTS; attempt++)); do
        if current_entry=$(aws eks describe-access-entry \
            --cluster-name "$CLUSTER_NAME" \
            --principal-arn "$IAM_ROLE_ARN" \
            --region "$REGION" \
            --output json 2>&1); then
            if ! jq -e --arg group "$K8S_GROUP" \
                '(.accessEntry.kubernetesGroups // []) | index($group) != null' \
                <<< "$current_entry" > /dev/null; then
                return 0
            fi
        elif is_access_entry_not_found "$current_entry"; then
            return 0
        else
            return 1
        fi

        if [ "$attempt" -lt "$ACCESS_ENTRY_POLL_ATTEMPTS" ]; then
            sleep "$ACCESS_ENTRY_POLL_DELAY_SECONDS"
        fi
    done

    return 1
}

if [ "$#" -lt 2 ]; then
    usage
    exit 1
fi

CLUSTER_NAME=$1
REGION=$2
ENABLE_AUDIT_LOGS=false
LOG_RETENTION_DAYS=7
KUBE_ROLE_ARN=""
shift 2

while [ "$#" -gt 0 ]; do
    case $1 in
        --role-name)
            require_option_value "$1" "$#" "${2:-}"
            IAM_ROLE_NAME=$2
            shift 2
            ;;
        --kube-role-arn)
            require_option_value "$1" "$#" "${2:-}"
            KUBE_ROLE_ARN=$2
            shift 2
            ;;
        --enable-audit-logs)
            ENABLE_AUDIT_LOGS=true
            shift
            ;;
        --log-retention-days)
            require_option_value "$1" "$#" "${2:-}"
            LOG_RETENTION_DAYS=$2
            if ! [[ "$LOG_RETENTION_DAYS" =~ ^[0-9]+$ ]] || [ "$LOG_RETENTION_DAYS" -lt 1 ]; then
                fail "--log-retention-days must be a positive integer."
            fi
            shift 2
            ;;
        *)
            fail "Unknown option: $1"
            ;;
    esac
done

ACCESS_ENTRY_EXISTS=false
ACCESS_ENTRY_JSON=""
ACCESS_POLICIES_JSON='{"associatedAccessPolicies":[]}'
ORIGINAL_GROUPS_JSON='[]'
ALREADY_ENROLLED=false
ACCESS_ENTRY_BACKUP_FILE=""
AWS_AUTH_BACKUP_FILE=""
AWS_AUTH_YAML=""
AWS_AUTH_ENTRIES_JSON='[]'
ACCESS_ENTRY_CREATED_BY_RUN=false
ACCESS_GROUP_ADDED_BY_RUN=false
IDENTITY_MAPPING_CREATED_BY_RUN=false
ROLE_EXISTED_BEFORE=false
BINDING_EXISTED_BEFORE=false
ROLE_CREATED_BY_RUN=false
BINDING_CREATED_BY_RUN=false

printf '%b\n' "${BLUE}🚀 Game Agent EKS Cluster Enrollment${NC}"
echo "================================================"
echo ""

echo -e "${BLUE}📋 Step 1: Checking prerequisites...${NC}"
for command_name in aws kubectl jq yq; do
    if ! command -v "$command_name" > /dev/null 2>&1; then
        fail "${command_name} not found. Install it before enrolling a cluster."
    fi
done
if [ ! -f "$MONITORING_ROLE_FILE" ]; then
    fail "Monitoring RBAC manifest not found: ${MONITORING_ROLE_FILE}"
fi
if ! [[ "$ACCESS_ENTRY_POLL_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || \
    ! [[ "$ACCESS_ENTRY_POLL_DELAY_SECONDS" =~ ^[0-9]+$ ]]; then
    fail "EKS access-entry polling settings must be non-negative integers, with at least one attempt."
fi
echo -e "${GREEN}✅ Prerequisites met${NC}"
echo ""

if ! AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || \
    [ -z "$AWS_ACCOUNT_ID" ] || [ "$AWS_ACCOUNT_ID" = "None" ]; then
    fail "Failed to get AWS account ID. Check your AWS credentials."
fi
if ! CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null) || \
    [ -z "$CALLER_ARN" ] || [ "$CALLER_ARN" = "None" ]; then
    fail "Failed to determine the AWS partition from the caller identity."
fi
IFS=':' read -r _ AWS_PARTITION _ <<< "$CALLER_ARN"
IAM_ROLE_ARN="arn:${AWS_PARTITION}:iam::${AWS_ACCOUNT_ID}:role/${IAM_ROLE_NAME}"

if [ "$IAM_ROLE_NAME" = "$DEFAULT_IAM_ROLE_NAME" ]; then
    K8S_USERNAME="$LEGACY_K8S_USERNAME"
    K8S_GROUP="$LEGACY_K8S_GROUP"
    CLUSTER_ROLE_BINDING_NAME="game-agent-monitoring-binding"
else
    PRINCIPAL_ID=$(principal_fingerprint)
    K8S_USERNAME="game-agent-${PRINCIPAL_ID}"
    K8S_GROUP="game-agent-monitoring-${PRINCIPAL_ID}"
    CLUSTER_ROLE_BINDING_NAME="game-agent-monitoring-${PRINCIPAL_ID}"
fi
EFFECTIVE_USERNAME="$K8S_USERNAME"

if ! AUTH_MODE=$(aws eks describe-cluster \
    --name "$CLUSTER_NAME" \
    --region "$REGION" \
    --query 'cluster.accessConfig.authenticationMode' \
    --output text 2>&1); then
    echo "$AUTH_MODE" >&2
    fail "Failed to determine the cluster authentication mode."
fi
case "$AUTH_MODE" in
    API|API_AND_CONFIG_MAP|CONFIG_MAP) ;;
    *) fail "Unsupported cluster authentication mode: ${AUTH_MODE}" ;;
esac

echo -e "${BLUE}Configuration:${NC}"
echo "  Cluster: ${CLUSTER_NAME}"
echo "  Region: ${REGION}"
echo "  AWS Account: ${AWS_ACCOUNT_ID}"
echo "  IAM Role: ${IAM_ROLE_ARN}"
echo "  Authentication Mode: ${AUTH_MODE}"
echo "  Kubernetes Group: ${K8S_GROUP}"
echo "  ClusterRoleBinding: ${CLUSTER_ROLE_BINDING_NAME}"
if [ -n "$KUBE_ROLE_ARN" ]; then
    echo "  Kubernetes Admin Role: ${KUBE_ROLE_ARN}"
fi
echo "  Audit Logs: ${ENABLE_AUDIT_LOGS}"
if [ "$ENABLE_AUDIT_LOGS" = true ]; then
    echo "  Log Retention: ${LOG_RETENTION_DAYS} days"
fi
echo ""

echo -e "${BLUE}📋 Step 2: Updating kubeconfig...${NC}"
if [ -n "$KUBE_ROLE_ARN" ]; then
    if ! aws eks update-kubeconfig \
        --name "$CLUSTER_NAME" \
        --region "$REGION" \
        --role-arn "$KUBE_ROLE_ARN" > /dev/null 2>&1; then
        fail "Failed to update kubeconfig. Check the cluster name, region, and Kubernetes admin role."
    fi
elif ! aws eks update-kubeconfig \
    --name "$CLUSTER_NAME" \
    --region "$REGION" > /dev/null 2>&1; then
    fail "Failed to update kubeconfig. Check the cluster name, region, and Kubernetes admin role."
fi
echo -e "${GREEN}✅ Kubeconfig updated${NC}"
echo ""

echo -e "${BLUE}📋 Step 3: Verifying Kubernetes administrator access...${NC}"
if ! CLUSTER_INFO_OUTPUT=$(kubectl cluster-info 2>&1); then
    echo "$CLUSTER_INFO_OUTPUT" >&2
    fail "Cannot connect to the cluster. Use --kube-role-arn with a role that has Kubernetes administrator access."
fi
if ! ROLE_DRY_RUN_OUTPUT=$(kubectl apply --dry-run=server -f "$MONITORING_ROLE_FILE" 2>&1); then
    echo "$ROLE_DRY_RUN_OUTPUT" >&2
    fail "The active Kubernetes identity cannot apply the monitoring ClusterRole."
fi
if ! BINDING_MANIFEST=$(kubectl create clusterrolebinding "$CLUSTER_ROLE_BINDING_NAME" \
    --clusterrole="$CLUSTER_ROLE_NAME" \
    --group="$K8S_GROUP" \
    --dry-run=client \
    -o json 2>&1); then
    echo "$BINDING_MANIFEST" >&2
    fail "Failed to generate the principal-specific ClusterRoleBinding."
fi
if ! BINDING_MANIFEST=$(jq --arg managed "$MANAGED_LABEL_VALUE" '
    .metadata.labels = ((.metadata.labels // {}) + {
      "app.kubernetes.io/name": "game-agent",
      "app.kubernetes.io/component": "rbac",
      "app.kubernetes.io/managed-by": $managed
    })
    ' <<< "$BINDING_MANIFEST"); then
    fail "Failed to label the generated principal-specific ClusterRoleBinding."
fi
if ! BINDING_DRY_RUN_OUTPUT=$(printf '%s\n' "$BINDING_MANIFEST" | kubectl apply --dry-run=server -f - 2>&1); then
    echo "$BINDING_DRY_RUN_OUTPUT" >&2
    fail "The active Kubernetes identity cannot apply the principal-specific ClusterRoleBinding."
fi
echo -e "${GREEN}✅ Kubernetes administrator access verified${NC}"
echo ""

echo -e "${BLUE}📋 Step 4: Backing up cluster access configuration...${NC}"
if [ "$AUTH_MODE" = "API" ] || [ "$AUTH_MODE" = "API_AND_CONFIG_MAP" ]; then
    if ACCESS_ENTRY_JSON=$(aws eks describe-access-entry \
        --cluster-name "$CLUSTER_NAME" \
        --principal-arn "$IAM_ROLE_ARN" \
        --region "$REGION" \
        --output json 2>&1); then
        ACCESS_ENTRY_EXISTS=true
        if ! ACCESS_POLICIES_JSON=$(aws eks list-associated-access-policies \
            --cluster-name "$CLUSTER_NAME" \
            --principal-arn "$IAM_ROLE_ARN" \
            --region "$REGION" \
            --output json 2>&1); then
            echo "$ACCESS_POLICIES_JSON" >&2
            fail "Failed to read policies associated with the existing access entry. No changes were made."
        fi
        ACCESS_ENTRY_BACKUP_FILE="${SCRIPT_DIR}/access-entry-backup-${CLUSTER_NAME}-$(date +%Y%m%d-%H%M%S).json"
        jq -n \
            --argjson entry "$ACCESS_ENTRY_JSON" \
            --argjson policies "$ACCESS_POLICIES_JSON" \
            '{accessEntry: $entry.accessEntry, associatedAccessPolicies: ($policies.associatedAccessPolicies // [])}' \
            > "$ACCESS_ENTRY_BACKUP_FILE"
        echo -e "${GREEN}✅ Access entry and policy backup created: ${ACCESS_ENTRY_BACKUP_FILE}${NC}"
    elif is_access_entry_not_found "$ACCESS_ENTRY_JSON"; then
        ACCESS_ENTRY_JSON=""
        echo -e "${YELLOW}⚠️  No existing access entry for the AgentCore role${NC}"
    else
        echo "$ACCESS_ENTRY_JSON" >&2
        fail "Failed to read the access entry. No changes were made."
    fi
fi

if [ "$AUTH_MODE" = "CONFIG_MAP" ] || [ "$AUTH_MODE" = "API_AND_CONFIG_MAP" ]; then
    if AWS_AUTH_YAML=$(kubectl get configmap aws-auth -n kube-system -o yaml 2>&1); then
        AWS_AUTH_BACKUP_FILE="${SCRIPT_DIR}/aws-auth-backup-${CLUSTER_NAME}-$(date +%Y%m%d-%H%M%S).yaml"
        printf '%s\n' "$AWS_AUTH_YAML" > "$AWS_AUTH_BACKUP_FILE"
        if ! AWS_AUTH_ENTRIES_JSON=$(parse_aws_auth_entries "$AWS_AUTH_YAML"); then
            fail "Failed to parse aws-auth mapRoles. No changes were made."
        fi
        echo -e "${GREEN}✅ aws-auth backup created: ${AWS_AUTH_BACKUP_FILE}${NC}"
    elif is_kubernetes_not_found "$AWS_AUTH_YAML"; then
        AWS_AUTH_YAML=""
        echo -e "${YELLOW}⚠️  aws-auth ConfigMap not found${NC}"
    else
        echo "$AWS_AUTH_YAML" >&2
        fail "Failed to read aws-auth. No changes were made."
    fi
fi

if [ "$AUTH_MODE" = "API_AND_CONFIG_MAP" ] && \
    [ "$(jq -r --arg arn "$IAM_ROLE_ARN" '[.[]? | select(.rolearn == $arn)] | length' <<< "$AWS_AUTH_ENTRIES_JSON")" -ne 0 ]; then
    fail "The dual-mode cluster also contains an aws-auth mapping for this role. Remove that legacy mapping before enrolling with an EKS access entry."
fi
echo ""

echo -e "${BLUE}📋 Step 5: Validating existing enrollment and RBAC state...${NC}"
if [ "$AUTH_MODE" = "API" ] || [ "$AUTH_MODE" = "API_AND_CONFIG_MAP" ]; then
    if [ "$ACCESS_ENTRY_EXISTS" = true ]; then
        ORIGINAL_GROUPS_JSON=$(jq -c '.accessEntry.kubernetesGroups // []' <<< "$ACCESS_ENTRY_JSON")
        EFFECTIVE_USERNAME=$(jq -r '.accessEntry.username // empty' <<< "$ACCESS_ENTRY_JSON")
        if [ -z "$EFFECTIVE_USERNAME" ]; then
            EFFECTIVE_USERNAME="$K8S_USERNAME"
        fi
        case "$EFFECTIVE_USERNAME" in
            *'{{SessionName}}'*|*'{{SessionNameRaw}}'*)
                fail "The existing access entry uses a session-templated username whose direct RBAC bindings cannot be proven read-only. Use a dedicated fixed username before enrollment."
                ;;
        esac
        POLICY_COUNT=$(jq -r '(.associatedAccessPolicies // []) | length' <<< "$ACCESS_POLICIES_JSON")
        if [ "$POLICY_COUNT" -ne 0 ]; then
            jq -r '.associatedAccessPolicies[] | "  - \(.policyArn) scope=\(.accessScope.type)"' \
                <<< "$ACCESS_POLICIES_JSON" >&2
            fail "The AgentCore access entry has policy-based permissions. Refusing to claim or broaden ambiguous effective access."
        fi
        if [ "$IAM_ROLE_NAME" != "$DEFAULT_IAM_ROLE_NAME" ] && \
            [ "$EFFECTIVE_USERNAME" = "$LEGACY_K8S_USERNAME" ] && \
            jq -e --arg group "$LEGACY_K8S_GROUP" \
                '(.accessEntry.kubernetesGroups // []) == [$group]' \
                <<< "$ACCESS_ENTRY_JSON" > /dev/null; then
            fail "A legacy custom-role access entry was detected. Deregister this role with --role-name, then re-enroll it to migrate to principal-specific RBAC."
        fi
        OTHER_GROUPS_JSON=$(jq -c --arg group "$K8S_GROUP" \
            '[.accessEntry.kubernetesGroups[]? | select(. != $group)]' <<< "$ACCESS_ENTRY_JSON")
        if [ "$(jq -r 'length' <<< "$OTHER_GROUPS_JSON")" -ne 0 ]; then
            echo "Other Kubernetes groups: $(jq -r 'join(", ")' <<< "$OTHER_GROUPS_JSON")" >&2
            fail "The AgentCore access entry has unrelated Kubernetes groups. Refusing to modify ambiguous permissions."
        fi
        if jq -e --arg group "$K8S_GROUP" \
            '(.accessEntry.kubernetesGroups // []) | index($group) != null' \
            <<< "$ACCESS_ENTRY_JSON" > /dev/null; then
            ALREADY_ENROLLED=true
        fi
    fi
elif [ "$AUTH_MODE" = "CONFIG_MAP" ]; then
    ROLE_MAPPING_COUNT=$(jq -r --arg arn "$IAM_ROLE_ARN" \
        '[.[]? | select(.rolearn == $arn)] | length' <<< "$AWS_AUTH_ENTRIES_JSON")
    EXPECTED_MAPPING_COUNT=$(jq -r \
        --arg arn "$IAM_ROLE_ARN" \
        --arg username "$K8S_USERNAME" \
        --arg group "$K8S_GROUP" '
        [.[]? | select(
          .rolearn == $arn and
          .username == $username and
          ((.groups // []) == [$group]))] | length
        ' <<< "$AWS_AUTH_ENTRIES_JSON")
    LEGACY_MAPPING_COUNT=$(jq -r \
        --arg arn "$IAM_ROLE_ARN" \
        --arg username "$LEGACY_K8S_USERNAME" \
        --arg group "$LEGACY_K8S_GROUP" '
        [.[]? | select(
          .rolearn == $arn and
          .username == $username and
          ((.groups // []) == [$group]))] | length
        ' <<< "$AWS_AUTH_ENTRIES_JSON")
    if [ "$ROLE_MAPPING_COUNT" -ne 0 ]; then
        if [ "$IAM_ROLE_NAME" != "$DEFAULT_IAM_ROLE_NAME" ] && \
            [ "$ROLE_MAPPING_COUNT" -eq 1 ] && [ "$LEGACY_MAPPING_COUNT" -eq 1 ]; then
            fail "A legacy custom-role aws-auth mapping was detected. Deregister this role with --role-name, then re-enroll it to migrate to principal-specific RBAC."
        fi
        if [ "$ROLE_MAPPING_COUNT" -ne 1 ] || [ "$EXPECTED_MAPPING_COUNT" -ne 1 ]; then
            fail "The existing aws-auth entry for this role does not exactly match the expected username and read-only group."
        fi
        ALREADY_ENROLLED=true
    fi
fi

ROLE_JSON=""
if ROLE_JSON=$(kubectl get clusterrole "$CLUSTER_ROLE_NAME" -o json 2>&1); then
    ROLE_EXISTED_BEFORE=true
    if ! jq -e --arg managed "$MANAGED_LABEL_VALUE" '
        .metadata.labels["app.kubernetes.io/managed-by"] == $managed or
        (.metadata.labels["app.kubernetes.io/name"] == "game-agent" and
         .metadata.labels["app.kubernetes.io/component"] == "rbac")
    ' <<< "$ROLE_JSON" > /dev/null; then
        fail "A same-named ClusterRole exists but is not owned by Game Agent enrollment."
    fi
elif ! is_kubernetes_not_found "$ROLE_JSON"; then
    echo "$ROLE_JSON" >&2
    fail "Failed to inspect the existing monitoring ClusterRole. No changes were made."
fi

BINDING_JSON=""
if BINDING_JSON=$(kubectl get clusterrolebinding "$CLUSTER_ROLE_BINDING_NAME" -o json 2>&1); then
    BINDING_EXISTED_BEFORE=true
    if ! jq -e --arg role "$CLUSTER_ROLE_NAME" --arg group "$K8S_GROUP" '
        .roleRef.kind == "ClusterRole" and .roleRef.name == $role and
        ([.subjects[]? | select(.kind == "Group" and .name == $group)] | length) == 1 and
        ([.subjects[]?] | length) == 1
    ' <<< "$BINDING_JSON" > /dev/null; then
        fail "A same-named ClusterRoleBinding exists with unexpected subjects or roleRef."
    fi
    if [ "$IAM_ROLE_NAME" != "$DEFAULT_IAM_ROLE_NAME" ] && \
        ! jq -e --arg managed "$MANAGED_LABEL_VALUE" \
            '.metadata.labels["app.kubernetes.io/managed-by"] == $managed' \
            <<< "$BINDING_JSON" > /dev/null; then
        fail "A custom-role ClusterRoleBinding exists without the Game Agent ownership label."
    fi
elif ! is_kubernetes_not_found "$BINDING_JSON"; then
    echo "$BINDING_JSON" >&2
    fail "Failed to inspect the principal-specific ClusterRoleBinding. No changes were made."
fi

if ! ALL_BINDINGS_JSON=$(kubectl get rolebindings,clusterrolebindings --all-namespaces -o json 2>&1); then
    echo "$ALL_BINDINGS_JSON" >&2
    fail "Failed to inspect existing RBAC bindings."
fi
UNRELATED_BINDINGS_JSON=$(jq -c \
    --arg group "$K8S_GROUP" \
    --arg user "$EFFECTIVE_USERNAME" \
    --arg expected "$CLUSTER_ROLE_BINDING_NAME" '
    [.items[]? |
     select(any(.subjects[]?;
       (.kind == "Group" and .name == $group) or
       (.kind == "User" and .name == $user))) |
     select(.kind != "ClusterRoleBinding" or .metadata.name != $expected)]
    ' <<< "$ALL_BINDINGS_JSON")
if [ "$(jq -r 'length' <<< "$UNRELATED_BINDINGS_JSON")" -ne 0 ]; then
    jq -r '.[] | "  - \(.kind) \(.metadata.namespace // "<cluster>")/\(.metadata.name)"' \
        <<< "$UNRELATED_BINDINGS_JSON" >&2
    fail "The monitoring group or access-entry username appears in unrelated RBAC bindings."
fi

if [ "$ALREADY_ENROLLED" = true ]; then
    echo -e "${GREEN}✅ AgentCore authentication mapping already exists${NC}"
else
    echo "AgentCore authentication mapping is not enrolled yet"
fi
echo ""

echo -e "${BLUE}📋 Step 6: Applying principal-specific monitoring RBAC...${NC}"
if [ "$ROLE_EXISTED_BEFORE" = false ]; then
    ROLE_CREATED_BY_RUN=true
fi
if [ "$BINDING_EXISTED_BEFORE" = false ]; then
    BINDING_CREATED_BY_RUN=true
fi
if ! kubectl apply -f "$MONITORING_ROLE_FILE" > /dev/null; then
    rollback_after_failure
    fail "Failed to apply the monitoring ClusterRole."
fi
if ! printf '%s\n' "$BINDING_MANIFEST" | kubectl apply -f - > /dev/null; then
    rollback_after_failure
    fail "Failed to apply the principal-specific ClusterRoleBinding."
fi
echo -e "${GREEN}✅ Principal-specific monitoring RBAC configured${NC}"
echo ""

echo -e "${BLUE}📋 Step 7: Verifying least-privilege RBAC before authentication...${NC}"
if ! kubectl get clusterrole "$CLUSTER_ROLE_NAME" > /dev/null 2>&1 || \
    ! kubectl get clusterrolebinding "$CLUSTER_ROLE_BINDING_NAME" > /dev/null 2>&1; then
    rollback_after_failure
    fail "Monitoring RBAC objects were not found after apply."
fi

if ! CAN_LIST_PODS=$(checked_can_i list pods --as="$EFFECTIVE_USERNAME" --as-group="$K8S_GROUP"); then
    rollback_after_failure
    fail "Failed to verify pod read access for the monitoring identity."
fi
if ! CAN_LIST_NODES=$(checked_can_i list nodes --as="$EFFECTIVE_USERNAME" --as-group="$K8S_GROUP"); then
    rollback_after_failure
    fail "Failed to verify node read access for the monitoring identity."
fi
if ! CAN_DELETE_PODS=$(checked_can_i delete pods --all-namespaces --as="$EFFECTIVE_USERNAME" --as-group="$K8S_GROUP"); then
    rollback_after_failure
    fail "Failed to verify pod delete denial for the monitoring identity."
fi
if ! CAN_CREATE_PODS=$(checked_can_i create pods --all-namespaces --as="$EFFECTIVE_USERNAME" --as-group="$K8S_GROUP"); then
    rollback_after_failure
    fail "Failed to verify pod create denial for the monitoring identity."
fi
if ! CAN_GET_SECRETS=$(checked_can_i get secrets --all-namespaces --as="$EFFECTIVE_USERNAME" --as-group="$K8S_GROUP"); then
    rollback_after_failure
    fail "Failed to verify secret read denial for the monitoring identity."
fi
if ! CAN_LIST_SECRETS=$(checked_can_i list secrets --all-namespaces --as="$EFFECTIVE_USERNAME" --as-group="$K8S_GROUP"); then
    rollback_after_failure
    fail "Failed to verify secret list denial for the monitoring identity."
fi
if ! CAN_IMPERSONATE=$(checked_can_i impersonate users --as="$EFFECTIVE_USERNAME" --as-group="$K8S_GROUP"); then
    rollback_after_failure
    fail "Failed to verify impersonation denial for the monitoring identity."
fi

if [ "$CAN_LIST_PODS" != "yes" ] || [ "$CAN_LIST_NODES" != "yes" ]; then
    rollback_after_failure
    fail "The monitoring RBAC does not grant the required pod and node read access."
fi
if [ "$CAN_DELETE_PODS" = "yes" ] || [ "$CAN_CREATE_PODS" = "yes" ] || \
    [ "$CAN_GET_SECRETS" = "yes" ] || [ "$CAN_LIST_SECRETS" = "yes" ] || \
    [ "$CAN_IMPERSONATE" = "yes" ]; then
    rollback_after_failure
    fail "The monitoring identity has mutating, secret, or impersonation access."
fi

echo -e "${GREEN}✅ Can list pods and nodes${NC}"
echo -e "${GREEN}✅ Cannot create/delete pods, read secrets, or impersonate users${NC}"
echo ""

echo -e "${BLUE}📋 Step 8: Configuring cluster authentication...${NC}"
if [ "$AUTH_MODE" = "API" ] || [ "$AUTH_MODE" = "API_AND_CONFIG_MAP" ]; then
    if [ "$ALREADY_ENROLLED" = true ]; then
        echo -e "${GREEN}✅ EKS access entry already contains the monitoring group${NC}"
    elif [ "$ACCESS_ENTRY_EXISTS" = true ]; then
        UPDATED_GROUPS_JSON=$(jq -c --arg group "$K8S_GROUP" \
            '(.accessEntry.kubernetesGroups // []) + [$group] | unique' <<< "$ACCESS_ENTRY_JSON")
        if ! aws eks update-access-entry \
            --cluster-name "$CLUSTER_NAME" \
            --principal-arn "$IAM_ROLE_ARN" \
            --kubernetes-groups "$UPDATED_GROUPS_JSON" \
            --region "$REGION" > /dev/null; then
            if ! rollback_rbac_change; then :; fi
            fail "Failed to add the monitoring group to the EKS access entry."
        fi
        ACCESS_GROUP_ADDED_BY_RUN=true
        echo -e "${GREEN}✅ EKS access entry updated${NC}"
    else
        if ! aws eks create-access-entry \
            --cluster-name "$CLUSTER_NAME" \
            --principal-arn "$IAM_ROLE_ARN" \
            --kubernetes-groups "$K8S_GROUP" \
            --username "$K8S_USERNAME" \
            --type STANDARD \
            --tags "${MANAGED_TAG_KEY}=true" \
            --region "$REGION" > /dev/null; then
            if ! rollback_rbac_change; then :; fi
            fail "Failed to create the EKS access entry."
        fi
        ACCESS_ENTRY_CREATED_BY_RUN=true
        echo -e "${GREEN}✅ EKS access entry created${NC}"
    fi

    if wait_for_access_group; then
        echo -e "${GREEN}✅ EKS access-entry configuration observed${NC}"
    else
        echo -e "${YELLOW}⚠️  The EKS API accepted the change, but it was not observable before the propagation timeout.${NC}" >&2
        echo "Verified read-only RBAC remains configured. Re-run enrollment to verify state instead of creating a duplicate entry." >&2
        exit 1
    fi
elif [ "$ALREADY_ENROLLED" = true ]; then
    echo -e "${GREEN}✅ aws-auth identity mapping already exists${NC}"
elif command -v eksctl > /dev/null 2>&1; then
    if eksctl create iamidentitymapping \
        --cluster "$CLUSTER_NAME" \
        --region "$REGION" \
        --arn "$IAM_ROLE_ARN" \
        --username "$K8S_USERNAME" \
        --group "$K8S_GROUP" > /dev/null; then
        IDENTITY_MAPPING_CREATED_BY_RUN=true
        echo -e "${GREEN}✅ IAM identity mapping created${NC}"
    else
        if ! rollback_rbac_change; then :; fi
        fail "Failed to create the IAM identity mapping."
    fi
else
    echo -e "${YELLOW}⚠️  eksctl not found; a manual aws-auth update is required.${NC}"
    echo "Run: kubectl edit configmap aws-auth -n kube-system"
    echo "Add this entry to mapRoles:"
    echo "---"
    echo "- groups:"
    echo "  - ${K8S_GROUP}"
    echo "  rolearn: ${IAM_ROLE_ARN}"
    echo "  username: ${K8S_USERNAME}"
    echo "---"
    read -r -p "Press Enter after updating the ConfigMap, or Ctrl+C to abort..."
    if ! CURRENT_AWS_AUTH=$(kubectl get configmap aws-auth -n kube-system -o yaml 2>&1); then
        echo "$CURRENT_AWS_AUTH" >&2
        if ! rollback_rbac_change; then :; fi
        fail "Failed to verify the manual aws-auth update; inspect aws-auth for residual access."
    fi
    if ! CURRENT_AWS_AUTH_ENTRIES=$(parse_aws_auth_entries "$CURRENT_AWS_AUTH"); then
        if ! rollback_rbac_change; then :; fi
        fail "Failed to parse aws-auth after the manual update; inspect aws-auth for residual access."
    fi
    ROLE_MAPPING_COUNT=$(jq -r --arg arn "$IAM_ROLE_ARN" \
        '[.[]? | select(.rolearn == $arn)] | length' <<< "$CURRENT_AWS_AUTH_ENTRIES")
    EXPECTED_MAPPING_COUNT=$(jq -r \
        --arg arn "$IAM_ROLE_ARN" \
        --arg username "$K8S_USERNAME" \
        --arg group "$K8S_GROUP" '
        [.[]? | select(
          .rolearn == $arn and
          .username == $username and
          ((.groups // []) == [$group]))] | length
        ' <<< "$CURRENT_AWS_AUTH_ENTRIES")
    if [ "$ROLE_MAPPING_COUNT" -ne 0 ]; then
        IDENTITY_MAPPING_CREATED_BY_RUN=true
    fi
    if [ "$ROLE_MAPPING_COUNT" -ne 1 ] || [ "$EXPECTED_MAPPING_COUNT" -ne 1 ]; then
        rollback_after_failure
        fail "The manual aws-auth update does not exactly match the expected role, username, and read-only group."
    fi
    IDENTITY_MAPPING_CREATED_BY_RUN=true
fi
echo ""

if [ "$ENABLE_AUDIT_LOGS" = true ]; then
    echo -e "${BLUE}📋 Step 9: Enabling audit logging...${NC}"
    if UPDATE_ID=$(aws eks update-cluster-config \
        --name "$CLUSTER_NAME" \
        --region "$REGION" \
        --logging '{"clusterLogging":[{"types":["api","audit","authenticator"],"enabled":true}]}' \
        --query 'update.id' \
        --output text 2>&1); then
        echo -e "${GREEN}✅ Audit logging update submitted (Update ID: ${UPDATE_ID})${NC}"
        echo -e "${YELLOW}⏱️  Update in progress (typically 5-10 minutes)${NC}"
        echo -e "${BLUE}📋 Step 10: Setting log retention to ${LOG_RETENTION_DAYS} days...${NC}"
        sleep 5
        if aws logs put-retention-policy \
            --log-group-name "/aws/eks/${CLUSTER_NAME}/cluster" \
            --retention-in-days "$LOG_RETENTION_DAYS" \
            --region "$REGION" > /dev/null 2>&1; then
            echo -e "${GREEN}✅ Log retention set to ${LOG_RETENTION_DAYS} days${NC}"
        else
            echo -e "${YELLOW}⚠️  The log group is not ready yet. Set retention after the EKS update completes:${NC}"
            echo "   aws logs put-retention-policy --log-group-name /aws/eks/${CLUSTER_NAME}/cluster --retention-in-days ${LOG_RETENTION_DAYS} --region ${REGION}"
        fi
    else
        echo "$UPDATE_ID" >&2
        fail "Failed to enable EKS audit logging."
    fi
    echo ""
fi

echo -e "${GREEN}✅ Enrollment complete!${NC}"
echo ""
echo -e "${BLUE}📊 Summary:${NC}"
echo "  Cluster: ${CLUSTER_NAME}"
echo "  Region: ${REGION}"
echo "  Authentication: ${AUTH_MODE}"
echo "  Authorization: Kubernetes RBAC (read-only)"
echo "  IAM Role: ${IAM_ROLE_ARN}"
echo "  Kubernetes User: ${EFFECTIVE_USERNAME}"
echo "  Kubernetes Group: ${K8S_GROUP}"
echo "  ClusterRoleBinding: ${CLUSTER_ROLE_BINDING_NAME}"
if [ "$ENABLE_AUDIT_LOGS" = true ]; then
    echo "  Audit Logs: Update submitted (${LOG_RETENTION_DAYS} day retention requested)"
fi
echo ""
echo -e "${BLUE}🔍 RBAC verification:${NC}"
echo "  kubectl auth can-i list pods --as=${EFFECTIVE_USERNAME} --as-group=${K8S_GROUP}"
echo "  kubectl auth can-i delete pods --all-namespaces --as=${EFFECTIVE_USERNAME} --as-group=${K8S_GROUP}"
echo ""
echo -e "${BLUE}📋 Backups:${NC}"
if [ -n "$ACCESS_ENTRY_BACKUP_FILE" ]; then
    echo "  ${ACCESS_ENTRY_BACKUP_FILE}"
fi
if [ -n "$AWS_AUTH_BACKUP_FILE" ]; then
    echo "  ${AWS_AUTH_BACKUP_FILE}"
fi
if [ -z "$ACCESS_ENTRY_BACKUP_FILE" ] && [ -z "$AWS_AUTH_BACKUP_FILE" ]; then
    echo "  No prior AgentCore access mapping existed"
fi
echo ""
echo -e "${BLUE}🔄 To deregister:${NC}"
printf '  %q %q %q' "${SCRIPT_DIR}/deregister-cluster.sh" "$CLUSTER_NAME" "$REGION"
if [ "$IAM_ROLE_NAME" != "$DEFAULT_IAM_ROLE_NAME" ]; then
    printf ' --role-name %q' "$IAM_ROLE_NAME"
fi
if [ -n "$KUBE_ROLE_ARN" ]; then
    printf ' --kube-role-arn %q' "$KUBE_ROLE_ARN"
fi
printf '\n'
