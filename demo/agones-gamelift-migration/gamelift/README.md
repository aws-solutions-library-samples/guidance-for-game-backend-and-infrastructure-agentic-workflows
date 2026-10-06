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
| `container-fleet.generated.yaml` | A template the agent produced from the Scenario 1 prompt, deployed unedited (see [Validated run](#validated-run)) |

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

The image parameter name is whatever the agent chose (`ContainerImageUri` in
the saved template); every other parameter has a working default. The fleet
role has a fixed name, so pass `CAPABILITY_NAMED_IAM`.

```bash
aws cloudformation deploy --stack-name gl-demo-migration \
  --template-file container-fleet.generated.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ContainerImageUri="$URI:v1"
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

## Validated run

`container-fleet.generated.yaml` came from one invocation of the deployed
runtime with the Scenario 1 prompt. Before answering, the GameLift specialist
checked it with its read-only `validate_cloudformation_template` tool. It was
then deployed without edits:

| Check | Result |
|---|---|
| `cfn-lint` 1.57.1 | No errors (one W3005 redundant `DependsOn` warning) |
| `aws cloudformation validate-template` | Passed |
| Stack create (`CAPABILITY_NAMED_IAM`, image URI only) | `CREATE_COMPLETE` |
| Fleet | `ACTIVE`, one c6i.large instance in the stack's Region |
| Players (in-cluster load job, 8 players for 30 s) | 8/8 receiving echoes |

What the template contains: a container group definition for UDP 7654 (the
Agones container port), a container fleet whose role has
`GameLiftContainerFleetPolicy`, a game session queue, and a target-based
`PercentAvailableGameSessions` scaling policy. It omits the connection port
range and inbound permissions, so GameLift computes and opens them.

The saved file is the template body that CloudFormation stored for the stack.
CloudFormation stores non-ASCII characters as `?`, so the agent's
box-drawing characters in comment lines show up as `??`. Nothing else differs.

Agent output varies from run to run, so validate each new template (step 2)
before you deploy it.
