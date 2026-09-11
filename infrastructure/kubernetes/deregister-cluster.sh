#!/bin/bash
# Deregister an EKS cluster from Game Agent AgentCore Runtime.
# Removes only the authentication and RBAC objects managed by enrollment.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_IAM_ROLE_NAME="game-agent-agentcore-execution-role"
IAM_ROLE_NAME="${DEFAULT_IAM_ROLE_NAME}"
LEGACY_K8S_USERNAME="game-agent-agentcore-user"
LEGACY_K8S_GROUP="game-agent-monitoring-group"
LEGACY_CLUSTER_ROLE_BINDING_NAME="game-agent-monitoring-binding"
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
    echo -e "${RED}Usage: $0 <cluster-name> <region> [--role-name ROLE] [--kube-role-arn ARN] [--disable-audit-logs]${NC}"
    echo ""
    echo "Examples:"
    echo "  $0 my-cluster us-west-2"
    echo "  $0 my-cluster us-west-2 --kube-role-arn arn:aws:iam::123456789012:role/eks-admin"
    echo "  $0 my-cluster us-west-2 --disable-audit-logs"
    echo "  $0 my-cluster us-west-2 --role-name my-custom-role"
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

wait_for_access_group_removal() {
    local attempt
    local current_entry

    for ((attempt = 1; attempt <= ACCESS_ENTRY_POLL_ATTEMPTS; attempt++)); do
        if current_entry=$(aws eks describe-access-entry \
            --cluster-name "$CLUSTER_NAME" \
            --principal-arn "$IAM_ROLE_ARN" \
            --region "$REGION" \
            --output json 2>&1); then
            if ! jq -e --arg group "$AUTH_GROUP_TO_REMOVE" \
                '(.accessEntry.kubernetesGroups // []) | index($group) != null' \
                <<< "$current_entry" > /dev/null; then
                return 0
            fi
        elif is_access_entry_not_found "$current_entry"; then
            return 0
        else
            echo "$current_entry" >&2
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
DISABLE_AUDIT_LOGS=false
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
        --disable-audit-logs)
            DISABLE_AUDIT_LOGS=true
            shift
            ;;
        *)
            fail "Unknown option: $1"
            ;;
    esac
done

ACCESS_ENTRY_EXISTS=false
ACCESS_ENTRY_JSON=""
ACCESS_POLICIES_JSON='{"associatedAccessPolicies":[]}'
ACCESS_ENTRY_BACKUP_FILE=""
AWS_AUTH_BACKUP_FILE=""
AWS_AUTH_YAML=""
AWS_AUTH_ENTRIES_JSON='[]'
ACCESS_ENTRY_DELETED=false
SESSION_TEMPLATE_USERNAME=false
RETAINED_POLICY_ACCESS=false
RETAINED_GROUP_ACCESS=false
RETAINED_BINDING_ACCESS=false
LEGACY_MAPPING_PRESENT=false
ROLE_EXISTS=false
BINDING_EXISTS=false
BINDING_PRESERVED=false

printf '%b\n' "${BLUE}🔄 Game Agent EKS Cluster Deregistration${NC}"
echo "================================================"
echo ""

echo -e "${BLUE}📋 Step 1: Checking prerequisites...${NC}"
for command_name in aws kubectl jq yq; do
    if ! command -v "$command_name" > /dev/null 2>&1; then
        fail "${command_name} not found. Install it before deregistering a cluster."
    fi
done
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
    CLUSTER_ROLE_BINDING_NAME="$LEGACY_CLUSTER_ROLE_BINDING_NAME"
else
    PRINCIPAL_ID=$(principal_fingerprint)
    K8S_USERNAME="game-agent-${PRINCIPAL_ID}"
    K8S_GROUP="game-agent-monitoring-${PRINCIPAL_ID}"
    CLUSTER_ROLE_BINDING_NAME="game-agent-monitoring-${PRINCIPAL_ID}"
