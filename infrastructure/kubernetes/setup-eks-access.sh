#!/bin/bash
# Setup Kubernetes API access for Game Agent AgentCore Runtime
# This script configures the eks-workshop cluster to allow AgentCore Runtime
# to access the Kubernetes API with read-only permissions.

set -e

CLUSTER_NAME="eks-workshop"
REGION="us-west-2"
IAM_ROLE_NAME="${IAM_ROLE_NAME:-game-agent-agentcore-execution-role}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🔧 Setting up Kubernetes API access for Game Agent AgentCore Runtime"
echo "   Cluster: ${CLUSTER_NAME}"
echo "   Region: ${REGION}"
echo ""

# Check if kubectl is installed
if ! command -v kubectl &> /dev/null; then
    echo "❌ kubectl not found. Please install kubectl first."
    exit 1
fi

# Resolve the role ARN. The role may live under an IAM path (for example
# /workloads/), but aws-auth mapRoles entries cannot include a path, so the
# mapping uses the path-free form arn:<partition>:iam::<account>:role/<name>.
if ! IAM_ROLE_ARN=$(aws iam get-role --role-name "$IAM_ROLE_NAME" --query 'Role.Arn' --output text); then
    echo "❌ Failed to resolve IAM role ${IAM_ROLE_NAME}."
    exit 1
fi
AUTH_MAP_ROLE_ARN="${IAM_ROLE_ARN%%:role/*}:role/${IAM_ROLE_NAME}"
echo "   IAM Role: ${IAM_ROLE_ARN}"
echo ""

# Update kubeconfig
echo "📋 Step 1: Updating kubeconfig..."
aws eks update-kubeconfig --name ${CLUSTER_NAME} --region ${REGION}

# Verify connection
echo "📋 Step 2: Verifying cluster connection..."
if ! kubectl cluster-info &> /dev/null; then
    echo "❌ Cannot connect to cluster. Check your AWS credentials and cluster access."
    exit 1
fi
echo "✅ Connected to cluster"

# Backup existing aws-auth ConfigMap
echo "📋 Step 3: Backing up existing aws-auth ConfigMap..."
kubectl get configmap aws-auth -n kube-system -o yaml > "${SCRIPT_DIR}/aws-auth-backup-$(date +%Y%m%d-%H%M%S).yaml"
echo "✅ Backup created"

# Check if our role is already in aws-auth
echo "📋 Step 4: Checking if role is already configured..."
if kubectl get configmap aws-auth -n kube-system -o yaml | grep -qF "$AUTH_MAP_ROLE_ARN"; then
    echo "⚠️  Role already exists in aws-auth ConfigMap"
    read -p "Do you want to continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "❌ Aborted"
        exit 1
    fi
fi

# Apply RBAC configuration
echo "📋 Step 5: Applying RBAC configuration..."
kubectl apply -f "${SCRIPT_DIR}/game-agent-rbac.yaml"
echo "✅ RBAC configured"

# Update aws-auth ConfigMap
echo "📋 Step 6: Updating aws-auth ConfigMap..."
echo ""
echo "⚠️  MANUAL STEP REQUIRED:"
echo "   Run: kubectl edit configmap aws-auth -n kube-system"
echo ""
echo "   Add this entry to the mapRoles section (aws-auth rolearn values cannot include an IAM path):"
echo "   ---"
echo "   - groups:"
echo "     - game-agent-monitoring-group"
echo "     rolearn: ${AUTH_MAP_ROLE_ARN}"
echo "     username: game-agent-agentcore-user"
echo "   ---"
echo ""
read -p "Press Enter when you've updated the ConfigMap, or Ctrl+C to abort..."

# Verify RBAC
echo "📋 Step 7: Verifying RBAC configuration..."
if kubectl get clusterrole game-agent-monitoring-role &> /dev/null; then
    echo "✅ ClusterRole created"
else
    echo "❌ ClusterRole not found"
    exit 1
fi

if kubectl get clusterrolebinding game-agent-monitoring-binding &> /dev/null; then
    echo "✅ ClusterRoleBinding created"
else
    echo "❌ ClusterRoleBinding not found"
    exit 1
fi

# Test access (this will fail until aws-auth is updated)
echo "📋 Step 8: Testing access..."
echo "   Note: This may show 'Unauthorized' until aws-auth ConfigMap is updated"
echo ""

echo "✅ Setup complete!"
echo ""
echo "📊 Summary:"
echo "   - RBAC configured: game-agent-monitoring-role (read-only)"
echo "   - ClusterRoleBinding: game-agent-monitoring-binding"
echo "   - Group: game-agent-monitoring-group"
echo "   - IAM Role: ${IAM_ROLE_ARN}"
echo ""
echo "🔍 To verify:"
echo "   kubectl auth can-i list pods --as=game-agent-agentcore-user"
echo "   kubectl auth can-i delete pods --as=game-agent-agentcore-user  # Should be 'no'"
echo ""
echo "🔄 To rollback:"
echo "   kubectl delete -f ${SCRIPT_DIR}/game-agent-rbac.yaml"
echo "   kubectl edit configmap aws-auth -n kube-system  # Remove the role entry"
