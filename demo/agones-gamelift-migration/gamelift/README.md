# GameLift side of the reference environment

This directory holds what you need to check and deploy an Amazon GameLift
Servers container fleet: either the assistant's generated template, or the
hand-checked reference template that serves as a baseline.

| Path | Purpose |
|---|---|
| `server/` | GameLift-ready game server image (same UDP echo behavior as the Agones `simple-game-server`) |
| `container-fleet.reference.yaml` | Hand-checked baseline template, for comparison and as a fallback |

## 1. Build and push the game server image

The Agones `simple-game-server` uses the Agones SDK sidecar, so it can't run on
GameLift as is. Swapping the Agones SDK for the GameLift Server SDK is the code
change at the heart of any Agones migration. Here the swap is done without
touching game code: `server/` wraps an echo server with the Containers Starter
Kit's GameLift SDK wrapper (Go Server SDK 5.2.0, checksum-verified). The wrapper
calls `InitSDK` and `ProcessReady`, activates game sessions, and calls
`ProcessEnding`.

Run these from this directory, with `CLUSTER_NAME`, `OWNER`, and `EXPIRES_AT`
set as in the main README:

```bash
REPO=agones-gamelift-demo/game-server            # must match the operator role's RepositoryName
aws ecr create-repository --repository-name "$REPO" --image-scanning-configuration scanOnPush=true \
  --tags Key=demo,Value=agones-gamelift-migration Key=demo-env,Value="$CLUSTER_NAME" \
    Key=owner,Value="$OWNER" Key=expires-at,Value="$EXPIRES_AT"
URI=$(aws ecr describe-repositories --repository-names "$REPO" --query 'repositories[0].repositoryUri' --output text)
aws ecr get-login-password | docker login --username AWS --password-stdin "${URI%%/*}"
./server/build.sh "$URI" v1                      # GAME_PORT=7777 ./server/build.sh "$URI" v1-port7777 for another port
```

## 2. Validate a generated template

A generated template is an untrusted draft, whatever checks the assistant ran
on it. Validate it, then read it before you deploy:

```bash
pip install "cfn-lint==1.*"
cfn-lint --regions us-east-1 -- <template>
aws cloudformation validate-template --template-body file://<template>
```

What to look for (the reference template shows each one):

- `AWS::GameLift::ContainerGroupDefinition` with `OperatingSystem: AMAZON_LINUX_2023`,
  `ServerSdkVersion` 5.2.0 or later, and the game server's UDP port.
- `AWS::GameLift::ContainerFleet`, not the deprecated `AWS::GameLift::Fleet`
  with `ContainerGroupsConfiguration`, whose `FleetRoleArn` has
  `GameLiftContainerFleetPolicy`.
- A `GameSessionQueue` that targets the fleet ARN, and a target-based scaling
  policy on `PercentAvailableGameSessions`.
- `InstanceConnectionPortRange` and `InstanceInboundPermissions` set
  explicitly, with matching ports and a narrow source (the cluster's NAT
  `/32`), never `0.0.0.0/0` or `::/0`. Without them the fleet gets a default
  rule that is open to every address from the moment its instances launch.
- Every `AWS::IAM::Role` has `PermissionsBoundary` set to the workload
  boundary (`../scripts/demo.sh boundary-arn`); the operator role cannot create
  a role without it. Add the boundary, and the source CIDR above, as
  parameters if the template lacks them.
- `demo` and `demo-env` tags on the stack (see step 3), so that the operator
  role allows the GameLift resources and `down` finds the stack.
- `LogConfiguration` pointing at a log group the stack creates, as the
  reference template does. Without it, GameLift writes to a default
  `gamelift-<fleet ID>-<Region>` group that outlives the stack; `down` deletes
  it for every fleet it deletes.
- Only the resources you expect, small capacity, and one location.

## 3. Deploy

The reference template, as a baseline:

```bash
aws cloudformation deploy --stack-name gl-demo-reference --template-file container-fleet.reference.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GameServerImageUri="$URI:v1" DemoEnv="$CLUSTER_NAME" \
    PlayerSourceCidr="$(../scripts/demo.sh nat-cidr)" BoundaryArn="$(../scripts/demo.sh boundary-arn)" \
  --tags demo=agones-gamelift-migration demo-env="$CLUSTER_NAME" owner="$OWNER" expires-at="$EXPIRES_AT"
```

A reviewed generated template, with the same tags and the parameters it
declares:

```bash
aws cloudformation deploy --stack-name gl-demo-migration --template-file <template> \
  --capabilities CAPABILITY_NAMED_IAM --parameter-overrides <ImageParameter>="$URI:v1" <other parameters> \
  --tags demo=agones-gamelift-migration demo-env="$CLUSTER_NAME" owner="$OWNER" expires-at="$EXPIRES_AT"
```

Stack names must start with `gl-demo-`; the operator role allows no other.
CloudFormation passes the stack tags on every create call, which is what the
operator role's `demo-env` condition checks.
The container group definition first copies the image (`COPYING` → `READY`).
The fleet then provisions instances and reaches `ACTIVE`, typically in 20–40
minutes.

## 4. Check ingress and send players to GameLift

`restrict-fleet-ingress` makes the fleet's inbound permissions exactly UDP on
the connection port range from the cluster's NAT gateway address, re-reads the
fleet, and fails if anything else remains. For the reference template it only
confirms what the template set. Then simulate players from inside the cluster:

```bash
../scripts/demo.sh restrict-fleet-ingress gl-demo-migration
FLEET=$(aws cloudformation describe-stacks --stack-name gl-demo-migration \
  --query "Stacks[0].Outputs[?OutputKey=='FleetId'].OutputValue" --output text)
read -r IP PORT < <(aws gamelift create-game-session --fleet-id "$FLEET" \
  --maximum-player-session-count 10 --query 'GameSession.[IpAddress,Port]' --output text)
../scripts/demo.sh load "$IP" "$PORT" 8 30
```

The same simulated players that were connected to Agones are now served by
GameLift. A production zero-downtime cutover keeps Agones serving existing
sessions, routes new sessions to the GameLift queue, and drains Agones as its
sessions end. The GameLift Anywhere hybrid-hosting post in the knowledge base
describes the pattern. This environment does not run a cutover.