fi
EFFECTIVE_USERNAME="$K8S_USERNAME"
AUTH_USERNAME_TO_REMOVE="$K8S_USERNAME"
AUTH_GROUP_TO_REMOVE="$K8S_GROUP"
PRESERVED_LEGACY_BINDING_NAME=""

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
echo "  Disable Audit Logs: ${DISABLE_AUDIT_LOGS}"
echo ""

echo -e "${YELLOW}⚠️  This removes this IAM role's authentication mapping and principal-specific RBAC binding.${NC}"
echo "It preserves namespaces, workloads, unrelated access-entry permissions, and RBAC used by other enrolled roles."
read -r -p "Are you sure you want to continue? (y/n) " -n 1 REPLY
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    fail "Aborted"
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

echo -e "${BLUE}📋 Step 3: Verifying Kubernetes cleanup permissions and object ownership...${NC}"
if ! CLUSTER_INFO_OUTPUT=$(kubectl cluster-info 2>&1); then
    echo "$CLUSTER_INFO_OUTPUT" >&2
    fail "Cannot connect to the cluster. Use --kube-role-arn with a role that can remove cluster RBAC."
fi
if ! CAN_DELETE_ROLE=$(checked_can_i delete clusterroles.rbac.authorization.k8s.io --all-namespaces); then
    fail "Failed to verify ClusterRole cleanup permission."
fi
if ! CAN_DELETE_BINDING=$(checked_can_i delete clusterrolebindings.rbac.authorization.k8s.io --all-namespaces); then
    fail "Failed to verify ClusterRoleBinding cleanup permission."
fi
if [ "$CAN_DELETE_ROLE" != "yes" ] || [ "$CAN_DELETE_BINDING" != "yes" ]; then
    fail "The active Kubernetes identity cannot remove monitoring RBAC."
fi

ROLE_JSON=""
if ROLE_JSON=$(kubectl get clusterrole "$CLUSTER_ROLE_NAME" -o json 2>&1); then
    ROLE_EXISTS=true
    if ! jq -e --arg managed "$MANAGED_LABEL_VALUE" '
        .metadata.labels["app.kubernetes.io/managed-by"] == $managed or
        (.metadata.labels["app.kubernetes.io/name"] == "game-agent" and
         .metadata.labels["app.kubernetes.io/component"] == "rbac")
    ' <<< "$ROLE_JSON" > /dev/null; then
        fail "The monitoring ClusterRole is not owned by Game Agent enrollment; refusing to delete it."
    fi
elif ! is_kubernetes_not_found "$ROLE_JSON"; then
    echo "$ROLE_JSON" >&2
    fail "Failed to inspect the monitoring ClusterRole. No changes were made."
fi

BINDING_JSON=""
if BINDING_JSON=$(kubectl get clusterrolebinding "$CLUSTER_ROLE_BINDING_NAME" -o json 2>&1); then
    BINDING_EXISTS=true
    if ! jq -e --arg role "$CLUSTER_ROLE_NAME" --arg group "$K8S_GROUP" '
        .roleRef.kind == "ClusterRole" and .roleRef.name == $role and
        ([.subjects[]? | select(.kind == "Group" and .name == $group)] | length) == 1 and
        ([.subjects[]?] | length) == 1
    ' <<< "$BINDING_JSON" > /dev/null; then
        fail "The principal-specific ClusterRoleBinding has unexpected subjects or roleRef; refusing to delete it."
    fi
    if [ "$IAM_ROLE_NAME" != "$DEFAULT_IAM_ROLE_NAME" ] && \
        ! jq -e --arg managed "$MANAGED_LABEL_VALUE" \
            '.metadata.labels["app.kubernetes.io/managed-by"] == $managed' \
            <<< "$BINDING_JSON" > /dev/null; then
        fail "The custom-role ClusterRoleBinding lacks the Game Agent ownership label; refusing to delete it."
    fi
elif ! is_kubernetes_not_found "$BINDING_JSON"; then
    echo "$BINDING_JSON" >&2
    fail "Failed to inspect the principal-specific ClusterRoleBinding. No changes were made."
