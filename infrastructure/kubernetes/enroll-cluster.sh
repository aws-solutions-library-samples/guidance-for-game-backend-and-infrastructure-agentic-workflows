#!/bin/bash
# Enroll an EKS cluster with Game Agent AgentCore Runtime
# This script configures Kubernetes API access for read-only monitoring

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
K8S_USERNAME="game-agent-agentcore-user"
K8S_GROUP="game-agent-monitoring-group"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🚀 Game Agent EKS Cluster Enrollment${NC}"
echo "================================================"
echo ""

# Check arguments
if [ $# -lt 2 ]; then
    echo -e "${RED}Usage: $0 <cluster-name> <region> [--role-name ROLE] [--enable-audit-logs] [--log-retention-days N]${NC}"
    echo ""
    echo "Examples:"
    echo "  $0 my-cluster us-west-2"
    echo "  $0 my-cluster us-west-2 --enable-audit-logs"
    echo "  $0 my-cluster us-west-2 --role-name my-custom-role --enable-audit-logs"
    exit 1
fi

CLUSTER_NAME=$1
REGION=$2
ENABLE_AUDIT_LOGS=false
LOG_RETENTION_DAYS=7

# Parse optional arguments
shift 2
while [[ $# -gt 0 ]]; do
    case $1 in
        --role-name)
            IAM_ROLE_NAME=$2
            IAM_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${IAM_ROLE_NAME}"
            shift 2
            ;;
        --enable-audit-logs)
            ENABLE_AUDIT_LOGS=true
            shift
            ;;
        --log-retention-days)
            LOG_RETENTION_DAYS=$2
            shift 2
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
echo "  Audit Logs: ${ENABLE_AUDIT_LOGS}"
if [ "$ENABLE_AUDIT_LOGS" = true ]; then
    echo "  Log Retention: ${LOG_RETENTION_DAYS} days"
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
BACKUP_FILE="${SCRIPT_DIR}/aws-auth-backup-${CLUSTER_NAME}-$(date +%Y%m%d-%H%M%S).yaml"
if kubectl get configmap aws-auth -n kube-system -o yaml > "${BACKUP_FILE}" 2>/dev/null; then
    echo -e "${GREEN}✅ Backup created: ${BACKUP_FILE}${NC}"
else
    echo -e "${YELLOW}⚠️  aws-auth ConfigMap not found (will be created)${NC}"
fi
echo ""

# Check if already enrolled
echo -e "${BLUE}📋 Step 5: Checking enrollment status...${NC}"
if kubectl get configmap aws-auth -n kube-system -o yaml 2>/dev/null | grep -q "${IAM_ROLE_NAME}"; then
    echo -e "${YELLOW}⚠️  Cluster already enrolled with Game Agent${NC}"
    read -p "Do you want to continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${RED}❌ Aborted${NC}"
        exit 1
    fi
fi
echo ""

# Apply RBAC
echo -e "${BLUE}📋 Step 6: Applying RBAC configuration...${NC}"
if kubectl apply -f "${SCRIPT_DIR}/game-agent-rbac.yaml" &> /dev/null; then
    echo -e "${GREEN}✅ RBAC configured${NC}"
else
    echo -e "${RED}❌ Failed to apply RBAC${NC}"
    exit 1
fi
echo ""

# Update aws-auth ConfigMap
echo -e "${BLUE}📋 Step 7: Updating aws-auth ConfigMap...${NC}"

# Check if eksctl is available
if command -v eksctl &> /dev/null; then
    echo "Using eksctl for safe ConfigMap update..."

    if eksctl create iamidentitymapping \
        --cluster "${CLUSTER_NAME}" \
        --region "${REGION}" \
        --arn "${IAM_ROLE_ARN}" \
        --username "${K8S_USERNAME}" \
        --group "${K8S_GROUP}" 2>&1; then
        echo -e "${GREEN}✅ IAM identity mapping created${NC}"
    else
        echo -e "${RED}❌ Failed to create IAM identity mapping${NC}"
        exit 1
    fi
else
    echo -e "${YELLOW}⚠️  eksctl not found - using manual method${NC}"
    echo ""
    echo "Run the following command:"
    echo -e "${GREEN}kubectl edit configmap aws-auth -n kube-system${NC}"
    echo ""
    echo "Add this entry to the mapRoles section:"
    echo "---"
    echo "- groups:"
    echo "  - ${K8S_GROUP}"
    echo "  rolearn: ${IAM_ROLE_ARN}"
    echo "  username: ${K8S_USERNAME}"
    echo "---"
    echo ""
    read -p "Press Enter when you've updated the ConfigMap, or Ctrl+C to abort..."
