# Agones to Amazon GameLift Servers: reference environment

A disposable, private-by-default environment for evaluating the assistant's
read-only migration advice. It runs a real Agones fleet on Amazon EKS (the
"from" side) and lets you deploy the GameLift container fleet the assistant
proposes (the "to" side), then compare the two under simulated player load.

**Boundaries**
- Use a dedicated sandbox account with no other workloads. That account, not
  the policy, is what contains EC2: the operator role's EC2 permissions are the
  eksctl minimum and apply to every EC2 resource in the Region. Its
  CloudFormation, EKS, GameLift, ECR, and IAM role permissions are scoped by
  name or tag.
- The workload permissions boundary is the effective ceiling for every role the
  environment creates. The operator can attach its own `eksctl-<cluster>-*`
  policy to a bounded role and run code with it on an instance it launches, so
  plan for that role being able to do anything the boundary allows: EC2, load
  balancing, and Auto Scaling in this Region, reads of this cluster, image
  pulls, the node management agent, and GameLift fleet logging.
- You run every step yourself, with that operator role. Nothing here is part
  of `deploy-all.sh`, `scripts/deploy.sh`, or the product's stacks.
- The assistant stays read-only. It gets `get`/`list`/`watch` on core and Agones
  resources through the canonical enrollment script, and never these operator
  permissions.
- Templates the assistant generates are untrusted drafts. Validate and review
  them before you deploy, and treat a successful deploy as evidence for this
  environment only.

## Layout

| Path | Purpose |
|---|---|
| `iam/operator-role.yaml` | Operator role and the permissions boundary for every role it creates. Both are bound to one Region. CloudFormation, EKS, GameLift, ECR, and IAM role access is scoped by name or tag; EC2 access covers the Region, which is why the account must be dedicated |
| `eks/cluster.yaml` | eksctl config: EKS 1.35, private nodes, single NAT gateway, API limited to your `/32` |
| `agones/helm-values.yaml` | Keeps every Agones service cluster-internal (no load balancers) |
| `agones/fleet.yaml` | `simple-game-server` fleet (UDP 7654), the workload migrated from |
| `agones/allocation.yaml` | Allocates a game server ("start a match") |
| `agones/agent-readonly-rbac.yaml` | Read-only Agones access for the assistant's enrolled group |
| `client/load-job.yaml` | In-cluster UDP player simulator |
| `scripts/demo.sh` | Operator commands: `preflight`, `cluster-up`, `agones-up`, `enroll-agent`, `kubeconfig`, `nat-cidr`, `boundary-arn`, `restrict-fleet-ingress`, `load`, `down`, `verify-clean` |
| `gamelift/` | GameLift-ready game server image, reference template, review checklist, deploy steps |

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
| 2 × 30 GiB gp3 node volumes | about USD 0.007/hour | Whole demo |
| GameLift container fleet, 1 × c6i.large (per stack) | USD 0.109/hour | From stack create to delete |

The EKS environment costs about **USD 0.35/hour**, plus about **USD 0.11/hour
per GameLift stack**. The reference fleet can scale to its `MaxSize` of 3
instances, so a stack can reach about USD 0.33/hour. A typical four-hour
session with two stacks costs about USD 2.30, before data transfer and ECR
storage. Rates are from the AWS Price List API at the time of writing; check
current pricing before you start, and consider an AWS Budgets alert on the
account (for example, USD 10 per month).

Tags record intent; nothing deletes resources automatically, so run `down`
when you finish. The commands below tag resources as follows:

| Resources | Tags |
|---|---|
| eksctl stacks, cluster, node group, and their taggable EC2 resources | `demo`, `demo-env`, `owner`, `expires-at` |
| GameLift stacks and the resources in them | `demo`, `demo-env`, `owner`, `expires-at` (stack tags); the reference template also sets `demo` and `demo-env` on each resource |
| ECR repository | `demo`, `demo-env`, `owner`, `expires-at` |
| Operator-role stack and role | `demo`, `owner` only: the role outlives `down`, which must not find it |

