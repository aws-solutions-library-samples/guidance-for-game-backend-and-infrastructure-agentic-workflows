#!/bin/bash
# Deregister an EKS cluster from Game Agent AgentCore Runtime
# This script removes Kubernetes API access configuration

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Auto-detect AWS account and IAM role
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
if [ -z "$AWS_ACCOUNT_ID" ]; then
    echo "❌ Failed to get AWS account ID. Check your AWS credentials."
    exit 1
fi

# Default role name (can be overridden with --role-name)
IAM_ROLE_NAME="game-agent-agentcore-execution-role"
IAM_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${IAM_ROLE_NAME}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🔄 Game Agent EKS Cluster Deregistration${NC}"
echo "================================================"
echo ""

# Check arguments
if [ $# -lt 2 ]; then
    echo -e "${RED}Usage: $0 <cluster-name> <region> [--role-name ROLE] [--disable-audit-logs]${NC}"
    echo ""
    echo "Examples:"
    echo "  $0 my-cluster us-west-2"
    echo "  $0 my-cluster us-west-2 --disable-audit-logs"
    echo "  $0 my-cluster us-west-2 --role-name my-custom-role"
    exit 1
fi

CLUSTER_NAME=$1
REGION=$2
DISABLE_AUDIT_LOGS=false

# Parse optional arguments
shift 2
while [[ $# -gt 0 ]]; do
    case $1 in
        --role-name)
            IAM_ROLE_NAME=$2
            IAM_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${IAM_ROLE_NAME}"
            shift 2
            ;;
        --disable-audit-logs)
            DISABLE_AUDIT_LOGS=true
            shift
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            exit 1
            ;;
    esac
done

echo -e "${BLUE}Configuration:${NC}"
echo "  Cluster: ${CLUSTER_NAME}"
echo "  Region: ${REGION}"
echo "  AWS Account: ${AWS_ACCOUNT_ID}"
echo "  IAM Role: ${IAM_ROLE_ARN}"
echo "  Disable Audit Logs: ${DISABLE_AUDIT_LOGS}"
echo ""

echo -e "${YELLOW}⚠️  WARNING: This will remove Game Agent's access to the cluster${NC}"
read -p "Are you sure you want to continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${RED}❌ Aborted${NC}"
    exit 1
fi
echo ""

# Check prerequisites
echo -e "${BLUE}📋 Step 1: Checking prerequisites...${NC}"

if ! command -v kubectl &> /dev/null; then
    echo -e "${RED}❌ kubectl not found. Please install kubectl.${NC}"
    exit 1
fi

if ! command -v aws &> /dev/null; then
    echo -e "${RED}❌ AWS CLI not found. Please install AWS CLI.${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Prerequisites met${NC}"
echo ""

# Update kubeconfig
echo -e "${BLUE}📋 Step 2: Updating kubeconfig...${NC}"
if ! aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${REGION}" &> /dev/null; then
    echo -e "${RED}❌ Failed to update kubeconfig. Check cluster name and region.${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Kubeconfig updated${NC}"
echo ""

# Verify connection
echo -e "${BLUE}📋 Step 3: Verifying cluster connection...${NC}"
if ! kubectl cluster-info &> /dev/null; then
    echo -e "${RED}❌ Cannot connect to cluster. Check your AWS credentials.${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Connected to cluster${NC}"
echo ""

# Backup aws-auth ConfigMap
echo -e "${BLUE}📋 Step 4: Backing up aws-auth ConfigMap...${NC}"
BACKUP_FILE="${SCRIPT_DIR}/aws-auth-backup-${CLUSTER_NAME}-deregister-$(date +%Y%m%d-%H%M%S).yaml"
if kubectl get configmap aws-auth -n kube-system -o yaml > "${BACKUP_FILE}" 2>/dev/null; then
    echo -e "${GREEN}✅ Backup created: ${BACKUP_FILE}${NC}"
else
    echo -e "${YELLOW}⚠️  aws-auth ConfigMap not found${NC}"
fi
echo ""

# Remove RBAC
echo -e "${BLUE}📋 Step 5: Removing RBAC configuration...${NC}"
if kubectl delete -f "${SCRIPT_DIR}/game-agent-rbac.yaml" &> /dev/null; then
    echo -e "${GREEN}✅ RBAC removed${NC}"
else
    echo -e "${YELLOW}⚠️  RBAC may not exist${NC}"
fi
echo ""

# Remove aws-auth entry
echo -e "${BLUE}📋 Step 6: Removing aws-auth ConfigMap entry...${NC}"

# Check if eksctl is available
if command -v eksctl &> /dev/null; then
    echo "Using eksctl for safe ConfigMap update..."

    if eksctl delete iamidentitymapping \
        --cluster "${CLUSTER_NAME}" \
        --region "${REGION}" \
        --arn "${IAM_ROLE_ARN}" 2>&1; then
        echo -e "${GREEN}✅ IAM identity mapping removed${NC}"
    else
        echo -e "${YELLOW}⚠️  IAM identity mapping may not exist or already removed${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  eksctl not found - using manual method${NC}"
    echo ""
    echo "Run the following command:"
    echo -e "${GREEN}kubectl edit configmap aws-auth -n kube-system${NC}"
    echo ""
    echo "Remove this entry from the mapRoles section:"
    echo "---"
    echo "- groups:"
    echo "  - game-agent-monitoring-group"
    echo "  rolearn: ${IAM_ROLE_ARN}"
    echo "  username: game-agent-agentcore-user"
    echo "---"
    echo ""
    read -p "Press Enter when you've removed the entry, or Ctrl+C to abort..."
fi
echo ""

# Verify removal
echo -e "${BLUE}📋 Step 7: Verifying removal...${NC}"
if kubectl get clusterrole game-agent-monitoring-role &> /dev/null; then
    echo -e "${YELLOW}⚠️  ClusterRole still exists${NC}"
else
    echo -e "${GREEN}✅ ClusterRole removed${NC}"
fi

if kubectl get clusterrolebinding game-agent-monitoring-binding &> /dev/null; then
    echo -e "${YELLOW}⚠️  ClusterRoleBinding still exists${NC}"
else
    echo -e "${GREEN}✅ ClusterRoleBinding removed${NC}"
fi

if kubectl get configmap aws-auth -n kube-system -o yaml 2>/dev/null | grep -q "${IAM_ROLE_NAME}"; then
    echo -e "${YELLOW}⚠️  aws-auth entry still exists${NC}"
else
    echo -e "${GREEN}✅ aws-auth entry removed${NC}"
fi
echo ""

# Disable audit logging (optional)
if [ "$DISABLE_AUDIT_LOGS" = true ]; then
    echo -e "${BLUE}📋 Step 8: Disabling audit logging...${NC}"

    UPDATE_OUTPUT=$(aws eks update-cluster-config \
        --name "${CLUSTER_NAME}" \
        --region "${REGION}" \
        --logging '{"clusterLogging":[{"types":["api","audit","authenticator"],"enabled":false}]}' \
        2>&1)

    if echo "$UPDATE_OUTPUT" | grep -q "update"; then
        UPDATE_ID=$(echo "$UPDATE_OUTPUT" | grep -o '"id": "[^"]*"' | head -1 | cut -d'"' -f4)
        echo -e "${GREEN}✅ Audit logging disabled (Update ID: ${UPDATE_ID})${NC}"
        echo -e "${YELLOW}⏱️  Update in progress (takes 5-10 minutes)${NC}"
    else
        echo -e "${YELLOW}⚠️  Audit logging may already be disabled${NC}"
    fi
    echo ""
fi

# Summary
echo -e "${GREEN}✅ Deregistration complete!${NC}"
echo ""
echo -e "${BLUE}📊 Summary:${NC}"
echo "  Cluster: ${CLUSTER_NAME}"
echo "  Region: ${REGION}"
echo "  RBAC: Removed"
echo "  aws-auth: Entry removed (verify manually)"
if [ "$DISABLE_AUDIT_LOGS" = true ]; then
    echo "  Audit Logs: Disabled"
fi
echo ""
echo -e "${BLUE}📋 Backup:${NC}"
echo "  ${BACKUP_FILE}"
echo ""
echo -e "${BLUE}🔄 To re-enroll:${NC}"
echo "  ${SCRIPT_DIR}/enroll-cluster.sh ${CLUSTER_NAME} ${REGION}"
