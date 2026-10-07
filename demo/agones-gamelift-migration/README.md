# Agones to Amazon GameLift Servers: reference environment

A disposable, private-by-default environment for evaluating the assistant's
read-only migration advice. It runs a real Agones fleet on Amazon EKS (the
"from" side) and lets you deploy the GameLift container fleet the assistant
proposes (the "to" side), then compare the two under simulated player load.

**Boundaries**
- You run every step yourself, with a purpose-built operator role, in a
  non-production account. Nothing here is part of `deploy-all.sh`,
  `scripts/deploy.sh`, or the product's stacks.
- The assistant stays read-only. It gets `get`/`list`/`watch` on core and Agones
  resources through the canonical enrollment script, and never these operator
  permissions.
- Templates the assistant generates are untrusted drafts. Validate and review
  them before you deploy, and treat a successful deploy as evidence for this
  environment only.

## Layout

| Path | Purpose |
|---|---|
| `iam/operator-role.yaml` | Least-privilege operator role: region-bound, scoped to this cluster, `gl-demo-*` stacks, and one ECR repository |
| `eks/cluster.yaml` | eksctl config: EKS 1.35, private nodes, single NAT gateway, API limited to your `/32` |
| `agones/helm-values.yaml` | Keeps every Agones service cluster-internal (no load balancers) |
| `agones/fleet.yaml` | `simple-game-server` fleet (UDP 7654), the workload migrated from |
| `agones/allocation.yaml` | Allocates a game server ("start a match") |
| `agones/agent-readonly-rbac.yaml` | Read-only Agones access for the assistant's enrolled group |
| `client/load-job.yaml` | In-cluster UDP player simulator |
| `scripts/demo.sh` | Operator commands: `preflight`, `cluster-up`, `agones-up`, `enroll-agent`, `restrict-fleet-ingress`, `load`, `down`, `verify-clean` |
| `gamelift/` | GameLift-ready game server image, reference template, generated template, deploy steps |

## Versions

| Component | Version | Notes |
|---|---|---|
| Amazon EKS | 1.35 | Standard support; inside Agones 1.61's supported range (1.34–1.36) |
| Agones Helm chart | 1.61.0 | Pinned in `scripts/demo.sh` |
| `simple-game-server` image | 0.43 | Released with Agones 1.61 |
| eksctl | 0.231.0 or later | Checked by `demo.sh preflight` |
| GameLift Go Server SDK | 5.2.0 | Download verified by SHA-256 in `gamelift/server/build.sh` |
| Load generator image | `python:3.12.15-alpine3.24` | |
| Game server base image | `amazonlinux:2023.12.20260928.0` | |

Also required: AWS CLI v2, `kubectl` within one minor version of 1.35, Helm 3.x
or 4.x, `jq`, and, to build the image, Go 1.25 and Docker.

## Cost and time limits

| Resource | On-demand rate (us-east-1) | Running |
|---|---|---|
| EKS control plane (standard support) | USD 0.10/hour | Whole demo |
| 2 × m5.large nodes | USD 0.192/hour | Whole demo |
| NAT gateway, plus USD 0.045/GB processed | USD 0.045/hour | Whole demo |
| Public IPv4 address (NAT) | USD 0.005/hour | Whole demo |
| GameLift container fleet, 1 × c6i.large (per stack) | USD 0.109/hour | From stack create to delete |

The EKS environment costs about **USD 0.34/hour**, plus about **USD 0.11/hour
per GameLift stack**. A typical four-hour session with two stacks costs about
USD 2.30, before data transfer and ECR storage. Rates are from the AWS Price
List API at the time of writing; check current pricing before you start.

Every taggable resource gets `demo=agones-gamelift-migration`, `demo-env=<cluster name>`, `owner=<you>`,
and `expires-at=<now + MAX_HOURS>` (default 4 hours, at most 8). The tag records
your intent; nothing deletes resources automatically, so run `down` when you
finish. An EKS version that falls out of standard support bills extended
support at USD 0.60/hour, which is one more reason to delete the cluster
promptly.

## Runbook

### 0. Operator role (once per account)

```bash
aws cloudformation deploy --stack-name agones-demo-operator-role \
  --template-file iam/operator-role.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides OperatorPrincipalArn=arn:aws:iam::123456789012:role/ExampleOperator
```

Assume the `agones-demo-operator` role for every following step, for example
through a named profile with `role_arn` and `source_profile`. The role's
sessions last at most one hour; re-assume it as needed.