`demo=agones-gamelift-migration`, `demo-env` is the cluster name, `owner` is
`OWNER`, and `expires-at` is now plus `MAX_HOURS` (default 4 hours, at most 8).
An EKS version that falls out of standard support bills extended support at
USD 0.60/hour, which is one more reason to delete the cluster promptly.

## Runbook

Run the commands from this directory. Set these once per shell:

```bash
export AWS_REGION=us-east-1 CLUSTER_NAME=agones-demo OWNER=example-owner
EXPIRES_AT="$(date -u -v+4H +%Y-%m-%dT%H-%M-%SZ 2>/dev/null || date -u -d '+4 hours' +%Y-%m-%dT%H-%M-%SZ)"
```

### 0. Operator role (once per environment)

With your own credentials:

```bash
aws cloudformation deploy --stack-name "$CLUSTER_NAME-operator-role" \
  --template-file iam/operator-role.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides OperatorPrincipalArn=arn:aws:iam::123456789012:role/ExampleOperator \
    ClusterName="$CLUSTER_NAME" \
  --tags demo=agones-gamelift-migration owner="$OWNER"
```

The stack creates the `<cluster>-operator` role and the
`<cluster>-workload-boundary` policy. Every role the environment creates
(the EKS cluster and node roles, and each GameLift fleet role) must carry that
boundary; the operator role cannot create a role without it, attach any policy
outside a short allowlist, or change the boundary, the assistant's role, or
itself. The boundary, not the allowlist, is the ceiling: see **Boundaries**
above.

To run a second environment in the same account, give it its own
`ClusterName`, `StackPrefix`, and `RepositoryName`, none of which is a prefix
of the other environment's values (for example `agones-a` and `agones-b`, not
`agones` and `agones-b`). IAM scopes, stack selection, and `verify-clean` match
by prefix.

Assume the operator role for every following step, for example through a named
profile with `role_arn` and `source_profile`. Its sessions last at most one
hour; re-assume it as needed.

### 1. Cluster (about 20 minutes)

```bash
export OPERATOR_CIDR="$(curl -s https://checkip.amazonaws.com)/32"
scripts/demo.sh preflight
scripts/demo.sh cluster-up
export KUBECONFIG="$(scripts/demo.sh kubeconfig)"
```

`cluster-up` accepts only a single IPv4 address as `OPERATOR_CIDR`. The
Kubernetes API is reachable publicly only from that address, and privately from
inside the VPC. Nodes have no public addresses. Every `demo.sh` command that
touches Kubernetes uses its own kubeconfig for this cluster and checks the
server endpoint first, whatever your current context is. Export `KUBECONFIG` as
above before running `kubectl` yourself.

### 2. Agones and the fleet

```bash
scripts/demo.sh agones-up     # fails if any LoadBalancer service exists
GS=$(kubectl create -f agones/allocation.yaml -o jsonpath='{.status.gameServerName}')
POD_IP=$(kubectl get pod "$GS" -o jsonpath='{.status.podIP}')
scripts/demo.sh load "$POD_IP" 7654 8 30      # "active players (receiving echoes): 8/8"
```

Agones assigns host ports in 7000–8000, but no security group opens them.
Players are simulated inside the cluster.

### 3. Enroll the assistant (read-only); it cannot reach this cluster

```bash
scripts/demo.sh enroll-agent
kubectl auth can-i list fleets.agones.dev --as=probe --as-group=game-agent-monitoring-group   # yes
kubectl auth can-i delete pods --as=probe --as-group=game-agent-monitoring-group              # no
kubectl auth can-i get secrets --as=probe --as-group=game-agent-monitoring-group              # no
```

`enroll-agent` runs the canonical `infrastructure/kubernetes/enroll-cluster.sh`,
reads the Kubernetes group it gave the assistant's role (shown above for the
default role), and binds `agent-readonly-rbac.yaml` to that group for the
Agones resources.

