# GameLift container-fleet target (agent-generated)

In the demo, the **Sentinel agent generates** the CloudFormation for the Amazon
GameLift Servers **container fleet** in a single shot (Scenario 1, step 2),
grounded in the GameLift KB (Containers Starter Kit + container-fleet posts).
We intentionally do **not** hand-author the template here — producing it is the
agent's job on camera. This file records the target shape so the generated IaC
can be checked and deployed.

## Target shape

- `AWS::GameLift::ContainerGroupDefinition` — the game-server container
  (same Linux `simple-game-server` image/binary as the Agones fleet), container
  port **7654/UDP**, with the GameLift container-group schema.
- `AWS::GameLift::ContainerFleet` — the fleet hosting the container group, with
  connection port range for UDP, an instance type, and a location.
- A session-placement **queue** and a **target-tracking scaling policy**.

## Known-good baseline

The authoritative starting point is the **Amazon GameLift Servers Containers
Starter Kit** (in the `aws/amazon-gamelift-toolkit` repo), which ships a
validated CloudFormation template and build automation and supports existing
Unreal/Unity/other Linux server builds. Juho Jantunen's "Faster multiplayer
hosting with containers on Amazon GameLift Servers" post (now in the KB) walks
through it.

## Validation at record time

1. Save the agent's generated template to `gamelift/container-fleet.generated.yaml`.
2. `aws cloudformation validate-template --template-body file://...`
3. Deploy to the sandbox; confirm the fleet reaches `ACTIVE` and
   `aws gamelift describe-fleet-attributes` / the agent's "list my GameLift
   fleets" shows it.
4. Re-point `client/udp_load.py` at the fleet endpoint for the cutover shot.
