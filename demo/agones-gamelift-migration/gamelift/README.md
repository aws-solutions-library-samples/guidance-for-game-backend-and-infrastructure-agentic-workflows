# GameLift side of the migration

In the demo, the **Sentinel agent generates** the CloudFormation for the Amazon
GameLift Servers **container fleet** in one interaction (Scenario 1), grounded
in the GameLift KB (container-fleet posts, CloudFormation reference pages, and
the Containers Starter Kit template). This directory holds what you need to
check and deploy that output.

| Path | Purpose |
|---|---|
| `server/` | GameLift-ready game server image (same UDP echo logic as the Agones `simple-game-server`) |
| `container-fleet.reference.yaml` | Hand-checked baseline template. Used to validate the agent's output and as a fallback target. Not the on-camera artifact. |
| `container-fleet.generated.yaml` | The template the agent produced (saved at record time) |

## 1. Build and push the game server image

The Agones `simple-game-server` uses the Agones SDK sidecar, so it can't run on
GameLift as is. Swapping the Agones SDK for the GameLift Server SDK is the code
change at the heart of any Agones migration. Here the swap is done without
touching game code: `server/` wraps an echo server with the Containers Starter
Kit's GameLift SDK wrapper (Go Server SDK 5.2.0), which calls `InitSDK`,
`ProcessReady`, activates game sessions, and calls `ProcessEnding`.

```bash
REPO=game-agent-demo/gamelift-echo-server
aws ecr create-repository --repository-name "$REPO" --image-scanning-configuration scanOnPush=true
URI=$(aws ecr describe-repositories --repository-names "$REPO" --query 'repositories[0].repositoryUri' --output text)
aws ecr get-login-password | docker login --username AWS --password-stdin "${URI%%/*}"
./server/build.sh "$URI" v1        # requires Go and Docker; builds linux/amd64
```

## 2. Validate the template

```bash
pip install "cfn-lint==1.*"
cfn-lint --regions us-east-1 -- container-fleet.generated.yaml
aws cloudformation validate-template --template-body file://container-fleet.generated.yaml
```

Things to check in the agent's output (all covered by the reference template):

- `AWS::GameLift::ContainerGroupDefinition` with `OperatingSystem: AMAZON_LINUX_2023`,
  `ServerSdkVersion` 5.2.0+, and the container's UDP port.
- `AWS::GameLift::ContainerFleet` (not the deprecated `AWS::GameLift::Fleet`
  with `ContainerGroupsConfiguration`) with a `FleetRoleArn` that has
  `GameLiftContainerFleetPolicy`.
- A `GameSessionQueue` pointing at the fleet ARN and a target-based scaling
  policy on `PercentAvailableGameSessions`.
- No literal IPs/CIDRs: the assistant's guardrail anonymizes IP addresses in
  model output, so templates rely on the fleet's default inbound rules.

## 3. Deploy

```bash
aws cloudformation deploy --stack-name gl-demo-migration \
  --template-file container-fleet.generated.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides GameServerImageUri="$URI:v1"
```

The container group definition first copies the image (`COPYING` → `READY`),
then the fleet provisions instances and reaches `ACTIVE` (typically 20–40 min).

## 4. Cutover: send players to GameLift

```bash
FLEET=$(aws cloudformation describe-stacks --stack-name gl-demo-migration \
  --query "Stacks[0].Outputs[?OutputKey=='FleetId'].OutputValue" --output text)
aws gamelift create-game-session --fleet-id "$FLEET" --maximum-player-session-count 10 \
  --query 'GameSession.[IpAddress,Port]' --output text
```

Point the in-cluster load generator (`../client/load-job.yaml`) at that
IP and port: the same players that were connected to Agones are now served by
GameLift. For a zero-downtime cutover, keep Agones serving existing sessions,
route new sessions to the GameLift queue, and drain Agones as its sessions end
(the GameLift Anywhere hybrid post in the KB describes the pattern).