fi
echo -e "${GREEN}✅ Kubernetes cleanup permissions and ownership verified${NC}"
echo ""

echo -e "${BLUE}📋 Step 4: Backing up cluster access configuration...${NC}"
if [ "$AUTH_MODE" = "API" ] || [ "$AUTH_MODE" = "API_AND_CONFIG_MAP" ]; then
    if ACCESS_ENTRY_JSON=$(aws eks describe-access-entry \
        --cluster-name "$CLUSTER_NAME" \
        --principal-arn "$IAM_ROLE_ARN" \
        --region "$REGION" \
        --output json 2>&1); then
        ACCESS_ENTRY_EXISTS=true
        EFFECTIVE_USERNAME=$(jq -r '.accessEntry.username // empty' <<< "$ACCESS_ENTRY_JSON")
        if [ -z "$EFFECTIVE_USERNAME" ]; then
            EFFECTIVE_USERNAME="$K8S_USERNAME"
        fi
        case "$EFFECTIVE_USERNAME" in
            *'{{SessionName}}'*|*'{{SessionNameRaw}}'*) SESSION_TEMPLATE_USERNAME=true ;;
        esac
        if [ "$IAM_ROLE_NAME" != "$DEFAULT_IAM_ROLE_NAME" ] && \
            [ "$EFFECTIVE_USERNAME" = "$LEGACY_K8S_USERNAME" ] && \
            jq -e --arg group "$LEGACY_K8S_GROUP" \
                '(.accessEntry.kubernetesGroups // []) == [$group]' \
                <<< "$ACCESS_ENTRY_JSON" > /dev/null; then
            AUTH_USERNAME_TO_REMOVE="$LEGACY_K8S_USERNAME"
            AUTH_GROUP_TO_REMOVE="$LEGACY_K8S_GROUP"
            PRESERVED_LEGACY_BINDING_NAME="$LEGACY_CLUSTER_ROLE_BINDING_NAME"
            BINDING_PRESERVED=true
        fi
        if ! ACCESS_POLICIES_JSON=$(aws eks list-associated-access-policies \
            --cluster-name "$CLUSTER_NAME" \
            --principal-arn "$IAM_ROLE_ARN" \
            --region "$REGION" \
            --output json 2>&1); then
            echo "$ACCESS_POLICIES_JSON" >&2
            fail "Failed to read policies associated with the access entry. No changes were made."
        fi
        ACCESS_ENTRY_BACKUP_FILE="${SCRIPT_DIR}/access-entry-backup-${CLUSTER_NAME}-deregister-$(date +%Y%m%d-%H%M%S).json"
        jq -n \
            --argjson entry "$ACCESS_ENTRY_JSON" \
            --argjson policies "$ACCESS_POLICIES_JSON" \
            '{accessEntry: $entry.accessEntry, associatedAccessPolicies: ($policies.associatedAccessPolicies // [])}' \
            > "$ACCESS_ENTRY_BACKUP_FILE"
        echo -e "${GREEN}✅ Access entry and policy backup created: ${ACCESS_ENTRY_BACKUP_FILE}${NC}"
    elif is_access_entry_not_found "$ACCESS_ENTRY_JSON"; then
        ACCESS_ENTRY_JSON=""
        echo -e "${YELLOW}⚠️  AgentCore access entry does not exist${NC}"
    else
        echo "$ACCESS_ENTRY_JSON" >&2
        fail "Failed to read the access entry. No changes were made."
    fi
fi

