# Recording runbook: Sentinel agent GameLift demos

Two short videos, each driven by one prompt to the deployed assistant:

1. **Scenario 1.** Migrate a live Agones fleet on Amazon EKS to Amazon GameLift
   Servers ([`agones-gamelift-migration/`](agones-gamelift-migration/)).
2. **Scenario 2.** Fresh GameLift setup for an Unreal Engine 5 game
   ([`unreal-gamelift-setup/`](unreal-gamelift-setup/)).

**Honest framing (say it on camera).** The assistant is read-only. It inspects
the environment and generates the infrastructure as code (IaC) plus the plan. A
person reviews, validates, and deploys the template. The assistant never
creates resources.

## Before you record

- [ ] The assistant is deployed (`scripts/deploy.sh`) with the GameLift prompt
      that includes the template self-check, and the GameLift KB is seeded.
      Check:
      `aws bedrock-agent-runtime retrieve --knowledge-base-id <gamelift-kb-id> --retrieval-query text="GameLift plugin for Unreal container fleet"`
      returns `unreal-plugin*.md`.
- [ ] Scenario 1 environment is up: the EKS cluster, Agones, the
      `simple-game-server` fleet, and agent read-only RBAC. See the
      [agones-gamelift-migration runbook](agones-gamelift-migration/README.md),
      steps 1–7.
- [ ] Game server image pushed to ECR
      ([`gamelift/README.md`](agones-gamelift-migration/gamelift/README.md),
      step 1). For Scenario 2, also create an image whose `GAME_PORT` is `7777`.
- [ ] Pre-deploy the stacks (fleets take 20–40 minutes to reach `ACTIVE`). On
      camera, show the deploy command, then cut to the stack that is already
      `ACTIVE`. Use the agent's own output from a rehearsal run, unedited.
- [ ] Sign in to the web UI with a demo user in a **new chat**. A new chat has
      no earlier conversation history that the model could draw on.
- [ ] Fallback: `gamelift/container-fleet.reference.yaml` is a hand-checked
      template. If a take produces a template that fails validation, re-run the
      prompt rather than editing the template on camera.

## Scenario 1: Agones on EKS to GameLift (about 4 minutes)

**Prompt (paste exactly):**

> I'm running a containerized multiplayer game-server fleet on Agones on Amazon EKS (cluster agones-demo in us-east-1, namespace default, Agones fleet simple-game-server, Linux server on UDP 7654 with dynamic host ports 7000-8000). Migrate me to Amazon GameLift Servers. In one shot: (1) inspect my current Agones/EKS setup, (2) produce a complete, deployable CloudFormation template for an Amazon GameLift Servers container fleet that replaces it (container port 7654 UDP), including the container group definition, the fleet, a game session queue, and a target-tracking scaling policy, and (3) give a zero-downtime cutover plan using GameLift Anywhere. Cite the GameLift best practices you base this on.

| # | Shot | What to show |
|---|---|---|
| 1 | Before | `kubectl get fleets,gameservers`; the in-cluster load job shows `active players: N/N` on Agones |
| 2 | Ask | Paste the prompt; the answer takes about 2 minutes (EKS inspection, KB retrieval, template self-check) |
| 3 | Answer | Overview, then **Generated infrastructure as code**: the specialist's plan, template, and cutover steps, shown as produced |
| 4 | Validate | Download the template; run `cfn-lint` and `aws cloudformation validate-template` |
| 5 | Deploy | Run `aws cloudformation deploy ... --capabilities CAPABILITY_NAMED_IAM` with only the image URI; cut to the pre-deployed `ACTIVE` fleet |
| 6 | After | `aws gamelift create-game-session`; point the load job at the session's IP and port; `active players: N/N` on GameLift |
| 7 | Close | Read-only assistant, generated IaC, human-approved deploy; mention the cutover plan |

The zero-downtime cutover through GameLift Anywhere is part of the plan the
assistant writes. It is not executed in this demo.

## Scenario 2: Unreal Engine 5 fresh setup (about 3 minutes)

**Prompt (paste exactly):**

> I'm building a multiplayer game in Unreal Engine 5 and want to host its dedicated servers on Amazon GameLift Servers. I have no hosting infrastructure yet. The Linux dedicated server listens on UDP 7777 and runs one match of up to 16 players per server process; start in us-east-1. In one shot: (1) explain how to integrate the GameLift Server SDK into my Unreal project with the Amazon GameLift Servers plugin for Unreal and package the Linux server as a container image, (2) produce a complete, deployable CloudFormation template for a GameLift Servers container fleet, including the container group definition, the fleet, a game session queue, and a target-tracking scaling policy, and (3) give the steps to push the image, deploy the stack, and test a game session. Cite the GameLift best practices you base this on.

| # | Shot | What to show |
|---|---|---|
| 1 | Ask | Paste the prompt; the answer takes about 2 minutes |
| 2 | Answer | Plugin install, Server SDK callbacks in the game mode, Linux packaging, a Dockerfile, the template, and the deploy and test steps |
| 3 | Validate and deploy | Same as Scenario 1, steps 4–5 (stack `gl-demo-unreal`) |
| 4 | Test | Create a game session with 16 maximum players; the load job shows `16/16` |

Say on camera that the server image is a stand-in for a packaged Unreal build.
It is the echo server on UDP 7777, which runs the same GameLift Server SDK
lifecycle.

## Timings observed in rehearsal

| Step | Time |
|---|---|
| Agent answer | 100–170 s |
| Container group definition `COPYING` → `READY` | A few minutes |
| Fleet `ACTIVE` | 20–40 min |
| Game session `ACTIVE` | Under 30 s |

## After recording

Delete everything; the cluster and fleets cost money while they run. See
**Teardown** in the
[agones-gamelift-migration runbook](agones-gamelift-migration/README.md#teardown-do-this-after-recording--the-cluster-and-fleets-cost-money).
