# Scenario 2: fresh GameLift setup for an Unreal Engine 5 game

The Sentinel agent takes a studio with no hosting infrastructure to a deployable
Amazon GameLift Servers container fleet for an Unreal Engine 5 dedicated server,
in one interaction. The answer is grounded in the GameLift KB: the Unreal plugin
docs, Server SDK integration, deploying a container fleet with the plugin, and
the container-fleet blog posts.

| Path | Purpose |
|---|---|
| `container-fleet.generated.yaml` | A template the agent produced from the Scenario 2 prompt, deployed unedited (see [Validated run](#validated-run)) |

## Prompt

See [`../RECORDING.md`](../RECORDING.md) for the exact prompt. It asks for three
things: integrating the plugin and packaging the Linux server as a container
image, a CloudFormation template for a container fleet (UDP 7777, up to 16
players per server process), and the steps to push the image, deploy, and test
a session.

## Deploy the generated template

```bash
aws cloudformation deploy --stack-name gl-demo-unreal \
  --template-file container-fleet.generated.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ContainerImageUri="<ecr-repo-uri>:<tag>"
```

The image parameter name is whatever the agent chose (`ContainerImageUri` in
the saved template). Every other parameter has a working default.

## Validated run

A real Unreal dedicated server needs an Unreal project and a Linux
cross-compile toolchain, so it isn't part of this repository. To prove that the
infrastructure works, the demo uses the GameLift-ready UDP echo image from
[`../agones-gamelift-migration/gamelift/server`](../agones-gamelift-migration/gamelift/server)
with `GAME_PORT=7777`. That image runs the GameLift Server SDK lifecycle
(`InitSDK`, `ProcessReady`, session activation, `ProcessEnding`), the same
calls the Unreal plugin makes. To produce the variant, set the `GAME_PORT`
environment variable when you build the image, or override it in the container
definition.

| Check | Result |
|---|---|
| Agent self-check (`validate_cloudformation_template`) | `valid` before answering |
| `cfn-lint` 1.57.1 | No errors |
| Stack create (`CAPABILITY_NAMED_IAM`, image URI only) | `CREATE_COMPLETE` |
| Fleet | `ACTIVE`, one c6i.large instance in the stack's Region |
| Players (in-cluster load job against the game session, 16 players for 30 s) | 16/16 receiving echoes |

What the template contains: a container group definition for UDP 7777 with
Server SDK 5.2.0, a container fleet whose role has
`GameLiftContainerFleetPolicy`, a game session queue, and a target-based
`PercentAvailableGameSessions` scaling policy.

Agent output varies from run to run. Validate each new template with `cfn-lint`
and `aws cloudformation validate-template` before you deploy it.