if [ "$AUTH_MODE" = "CONFIG_MAP" ] || [ "$AUTH_MODE" = "API_AND_CONFIG_MAP" ]; then
    if AWS_AUTH_YAML=$(kubectl get configmap aws-auth -n kube-system -o yaml 2>&1); then
        AWS_AUTH_BACKUP_FILE="${SCRIPT_DIR}/aws-auth-backup-${CLUSTER_NAME}-deregister-$(date +%Y%m%d-%H%M%S).yaml"
        printf '%s\n' "$AWS_AUTH_YAML" > "$AWS_AUTH_BACKUP_FILE"
        if ! AWS_AUTH_ENTRIES_JSON=$(parse_aws_auth_entries "$AWS_AUTH_YAML"); then
            fail "Failed to parse aws-auth mapRoles. No changes were made."
        fi
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
            if [ "$ROLE_MAPPING_COUNT" -eq 1 ] && [ "$EXPECTED_MAPPING_COUNT" -eq 1 ]; then
                LEGACY_MAPPING_PRESENT=true
            elif [ "$IAM_ROLE_NAME" != "$DEFAULT_IAM_ROLE_NAME" ] && \
                [ "$ROLE_MAPPING_COUNT" -eq 1 ] && [ "$LEGACY_MAPPING_COUNT" -eq 1 ]; then
                LEGACY_MAPPING_PRESENT=true
                AUTH_USERNAME_TO_REMOVE="$LEGACY_K8S_USERNAME"
                AUTH_GROUP_TO_REMOVE="$LEGACY_K8S_GROUP"
                PRESERVED_LEGACY_BINDING_NAME="$LEGACY_CLUSTER_ROLE_BINDING_NAME"
                BINDING_PRESERVED=true
            else
                fail "The aws-auth entry for this role does not match either the current principal-specific tuple or the supported legacy read-only tuple; refusing to delete it."
            fi
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
echo ""

echo -e "${BLUE}📋 Step 5: Removing this role's authentication mappings...${NC}"
# On dual-mode clusters, remove the ConfigMap mapping first so deleting an
# access entry cannot reactivate a dormant legacy grant.
if [ "$LEGACY_MAPPING_PRESENT" = true ]; then
    if command -v eksctl > /dev/null 2>&1; then
        if ! eksctl delete iamidentitymapping \
            --cluster "$CLUSTER_NAME" \
            --region "$REGION" \
            --arn "$IAM_ROLE_ARN" > /dev/null 2>&1; then
            fail "Failed to remove the exact aws-auth mapping; the EKS access entry was left unchanged."
        fi
    else
        echo -e "${YELLOW}⚠️  eksctl not found; remove the exact aws-auth entry whose rolearn is ${IAM_ROLE_ARN}.${NC}"
        read -r -p "Press Enter after removing the entry, or Ctrl+C to abort..."
    fi
    if CURRENT_AWS_AUTH=$(kubectl get configmap aws-auth -n kube-system -o yaml 2>&1); then
        if aws_auth_has_role "$CURRENT_AWS_AUTH"; then
            fail "aws-auth still contains this role; the EKS access entry was left unchanged."
        fi
    elif ! is_kubernetes_not_found "$CURRENT_AWS_AUTH"; then
        echo "$CURRENT_AWS_AUTH" >&2
        fail "Failed to verify aws-auth cleanup; the EKS access entry was left unchanged."
    fi
    echo -e "${GREEN}✅ Legacy aws-auth mapping removed${NC}"
elif [ "$AUTH_MODE" = "CONFIG_MAP" ]; then
    echo -e "${GREEN}✅ aws-auth mapping was already absent${NC}"
fi

