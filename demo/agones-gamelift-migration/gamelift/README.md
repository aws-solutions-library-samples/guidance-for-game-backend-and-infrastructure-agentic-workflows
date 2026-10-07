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

```bash
REPO=agones-gamelift-demo/game-server            # must match the operator role's RepositoryName
aws ecr create-repository --repository-name "$REPO" --image-scanning-configuration scanOnPush=true \
  --tags Key=demo,Value=agones-gamelift-migration Key=demo-env,Value=agones-demo Key=owner,Value="$OWNER"
URI=$(aws ecr describe-repositories --repository-names "$REPO" --query 'repositories[0].repositoryUri' --output text)
aws ecr get-login-password | docker login --username AWS --password-stdin "${URI%%/*}"
./server/build.sh "$URI" v1                      # GAME_PORT=7777 ./server/build.sh "$URI" v1-port7777 for another port
```

## 2. Validate a generated template

A generated template is an untrusted draft, even after the assistant's own
schema check. Validate it, then read it before you deploy:

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
- Only the resources you expect, small capacity, and one location.

## 3. Deploy

```bash
aws cloudformation deploy --stack-name gl-demo-migration --template-file <template> \
  --capabilities CAPABILITY_NAMED_IAM --parameter-overrides <ImageParameter>="$URI:v1" \
  --tags demo=agones-gamelift-migration demo-env=agones-demo owner="$OWNER"
```

Stack names must start with `gl-demo-`; the operator role allows no other.
The container group definition first copies the image (`COPYING` → `READY`).
The fleet then provisions instances and reaches `ACTIVE`, typically in 20–40
minutes.

## 4. Restrict ingress and send players to GameLift

A container fleet created without explicit inbound permissions opens its
connection port range to every address. Replace that rule with the cluster's
NAT gateway address, then simulate players from inside the cluster:

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