fi
echo ""

# Verify RBAC
echo -e "${BLUE}📋 Step 8: Verifying RBAC configuration...${NC}"
if kubectl get clusterrole game-agent-monitoring-role &> /dev/null; then
    echo -e "${GREEN}✅ ClusterRole created${NC}"
else
    echo -e "${RED}❌ ClusterRole not found${NC}"
    exit 1
fi

if kubectl get clusterrolebinding game-agent-monitoring-binding &> /dev/null; then
    echo -e "${GREEN}✅ ClusterRoleBinding created${NC}"
else
    echo -e "${RED}❌ ClusterRoleBinding not found${NC}"
    exit 1
fi
echo ""

# Test permissions
echo -e "${BLUE}📋 Step 9: Testing permissions...${NC}"
if kubectl auth can-i list pods --as="${K8S_USERNAME}" --as-group="${K8S_GROUP}" &> /dev/null; then
    echo -e "${GREEN}✅ Can list pods${NC}"
else
    echo -e "${RED}❌ Cannot list pods${NC}"
fi

if kubectl auth can-i delete pods --as="${K8S_USERNAME}" --as-group="${K8S_GROUP}" 2>&1 | grep -q "no"; then
    echo -e "${GREEN}✅ Cannot delete pods (correct)${NC}"
else
    echo -e "${YELLOW}⚠️  Unexpected delete permission${NC}"
fi
echo ""

# Enable audit logging (optional)
if [ "$ENABLE_AUDIT_LOGS" = true ]; then
    echo -e "${BLUE}📋 Step 10: Enabling audit logging...${NC}"

    UPDATE_OUTPUT=$(aws eks update-cluster-config \
        --name "${CLUSTER_NAME}" \
        --region "${REGION}" \
        --logging '{"clusterLogging":[{"types":["api","audit","authenticator"],"enabled":true}]}' \
        2>&1)

    if echo "$UPDATE_OUTPUT" | grep -q "update"; then
        UPDATE_ID=$(echo "$UPDATE_OUTPUT" | grep -o '"id": "[^"]*"' | head -1 | cut -d'"' -f4)
        echo -e "${GREEN}✅ Audit logging enabled (Update ID: ${UPDATE_ID})${NC}"
        echo -e "${YELLOW}⏱️  Update in progress (takes 5-10 minutes)${NC}"

        # Set log retention
        echo -e "${BLUE}📋 Step 11: Setting log retention to ${LOG_RETENTION_DAYS} days...${NC}"
        sleep 5  # Wait a bit for log group to be created

        if aws logs put-retention-policy \
            --log-group-name "/aws/eks/${CLUSTER_NAME}/cluster" \
            --retention-in-days "${LOG_RETENTION_DAYS}" \
            --region "${REGION}" &> /dev/null; then
            echo -e "${GREEN}✅ Log retention set to ${LOG_RETENTION_DAYS} days${NC}"
        else
            echo -e "${YELLOW}⚠️  Log retention will be set once log group is created${NC}"
            echo "   Run this command later:"
            echo "   aws logs put-retention-policy --log-group-name /aws/eks/${CLUSTER_NAME}/cluster --retention-in-days ${LOG_RETENTION_DAYS} --region ${REGION}"
        fi
    else
        echo -e "${YELLOW}⚠️  Audit logging may already be enabled${NC}"
    fi
    echo ""
fi

# Summary
echo -e "${GREEN}✅ Enrollment complete!${NC}"
echo ""
echo -e "${BLUE}📊 Summary:${NC}"
echo "  Cluster: ${CLUSTER_NAME}"
echo "  Region: ${REGION}"
echo "  RBAC: Read-only access configured"
echo "  IAM Role: ${IAM_ROLE_ARN}"
echo "  Kubernetes User: ${K8S_USERNAME}"
echo "  Kubernetes Group: ${K8S_GROUP}"
if [ "$ENABLE_AUDIT_LOGS" = true ]; then
    echo "  Audit Logs: Enabled (${LOG_RETENTION_DAYS} day retention)"
fi
echo ""
echo -e "${BLUE}🔍 Verification:${NC}"
echo "  kubectl auth can-i list pods --as=${K8S_USERNAME} --as-group=${K8S_GROUP}"
echo ""
echo -e "${BLUE}📋 Backup:${NC}"
echo "  ${BACKUP_FILE}"
echo ""
echo -e "${BLUE}🔄 To deregister:${NC}"
echo "  ${SCRIPT_DIR}/deregister-cluster.sh ${CLUSTER_NAME} ${REGION}"