if [ "$AUTH_MODE" = "API" ] || [ "$AUTH_MODE" = "API_AND_CONFIG_MAP" ]; then
    if [ "$ACCESS_ENTRY_EXISTS" = true ]; then
        REMAINING_GROUPS_JSON=$(jq -c --arg group "$AUTH_GROUP_TO_REMOVE" \
            '[.accessEntry.kubernetesGroups[]? | select(. != $group)]' <<< "$ACCESS_ENTRY_JSON")
        GROUP_WAS_PRESENT=false
        if jq -e --arg group "$AUTH_GROUP_TO_REMOVE" \
            '(.accessEntry.kubernetesGroups // []) | index($group) != null' \
            <<< "$ACCESS_ENTRY_JSON" > /dev/null; then
            GROUP_WAS_PRESENT=true
        fi
        POLICY_COUNT=$(jq -r '(.associatedAccessPolicies // []) | length' <<< "$ACCESS_POLICIES_JSON")
        REMAINING_GROUP_COUNT=$(jq -r 'length' <<< "$REMAINING_GROUPS_JSON")
        ENTRY_IS_MANAGED=$(jq -r --arg key "$MANAGED_TAG_KEY" \
            '.accessEntry.tags[$key] // "false"' <<< "$ACCESS_ENTRY_JSON")

        if [ "$ENTRY_IS_MANAGED" = "true" ] && [ "$REMAINING_GROUP_COUNT" -eq 0 ] && [ "$POLICY_COUNT" -eq 0 ]; then
            if ! aws eks delete-access-entry \
                --cluster-name "$CLUSTER_NAME" \
                --principal-arn "$IAM_ROLE_ARN" \
                --region "$REGION" > /dev/null; then
                fail "Failed to delete the script-managed EKS access entry."
            fi
            ACCESS_ENTRY_DELETED=true
            echo -e "${GREEN}✅ Script-managed EKS access entry deleted${NC}"
        elif [ "$GROUP_WAS_PRESENT" = true ]; then
            if ! aws eks update-access-entry \
                --cluster-name "$CLUSTER_NAME" \
                --principal-arn "$IAM_ROLE_ARN" \
                --kubernetes-groups "$REMAINING_GROUPS_JSON" \
                --region "$REGION" > /dev/null; then
                fail "Failed to remove the monitoring group from the EKS access entry."
            fi
            echo -e "${GREEN}✅ Monitoring group removed from the existing EKS access entry${NC}"
        else
            echo -e "${GREEN}✅ Monitoring group was already absent from the EKS access entry${NC}"
        fi

        if [ "$POLICY_COUNT" -ne 0 ]; then
            RETAINED_POLICY_ACCESS=true
            echo -e "${YELLOW}⚠️  Existing access policies were preserved:${NC}"
            jq -r '.associatedAccessPolicies[] | "  - \(.policyArn) scope=\(.accessScope.type)"' \
                <<< "$ACCESS_POLICIES_JSON"
        fi
        if [ "$REMAINING_GROUP_COUNT" -ne 0 ]; then
            RETAINED_GROUP_ACCESS=true
            echo -e "${YELLOW}⚠️  Unrelated Kubernetes groups were preserved: $(jq -r 'join(", ")' <<< "$REMAINING_GROUPS_JSON")${NC}"
        fi

        if ! wait_for_access_group_removal; then
            fail "The access-entry change was accepted but the monitoring group remained observable through the propagation timeout. Re-run deregistration to verify state."
        fi
    else
        echo -e "${GREEN}✅ No EKS access entry to update${NC}"
    fi
elif [ "$LEGACY_MAPPING_PRESENT" = false ]; then
    echo -e "${GREEN}✅ No aws-auth mapping to update${NC}"
fi
if [ "$SESSION_TEMPLATE_USERNAME" = true ] && [ "$ACCESS_ENTRY_DELETED" = false ]; then
    RETAINED_BINDING_ACCESS=true
    echo -e "${YELLOW}⚠️  The preserved access entry uses a session-templated username; expanded-session User bindings cannot be exhaustively verified.${NC}"
fi
echo ""

echo -e "${BLUE}📋 Step 6: Removing principal-specific monitoring RBAC...${NC}"
if [ "$BINDING_EXISTS" = true ]; then
    if [ "$IAM_ROLE_NAME" = "$DEFAULT_IAM_ROLE_NAME" ]; then
        BINDING_PRESERVED=true
        echo -e "${GREEN}✅ Legacy shared ClusterRoleBinding preserved for compatibility with earlier custom-role enrollments${NC}"
    else
        if ! kubectl delete clusterrolebinding "$CLUSTER_ROLE_BINDING_NAME" --ignore-not-found > /dev/null; then
            fail "Failed to remove the principal-specific ClusterRoleBinding."
        fi
        echo -e "${GREEN}✅ Principal-specific ClusterRoleBinding removed${NC}"
    fi