In the default runtime network mode, the assistant's Kubernetes and Agones
reads cannot reach this cluster: its API accepts public traffic only from
`OPERATOR_CIDR`. The EKS specialist reports the cluster as unreachable, and the
assistant works from the details in your prompt. A private network path for the
runtime is a separate change to the canonical deployment, with its own security
review. Do not widen `publicAccessCIDRs` to work around it.

### 4. Ask the assistant and deploy its template

Build and push the game server image first ([`gamelift/README.md`](gamelift/README.md),
step 1). Then ask the assistant, for example:

> My Agones fleet `simple-game-server` runs on the EKS cluster `agones-demo` in
> us-east-1: 3 replicas, one UDP port (container port 7654, Dynamic port
> policy), 20m CPU and 64 MiB per game server. Plan its migration to an Amazon
> GameLift Servers container fleet and write the CloudFormation template. The
> GameLift-ready image is `<image-uri>`.

Review the template against the checklist in
[`gamelift/README.md`](gamelift/README.md) before you deploy it, and deploy it as
described there. Stacks must be named `gl-demo-<name>` and carry the tags shown;
the operator role refuses GameLift resources without `demo-env`. Then send the
same simulated players to GameLift:

```bash
scripts/demo.sh restrict-fleet-ingress gl-demo-migration   # verifies only the NAT /32 is allowed
FLEET=$(aws cloudformation describe-stacks --stack-name gl-demo-migration \
  --query "Stacks[0].Outputs[?OutputKey=='FleetId'].OutputValue" --output text)
read -r IP PORT < <(aws gamelift create-game-session --fleet-id "$FLEET" \
  --maximum-player-session-count 10 --query 'GameSession.[IpAddress,Port]' --output text)
scripts/demo.sh load "$IP" "$PORT" 8 30
```

### 5. Teardown and verification

```bash
scripts/demo.sh down          # lists what it will delete and asks for the cluster name
scripts/demo.sh verify-clean  # exits non-zero if any resource of this environment remains
aws cloudformation delete-stack --stack-name "$CLUSTER_NAME-operator-role"   # with your own credentials
```

`down` resolves its targets first and deletes only resources of this
environment: `gl-demo-*` stacks tagged `demo=agones-gamelift-migration` and
`demo-env=<cluster>`, the cluster (after checking the same tags) with its
`eksctl-<cluster>-*` stacks, and the ECR repository (after checking its tags).
It waits for in-progress stacks to settle, because a container fleet cannot be
deleted until it is active; a stack in `REVIEW_IN_PROGRESS` is deleted at once.
If a failed `cluster-up` left `eksctl-<cluster>-*` stacks without a cluster,
`down` lists them and removes them with `eksctl delete cluster`. For every
fleet it deletes, it also removes the default `gamelift-<fleet ID>-<Region>`
log group that GameLift creates when a template sets no `LogConfiguration`; the
reference template logs to its own `gamelift-<stack>` group, which goes
with the stack. A fleet deleted outside `down` leaves its default log group
behind. For non-interactive use, set `DEMO_CONFIRM=<cluster>`. It never changes
deletion, termination, or retention settings.

`verify-clean` fails on any AWS error, including an expired session; only a
service's not-found answer counts as absent. It checks tagged resources, stacks
in every status except `DELETE_COMPLETE` for both name patterns,
`gamelift-<prefix>-*` log groups, the cluster, and the repository. Recently deleted resources can stay visible to the tagging
API for a short time, so it retries before reporting them.

## Tests

`scripts/test/test_agones_gamelift_demo.py` checks image pins, ingress, isolation
from the canonical deployment, and `demo.sh` behavior against fake CLIs.
`backend/tests/unit/test_agones_demo_manifests_unit.py` checks the RBAC, cluster,
Helm values, and operator-role structure. Neither creates cloud resources.