### 1. Cluster (about 20 minutes)

```bash
export AWS_REGION=us-east-1 OWNER=example-owner
export OPERATOR_CIDR="$(curl -s https://checkip.amazonaws.com)/32"
scripts/demo.sh preflight
scripts/demo.sh cluster-up
```

`cluster-up` rejects a missing or wide `OPERATOR_CIDR`. The Kubernetes API is
reachable publicly only from that address, and privately from inside the VPC.
Nodes have no public addresses.

### 2. Agones and the fleet

```bash
scripts/demo.sh agones-up     # fails if any LoadBalancer service exists
GS=$(kubectl create -f agones/allocation.yaml -o jsonpath='{.status.gameServerName}')
POD_IP=$(kubectl get pod "$GS" -o jsonpath='{.status.podIP}')
scripts/demo.sh load "$POD_IP" 7654 8 30      # "active players (receiving echoes): 8/8"
```

Agones assigns host ports in 7000–8000, but no security group opens them.
Players are simulated inside the cluster.

### 3. Let the assistant inspect the cluster (read-only)

```bash
scripts/demo.sh enroll-agent
kubectl auth can-i list fleets.agones.dev --as=probe --as-group=game-agent-monitoring-group   # yes
kubectl auth can-i delete pods --as=probe --as-group=game-agent-monitoring-group              # no
kubectl auth can-i get secrets --as=probe --as-group=game-agent-monitoring-group              # no
```

`enroll-agent` runs the canonical `infrastructure/kubernetes/enroll-cluster.sh`
and adds `agent-readonly-rbac.yaml` for the Agones resources.

**Network reachability.** The assistant's runtime reaches the Kubernetes API
over its public endpoint, which this cluster restricts to `OPERATOR_CIDR`.
Unless the runtime's egress is inside an allowed range, the EKS specialist
reports the cluster as unreachable, and the assistant then works from the
details in your prompt. Giving the runtime a private network path is a separate
change to the canonical deployment and is not part of this environment. Do not
widen `publicAccessCIDRs` to work around it.

### 4. Ask the assistant and deploy its template

See [`../RECORDING.md`](../RECORDING.md) for the prompt and
[`gamelift/README.md`](gamelift/README.md) to validate and deploy the template.
Name stacks `gl-demo-<name>` and tag them, for example:

```bash
aws cloudformation deploy --stack-name gl-demo-migration --template-file <template> \
  --capabilities CAPABILITY_NAMED_IAM --parameter-overrides <ImageParameter>=<image-uri> \
  --tags demo=agones-gamelift-migration demo-env=agones-demo owner="$OWNER"
```

A container fleet created without explicit inbound permissions opens its
connection ports to every address. Restrict it to the cluster's NAT address
before you test:

```bash
scripts/demo.sh restrict-fleet-ingress gl-demo-migration
FLEET=$(aws cloudformation describe-stacks --stack-name gl-demo-migration \
  --query "Stacks[0].Outputs[?OutputKey=='FleetId'].OutputValue" --output text)
read -r IP PORT < <(aws gamelift create-game-session --fleet-id "$FLEET" \
  --maximum-player-session-count 10 --query 'GameSession.[IpAddress,Port]' --output text)
scripts/demo.sh load "$IP" "$PORT" 8 30
```

### 5. Teardown and verification

```bash
scripts/demo.sh down          # GameLift stacks → Agones → cluster → ECR repository, then verify-clean
scripts/demo.sh verify-clean  # exits non-zero if any demo resource remains
aws cloudformation delete-stack --stack-name agones-demo-operator-role   # with your own credentials
```

`down` deletes only resources this environment created: stacks named
`gl-demo-*`, the named cluster and its `eksctl-<cluster>-*` stacks, and the
named ECR repository. It never changes deletion, termination, or retention
settings. `verify-clean` checks the demo tag across tagged resources, both
stack name patterns, the cluster, and the repository. Recently deleted
resources can stay visible to the tagging API for a short time, so re-run
`verify-clean` after a few minutes if it reports a resource you just deleted.

## Tests

`scripts/test/test_agones_gamelift_demo.py` checks image pins, ingress, isolation
from the canonical deployment, and `demo.sh` behavior against fake CLIs.
`backend/tests/unit/test_agones_demo_manifests_unit.py` checks the RBAC, cluster,
Helm values, and operator-role structure. Neither creates cloud resources.