else
    echo -e "${GREEN}✅ Principal-specific ClusterRoleBinding was already absent${NC}"
fi

if ! ROLE_BINDINGS_JSON=$(kubectl get rolebindings,clusterrolebindings --all-namespaces -o json 2>&1); then
    echo "$ROLE_BINDINGS_JSON" >&2
    fail "Failed to inspect remaining ClusterRoleBindings."
fi
ROLE_REFERENCE_COUNT=$(jq -r --arg role "$CLUSTER_ROLE_NAME" \
    '[.items[]? | select(.roleRef.kind == "ClusterRole" and .roleRef.name == $role)] | length' \
    <<< "$ROLE_BINDINGS_JSON")
if [ "$ROLE_EXISTS" = true ] && [ "$ROLE_REFERENCE_COUNT" -eq 0 ]; then
    if ! kubectl delete clusterrole "$CLUSTER_ROLE_NAME" --ignore-not-found > /dev/null; then
        fail "Failed to remove the now-unused monitoring ClusterRole."
    fi
    echo -e "${GREEN}✅ Unused monitoring ClusterRole removed${NC}"
elif [ "$ROLE_REFERENCE_COUNT" -ne 0 ]; then
    echo -e "${GREEN}✅ Shared monitoring ClusterRole preserved for ${ROLE_REFERENCE_COUNT} remaining binding(s)${NC}"
fi
echo ""

echo -e "${BLUE}📋 Step 7: Verifying removal...${NC}"
if [ "$BINDING_PRESERVED" = false ] && \
    kubectl get clusterrolebinding "$CLUSTER_ROLE_BINDING_NAME" > /dev/null 2>&1; then
    fail "Principal-specific ClusterRoleBinding still exists."
fi
if [ "$AUTH_MODE" = "CONFIG_MAP" ] || [ "$AUTH_MODE" = "API_AND_CONFIG_MAP" ]; then
    if CURRENT_AWS_AUTH=$(kubectl get configmap aws-auth -n kube-system -o yaml 2>&1); then
        if aws_auth_has_role "$CURRENT_AWS_AUTH"; then
            fail "aws-auth still contains this role mapping."
        fi
    elif ! is_kubernetes_not_found "$CURRENT_AWS_AUTH"; then
        echo "$CURRENT_AWS_AUTH" >&2
        fail "Failed to verify aws-auth after deregistration."
    fi
fi

if ! ALL_BINDINGS_JSON=$(kubectl get rolebindings,clusterrolebindings --all-namespaces -o json 2>&1); then
    echo "$ALL_BINDINGS_JSON" >&2
    fail "Failed to inspect remaining RBAC bindings."
