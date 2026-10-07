# Agones → Amazon GameLift Servers Migration Demo

Reference environment and runbook for the Sentinel-agent migration demo. The
agent (read-only) **reasons through the migration and generates the GameLift
IaC**; this directory provides the **"from" side** (a real Agones fleet on EKS)
and the supporting assets to record a compelling before/after.

## Layout

```
eks/cluster.yaml                eksctl config for a small Agones-ready EKS cluster
agones/fleet.yaml               Agones Fleet running the simple-game-server (UDP echo)
agones/allocation.yaml          GameServerAllocation to "start a match"
agones/agent-readonly-rbac.yaml Read-only K8s access for the agent (no writes, no Secrets)
client/udp_load.py              UDP load generator (workstation)
client/load-job.yaml            UDP load generator (in-cluster, recommended)
gamelift/                       GameLift side: game server image, reference template, deploy + cutover
```

## Prerequisites

- `eksctl`, `kubectl`, `helm`, `aws` on PATH
- Credentials for a non-production AWS account: `export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1`

## Runbook

### 1. Create the cluster (~15–20 min)
```bash
eksctl create cluster -f eks/cluster.yaml
```

### 2. Open the Agones game-server UDP port range
Agones assigns dynamic host ports in **7000–8000/UDP**; players must reach them.
eksctl managed nodes use the EKS cluster security group:
```bash
CLUSTER_SG=$(aws eks describe-cluster --name agones-demo \
  --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id "$CLUSTER_SG" \
  --ip-permissions 'IpProtocol=udp,FromPort=7000,ToPort=8000,IpRanges=[{CidrIp=0.0.0.0/0,Description=agones-gameservers}]'
```

### 3. Install Agones
```bash
helm repo add agones https://agones.dev/chart/stable
helm repo update
helm install agones agones/agones --namespace agones-system --create-namespace \
  --set agones.allocator.service.serviceType=ClusterIP --wait
```

### 4. Deploy the game-server fleet
```bash
kubectl apply -f agones/fleet.yaml
kubectl get gameservers    # wait for Ready
```

### 5. "Start a match" (allocate) a game server
`GameServerAllocation` is a request/response object (not persisted), so capture
the allocated server name from the create response:
```bash
GS=$(kubectl create -f agones/allocation.yaml -o jsonpath='{.status.gameServerName}')
kubectl get gameserver "$GS"   # STATE should be Allocated
```

### 6. Connect players (show live load)

**Recommended — in-cluster load generator** (reliable; does not depend on the
local network allowing outbound UDP):
```bash
POD_IP=$(kubectl get pod "$GS" -o jsonpath='{.status.podIP}')
kubectl delete job udp-load --ignore-not-found
sed "s/REPLACE_WITH_GAMESERVER_POD_IP/$POD_IP/" client/load-job.yaml | kubectl apply -f -
kubectl logs -f job/udp-load          # shows "active players (receiving echoes): N/N"
```

**Optional — from your workstation** (only works if your network permits
outbound UDP to the game port; many corporate/dev networks block it):
```bash
NODE=$(kubectl get gameserver "$GS" -o jsonpath='{.status.address}')
PORT=$(kubectl get gameserver "$GS" -o jsonpath='{.status.ports[0].port}')
python3 client/udp_load.py "$NODE" "$PORT" 10 120
```

### 7. Let the agent see the cluster (read-only)
The agent's runtime role needs Kubernetes read access to inspect the Agones
fleet it is migrating. This grants `get/list/watch` only — no write verbs and
no Secrets:
```bash
kubectl apply -f agones/agent-readonly-rbac.yaml
aws eks create-access-entry --cluster-name agones-demo --type STANDARD \
  --principal-arn "arn:aws:iam::<account-id>:role/game-agent-agentcore-execution-role" \
  --kubernetes-groups game-agent-readonly
kubectl auth can-i list fleets.agones.dev --as=probe --as-group=game-agent-readonly   # yes
kubectl auth can-i delete pods --as=probe --as-group=game-agent-readonly              # no
```

### 8. Record the migration
Drive the agent with the Scenario 1 prompt (see [`../RECORDING.md`](../RECORDING.md)). Its
answer ends with a **Generated infrastructure as code** section containing the
CloudFormation template exactly as the GameLift specialist produced it; use the
download button on the code block. Then follow [`gamelift/README.md`](gamelift/README.md)
to validate, deploy, and cut players over to GameLift.

## Teardown (do this after recording — the cluster and fleets cost money)
```bash
aws cloudformation delete-stack --stack-name gl-demo-migration
aws cloudformation delete-stack --stack-name gl-demo-reference
aws cloudformation delete-stack --stack-name gl-demo-unreal      # Scenario 2
kubectl delete -f agones/fleet.yaml --ignore-not-found
eksctl delete cluster -f eks/cluster.yaml --disable-nodegroup-eviction
aws ecr delete-repository --repository-name game-agent-demo/gamelift-echo-server --force
```
