# Agones → Amazon GameLift Servers Migration Demo

Reference environment and runbook for the Sentinel-agent migration demo. The
agent (read-only) **reasons through the migration and generates the GameLift
IaC**; this directory provides the **"from" side** (a real Agones fleet on EKS)
and the supporting assets to record a compelling before/after.

## Layout

```
eks/cluster.yaml          eksctl config for a small Agones-ready EKS cluster
agones/fleet.yaml         Agones Fleet running the simple-game-server (UDP echo)
agones/allocation.yaml    GameServerAllocation to "start a match"
client/udp_load.py        UDP load generator — simulates N connected players
gamelift/README.md        The GameLift container-fleet target (agent-generated in-demo)
```

## Prerequisites

- `eksctl`, `kubectl`, `helm`, `aws` on PATH
- Sandbox creds: `export AWS_PROFILE=ubi-eba-sandbox AWS_REGION=us-east-1`
  (refresh with `ada credentials update --account=742301976366 --provider=isengard --role=Admin-OneClick --once`)

## Runbook

### 1. Create the cluster (~15–20 min)
```bash
eksctl create cluster -f eks/cluster.yaml
```

### 2. Open the Agones game-server UDP port range on the node security group
Agones assigns dynamic host ports in **7000–8000/UDP**; players must reach them.
```bash
CLUSTER=agones-demo
NODE_SG=$(aws ec2 describe-security-groups \
  --filters "Name=tag:aws:eks:cluster-name,Values=$CLUSTER" "Name=group-name,Values=*node*" \
  --query 'SecurityGroups[0].GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id "$NODE_SG" \
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

### 7. Record the migration
Drive the agent with the Scenario 1 prompt (see the demo prompts artifact),
download its generated GameLift CloudFormation, deploy it, then re-point the
client at the GameLift fleet endpoint for the cutover.

## Teardown (do this after recording — the cluster costs money)
```bash
kubectl delete -f agones/fleet.yaml --ignore-not-found
eksctl delete cluster -f eks/cluster.yaml --disable-nodegroup-eviction
```