fi
RETAINED_BINDINGS_JSON=$(jq -c \
    --arg group "$AUTH_GROUP_TO_REMOVE" \
    --arg expected_user "$AUTH_USERNAME_TO_REMOVE" \
    --arg effective_user "$EFFECTIVE_USERNAME" \
    --arg expected_binding "$CLUSTER_ROLE_BINDING_NAME" \
    --arg preserved_legacy_binding "$PRESERVED_LEGACY_BINDING_NAME" '
    [.items[]? |
     select(any(.subjects[]?;
       (.kind == "Group" and .name == $group) or
       (.kind == "User" and (.name == $expected_user or .name == $effective_user)))) |
     select(
       .kind != "ClusterRoleBinding" or
       (.metadata.name != $expected_binding and
        ($preserved_legacy_binding == "" or .metadata.name != $preserved_legacy_binding)))]
    ' <<< "$ALL_BINDINGS_JSON")
if [ "$(jq -r 'length' <<< "$RETAINED_BINDINGS_JSON")" -ne 0 ]; then
    RETAINED_BINDING_ACCESS=true
    echo -e "${YELLOW}⚠️  Other RBAC bindings still reference this enrollment identity:${NC}"
    jq -r '.[] | "  - \(.kind) \(.metadata.namespace // "<cluster>")/\(.metadata.name)"' \
        <<< "$RETAINED_BINDINGS_JSON"
fi
echo -e "${GREEN}✅ Enrollment-managed authentication and binding removal verified${NC}"
echo ""

if [ "$DISABLE_AUDIT_LOGS" = true ]; then
    echo -e "${BLUE}📋 Step 8: Disabling audit logging...${NC}"
    if UPDATE_ID=$(aws eks update-cluster-config \
        --name "$CLUSTER_NAME" \
        --region "$REGION" \
        --logging '{"clusterLogging":[{"types":["api","audit","authenticator"],"enabled":false}]}' \
        --query 'update.id' \
        --output text 2>&1); then
        echo -e "${GREEN}✅ Audit logging update submitted (Update ID: ${UPDATE_ID})${NC}"
        echo -e "${YELLOW}⏱️  Update in progress (typically 5-10 minutes)${NC}"
    else
        echo "$UPDATE_ID" >&2
        fail "Failed to disable EKS audit logging."
    fi
    echo ""
fi

if [ "$RETAINED_POLICY_ACCESS" = true ] || [ "$RETAINED_GROUP_ACCESS" = true ] || \
    [ "$RETAINED_BINDING_ACCESS" = true ]; then
    echo -e "${YELLOW}⚠️  Deregistration is incomplete.${NC}"
    echo "Enrollment-managed access was removed, but this role may retain permissions that predated or were added outside this script."
    echo "Review the backups and current EKS/RBAC state before removing unrelated permissions."
    if [ -n "$ACCESS_ENTRY_BACKUP_FILE" ]; then echo "  ${ACCESS_ENTRY_BACKUP_FILE}"; fi
    if [ -n "$AWS_AUTH_BACKUP_FILE" ]; then echo "  ${AWS_AUTH_BACKUP_FILE}"; fi
    exit 1
fi

echo -e "${GREEN}✅ Deregistration complete!${NC}"
echo ""
echo -e "${BLUE}📊 Summary:${NC}"
echo "  Cluster: ${CLUSTER_NAME}"
echo "  Region: ${REGION}"
echo "  Authentication Mode: ${AUTH_MODE}"
echo "  IAM Role: ${IAM_ROLE_ARN}"
echo "  Authentication mappings: Removed"
if [ "$BINDING_PRESERVED" = true ]; then
    echo "  Legacy shared ClusterRoleBinding: Preserved (inert for this removed role)"
else
    echo "  Principal-specific ClusterRoleBinding: Removed"
fi
echo "  Shared ClusterRole: Removed only when unused"
echo "  Namespaces and workloads: Preserved"
if [ "$DISABLE_AUDIT_LOGS" = true ]; then
    echo "  Audit Logs: Disable update submitted"
fi
echo ""
echo -e "${BLUE}📋 Backups:${NC}"
if [ -n "$ACCESS_ENTRY_BACKUP_FILE" ]; then echo "  ${ACCESS_ENTRY_BACKUP_FILE}"; fi
if [ -n "$AWS_AUTH_BACKUP_FILE" ]; then echo "  ${AWS_AUTH_BACKUP_FILE}"; fi
if [ -z "$ACCESS_ENTRY_BACKUP_FILE" ] && [ -z "$AWS_AUTH_BACKUP_FILE" ]; then
    echo "  No prior AgentCore access mapping existed"
fi
echo ""
echo -e "${BLUE}🔄 To re-enroll:${NC}"
printf '  %q %q %q' "${SCRIPT_DIR}/enroll-cluster.sh" "$CLUSTER_NAME" "$REGION"
if [ "$IAM_ROLE_NAME" != "$DEFAULT_IAM_ROLE_NAME" ]; then
    printf ' --role-name %q' "$IAM_ROLE_NAME"
fi
if [ -n "$KUBE_ROLE_ARN" ]; then
    printf ' --kube-role-arn %q' "$KUBE_ROLE_ARN"
fi
printf '\n'
