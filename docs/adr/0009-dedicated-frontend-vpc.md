# ADR 0009: Give the Frontend a Dedicated VPC

- **Status:** Proposed
- **Date:** 2026-11-04
- **Decision issue:** #728
- **Related:** [#588](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/588)
  (private network path for read-only cluster inspection)

## Status Rationale

This record is **Proposed**. Like ADR 0005 and ADR 0007, it merges as Proposed
and commits nothing: no template, script, or deployed resource changes with
it, and deployed frontends keep using the account's default VPC. A later pull
request flips it to **Accepted** after spikes S1 to S5 in the validation plan
have run in a disposable account and their results have been reviewed. The
spikes test Express Mode behavior that the AWS documentation does not
describe, and S1 and S3 can change the `private` mode decision (see Open
Questions).

## Context

### Current placement

The checked-out source places only the frontend in a VPC, and only
implicitly:

- `infrastructure/cloudformation/02-frontend-ecs-express.yaml` defines the
  frontend as an `AWS::ECS::ExpressGatewayService` without
  `NetworkConfiguration`. The CloudFormation reference states that an Express
  service then uses the subnets and security groups of the default VPC
  [R1, R2]. No other template, `scripts/deploy.sh`, `scripts/teardown.sh`, or
  the PowerShell module references a VPC, subnet, security group, or network
  mode. The separate `demo/` Agones walkthrough is not part of the guidance
  deployment.
- `scripts/deploy.sh` runs `agentcore configure` without network options, so
  the AgentCore Runtime uses `PUBLIC` network mode, outside any VPC (#588).
  Knowledge Bases, Cognito, Guardrails, Prompt Management, and the
  cost-snapshot DynamoDB table are regional managed services that the solution
  does not place in a VPC.
- The frontend server (`ui/src/pages/api/`) makes four kinds of outbound
  call: it fetches the Cognito JWKS document through `aws-jwt-verify`, calls
  Cognito `InitiateAuth` to refresh tokens, calls STS `GetCallerIdentity` to
  build the runtime ARN, and calls `InvokeAgentRuntime` on the regional
  `bedrock-agentcore` endpoint with the verified access token as a bearer
  token. Fargate also pulls the image from Amazon ECR and delivers `awslogs`
  output to CloudWatch Logs with the task execution role. The task role has no
  policies. Browser sign-in calls Cognito directly from the client
  (`ui/src/components/CognitoAuth.tsx`), not through the frontend server.

### What Express Mode does with the default

The Express Mode developer guide [R3] documents these defaults:

- Without subnets, Express Mode uses the default VPC's public subnets. The
  default VPC must have at least two public subnets in at least two
  Availability Zones, each with at least eight free IP addresses per CIDR
  block.
- The subnets decide the load balancer scheme. Public subnets produce an
  internet-facing Application Load Balancer and tasks with `assignPublicIp`
  enabled. Private subnets, meaning subnets without an internet gateway in
  their route table, produce an **internal** load balancer, and the operator
  must provide NAT for any outbound access the tasks need.
- Without security groups, Express Mode creates a service security group and a
  load balancer security group with minimal ingress. A supplied security group
  adds "an additional ingress path to your service".
- Up to 25 Express Mode services in the same VPC share one load balancer, and
  the first Express Mode service in a VPC defines the subnets of that VPC's
  internet-facing or internal load balancer.
- Updates are canary deployments [R4]. CloudFormation classifies a
  `NetworkConfiguration` change as *No interruption* [R1], but neither page
  describes moving a service to another VPC. A load balancer belongs to one
  VPC, so such a move needs a different load balancer. Whether the service
  endpoint, its certificate, and a WAF association survive the move is not
  documented.
- Deleting an Express service drains its tasks, then removes its service
  security group, certificate, alarms, and scaling resources. It also removes
  the load balancer and its security group when no other service uses them
  [R5, R6].

### Observed in a current deployment

A read-only inspection of a maintainer deployment in `us-west-2` on 2026-11-04
matched these defaults:

- The service runs in the default VPC across all four of its default subnets.
- The load balancer is internet-facing, with one public IPv4 address in each
  of the four Availability Zones.
- Its only listener is HTTPS on 443, with one host-header rule, so no other
  service shares it today.
- Its managed security group admits TCP 80 and 443 from any address.
- The single running task holds a public IPv4 address.
- The task security group admits only the load balancer security group, on the
  container port.

### Problems with the default VPC

1. **Accounts without a usable default VPC.** AWS Control Tower removes the
   default VPC when Account Factory creates an account [R24], and many
   organizations delete it or restrict it with service control policies.
   Express Mode documents no fallback, so the frontend stack cannot be created
   in those accounts. Both deployment scripts stop at the frontend step, after
   the backend resources already exist.
2. **Shared accounts.** Every workload that does not choose another VPC uses
   the account's default VPC. Express Mode can place another Express service
   in that VPC on this frontend's load balancer [R3], and a resource can have
   only one web ACL [R12]. The project web ACL that the security stack
   associates and that `deploy.sh` reconciles (#459) would then govern that
   service's traffic too. The 190-second idle timeout that `deploy.sh` sets
   for long agent runs (#250) would also apply to it. The other owner's
   changes would apply here in turn, and tearing down the security stack would
   remove the shared load balancer's web ACL. Route tables, network ACLs, and
   flow-log coverage also belong to whoever manages the default VPC.
3. **No reviewed network boundary.** The frontend's network is whatever the
   account's default VPC happens to be. It is not a reviewed part of the
   deployment. `SECURITY.md` describes the frontend as deployed in an
   "AWS-managed VPC", but the default VPC is a customer-owned VPC in the
   deploying account.
4. **Cost follows the Region, not the workload.** The default VPC has a
   default subnet in every Availability Zone, and in the observed deployment
   the load balancer spans all of them, holding one public IPv4 address per
   zone. Public IPv4 addresses are billed hourly [R16], and the sample cost
   table in `README.md` does not list them today.

## Decision Drivers

1. Deploy unchanged in accounts that have no default VPC.
2. Isolate the frontend's load balancer, web ACL association, and load
   balancer attributes from unrelated workloads.
3. Keep today's public, Cognito-gated entry point, the WAF protection, and the
   Express-managed HTTPS endpoint, without requiring a custom domain or
   certificate.
4. Expose only HTTPS 443 on the load balancer and give tasks no direct inbound
   path.
5. Keep fixed network cost proportionate to a sample deployment: no NAT
   gateway or interface endpoint charges unless an operator opts in.
6. Let operators with landing-zone networking supply their own subnets.
7. Keep `scripts/deploy.sh`, `scripts/teardown.sh`, and the PowerShell module
   behaviorally identical, and upgrade existing deployments without surprise.
8. Leave the runtime network decision (#588) open without making it here.

## Considered Options

| Option | Load balancer and reachability | Task outbound path | Fixed monthly change vs today | Main trade-offs |
|---|---|---|---|---|
| A. Default VPC (today) | Internet-facing, in every default subnet | Task public IPv4 address | Baseline: five public IPv4 addresses, $18.25 | Fails without a default VPC; can share a load balancer; no reviewed boundary |
| B. Dedicated VPC, two public subnets | Internet-facing, two zones | Task public IPv4 address; inbound only from the load balancer security group | -$7.30 (three addresses) | Tasks keep public addresses; no NAT or endpoints |
| C. Dedicated VPC, two private subnets with NAT | **Internal**: reachable only from networks routed into the VPC | One NAT gateway per zone | +$54.75, plus $0.045 per GB processed | No public entry point without option E |
| D. Dedicated VPC, two private subnets with interface endpoints | **Internal** | Interface endpoints for `ecr.api`, `ecr.dkr`, `logs`, `sts`, `bedrock-agentcore`, and `cognito-idp`, plus an S3 gateway endpoint | +$69.35, plus $0.01 per GB | Highest fixed cost; JWKS retrieval through the Cognito endpoint is undocumented; any uncovered egress fails |
| E. Private tasks behind a public entry point | Internet-facing through a CloudFront VPC origin (E1) [R27] or a self-managed load balancer (E2) | NAT or endpoints | Not estimated | E1 moves WAF to CloudFront scope and must work with Express Mode's host-header rule and certificate; E2 leaves Express Mode and needs a custom domain and certificate |
| F. Operator-supplied subnets | Follows the subnets: public gives internet-facing, private gives internal | The operator's network | Depends on the operator's network | Fits landing zones; needs pre-flight validation |

Options C and D follow from Express Mode's scheme rule [R3] and the
definition of an internal load balancer, whose nodes have only private
addresses [R11]. Option D's endpoint list follows the Fargate and Amazon ECS
endpoint guidance [R8, R9] and the AgentCore data-plane endpoint [R14]. The
Cognito endpoint serves user pool API operations only for pools without a
domain, and this solution assigns none, but its documentation does not cover
the issuer's JWKS document [R15].

Pricing basis: 730 hours per month. The public IPv4 address, NAT gateway, and
interface endpoint rates are `us-west-2` on-demand rates confirmed with the
AWS Price List API on 2026-11-04 [R16, R17]. A public IPv4 address costs
$0.005 per hour ($3.65 per month). A NAT gateway costs $0.045 per hour and
$0.045 per GB processed, and the pricing page's regional NAT gateway example
bills the zonal hourly rate for each zone served [R16]. An interface endpoint
costs $0.01 per hour in each zone and $0.01 per GB. The baseline is the
observed shape: four load balancer addresses and one task address. Option C
counts two NAT gateways and their two addresses. A single NAT gateway would
cost +$18.25 instead, but it leaves one zone's egress dependent on the other.
Each additional running task adds $3.65 in options A and B, and in option F
with public subnets. No option changes the load balancer's hourly and capacity
charges, Fargate, or WAF. Optional VPC flow logs add vended-log ingestion,
$0.50 per GB delivered to CloudWatch Logs at the first-tier rate in the
pricing page's N. Virginia example [R18], plus storage.

## Decision

Create a minimal, dedicated VPC in two Availability Zones for the frontend by
default, with public subnets (option B). Offer private subnets with NAT
(option C) and operator-supplied subnets (option F) as explicit choices. Keep
the AgentCore Runtime in `PUBLIC` network mode.

### 1. Network modes

One deployment setting, `GBAW_FRONTEND_NETWORK_MODE`, selects the frontend
network. Both deployment paths resolve it through
`config/load_deployment_settings.py`, like the model and identity settings,
and reject unknown values.

| Mode | Network | Load balancer | Intended use |
|---|---|---|---|
| `public` (default for new deployments) | New VPC with two public subnets | Internet-facing | The current public, Cognito-gated deployment |
| `private` | New VPC with two private subnets and NAT egress | Internal | Organizations that reach the UI only through their own network, such as a VPN, AWS Direct Connect, or AWS Transit Gateway |
| `existing` | Operator-supplied subnets and optional security groups | Follows the subnets | Landing zones and shared network accounts |
| `default-vpc` | The account's default VPC (today's template) | Internet-facing | Existing deployments until they migrate; deprecated |

`private` does **not** put tasks behind a public load balancer. Express Mode
provisions an internal load balancer for private subnets [R3], so the frontend
URL is reachable only from networks with a route into the VPC. The deployment
summary must say so instead of presenting the URL as public. A public entry
point for private tasks (option E) needs a separate decision.

`existing` takes `GBAW_FRONTEND_SUBNET_IDS` (required),
`GBAW_FRONTEND_SECURITY_GROUP_IDS` (optional), and `GBAW_FRONTEND_VPC_ID`
(optional, used only as a cross-check). The resulting scheme follows the route
tables of the supplied subnets.

### 2. A separate network stack

A new template, `infrastructure/cloudformation/06-frontend-network.yaml`,
holds the network for `public` and `private` and deploys as the
`<project>-network` stack. It is created only when the frontend is deployed,
so a backend-only deployment without Docker creates no NAT gateway. It
contains:

- one IPv4-only VPC with DNS support and DNS hostnames enabled, and a
  parameterized CIDR whose default avoids the default VPC range and leaves
  room for later subnets;
- exactly two subnets per tier, placed in two Availability Zones by zone ID
  (`AvailabilityZoneId` [R21]). The zone IDs come from a per-Region mapping of
  the zones that AgentCore Runtime supports for VPC connectivity [R13], with a
  parameter override. The service subnets are `/24`s, well above the `/27`
  load balancer minimum [R10];
- for `public`: an internet gateway and one public route table. The subnets
  keep `MapPublicIpOnLaunch` disabled, because Express Mode assigns task
  public addresses itself;
- for `private`: private route tables with NAT egress, provided either by one
  zonal NAT gateway per zone in two small public subnets or by one regional
  NAT gateway (`AvailabilityMode: regional` [R19, R20]). The choice is an open
  question;
- VPC flow logs to a CloudWatch Logs group with bounded retention, enabled by
  default to satisfy Security Hub control EC2.6 [R22, R23], with a parameter
  to disable them. The flow-log delivery role is the stack's only IAM role and
  is limited to log delivery;
- no interface endpoints and no security groups for the service. Express Mode
  keeps creating its own minimal-ingress service and load balancer security
  groups.

### 3. Frontend template change

`02-frontend-ecs-express.yaml` gains `SubnetIds` and `SecurityGroupIds`
parameters, comma-delimited lists that are empty by default. It sets
`NetworkConfiguration` only when subnets are supplied, so with empty values
the rendered resource is identical to today's. That is what `default-vpc` mode
deploys. No other frontend property changes.

### 4. Read-only pre-flight

Before any stack change, both deployment paths validate the target network
with read-only calls. They stop with a bounded, public-safe message when the
network is unsuitable:

- `public` and `private`: the selected zone IDs exist in the Region.
- `existing`: the subnets exist, belong to one VPC (matching
  `GBAW_FRONTEND_VPC_ID` when it is set), span at least two Availability
  Zones, and each have at least eight free addresses [R3, R10]. The check
  derives the resulting scheme from the route tables and prints it.
- `default-vpc`: a default VPC exists with at least two public subnets in two
  zones, so today's late stack failure becomes an early, explicit message.

### 5. Deployment and teardown order

Deployment: the network stack deploys after the base stack and before the
frontend stack. As today, the security stack, the WAF reconciliation (#459),
and the load balancer idle-timeout step (#250) then use the frontend stack's
`LoadBalancerArn` output.

Teardown: security, observability, Knowledge Bases, prompts, and the frontend
stack are deleted in today's order. Express Mode removes its load balancer and
security groups after the service drains [R6]. Before it deletes the network
stack, teardown therefore polls, with a time limit, until the VPC has no
network interfaces except those of the stack's own NAT gateways and no
security groups except the default group. On timeout it stops and reports the
network stack it left in place, without deleting those resources itself.
`existing` mode never creates or deletes networking, and `default-vpc`
teardown is unchanged.

### 6. Existing deployments and migration

Re-running the deployment over an existing frontend stack that has no subnet
parameters keeps `default-vpc` and prints a notice, unless the operator sets
`GBAW_FRONTEND_NETWORK_MODE` explicitly. Moving is an explicit, documented
step:

1. Deploy the network stack, or select `existing` subnets.
2. Update the frontend stack with the subnets **and** a new service name. A
   `ServiceName` change requires replacement [R1], and CloudFormation creates
   the replacement before deleting the old resource [R26]. The new service and
   its load balancer therefore exist in the new VPC before the old service
   drains.
3. Redeploy the security stack with the new `LoadBalancerArn`, then run the
   WAF reconciliation and the idle-timeout step.
4. Run this record's deployed validation against the new endpoint before
   announcing it.

The frontend URL changes, because the endpoint follows the service name
(`https://<service-name>.ecs.<region>.on.aws/` [R7]), and users sign in again
at the new address. The Cognito app client registers no callback URLs, and
the same-origin check compares the request's own host, so neither needs a
change. An in-place `NetworkConfiguration` update is not used unless spike S2
shows that the endpoint, certificate, and WAF association survive it.

### 7. Bash and PowerShell parity

`scripts/deploy.sh`, `scripts/teardown.sh`, and the PowerShell
`Deploy-GameAgent`, `Remove-GameAgent`, and `Get-GameAgentStatus` functions
implement the same modes, pre-flight checks, ordering, waits, outputs, and
messages, with tests on both sides.
`scripts/infrastructure/check-deployment.sh` and `validate-deployment.sh`
report the mode and the load balancer scheme.

The PowerShell frontend path currently differs from Bash in two ways that this
work depends on. It pushes only the `latest` image tag and passes no
`ImageTag`, and it does not set the 190-second load balancer idle timeout.
Migration replaces the load balancer, so both paths must have the
idle-timeout step before the migration is documented.

### 8. Unchanged boundaries

Cognito access-token verification at the proxy and the runtime, the
same-origin check, the WAF rules, and the read-only chat runtime do not
change. No permission is added to the chat runtime, the frontend task role, or
the task execution role.

## Relation to #588 and Runtime VPC Mode

#588 proposes AgentCore `VPC` network mode so the EKS specialist can reach
clusters whose Kubernetes API endpoints are closed to the runtime's public
path. This record does not decide that, but it keeps the option open. The
zone IDs come from the AgentCore-supported list, the CIDR is a parameter that
can avoid overlap with peered or transit-attached networks, `private` mode
provides private subnets, and `existing` mode lets an operator-owned network
account supply the subnets.

Moving the runtime is a separate and larger decision:

- A runtime in `VPC` mode has no internet access unless its private subnets
  route to a NAT gateway, and public subnets do not provide it [R13]. Every
  AWS API that the runtime and its embedded MCP servers call then needs NAT
  egress or an interface endpoint. The runtime's execution role grants actions
  in 15 IAM service prefixes (`autoscaling`, `aws-portal`, `bedrock`,
  `bedrock-agentcore`, `ce`, `cloudwatch`, `compute-optimizer`,
  `cost-optimization-hub`, `dynamodb`, `ec2`, `ecr`, `eks`, `gamelift`,
  `logs`, and `xray`). The runtime also fetches the Cognito JWKS and calls the
  Kubernetes API endpoints of enrolled clusters. AgentCore recommends ECR, S3
  gateway, and CloudWatch Logs endpoints even when NAT is present [R13].
- Its subnets must be in the Availability Zone IDs that AgentCore supports in
  each Region [R13].
- Runtime network interfaces can remain for up to eight hours after the
  runtime is deleted [R13], so a VPC shared with the runtime cannot be torn
  down promptly.
- If the frontend later calls the runtime through the `bedrock-agentcore`
  interface endpoint, OAuth callers succeed only when the endpoint policy
  allows every principal [R14]. The policy then cannot restrict callers by
  identity, so network reach depends on the endpoint's security group.
- These changes alter the runtime's threat model and the failure modes of all
  three specialists. A frontend network change does neither.

## Consequences

- New deployments work in accounts without a default VPC and no longer share
  a load balancer, a web ACL association, or load balancer attributes with
  unrelated workloads.
- In `public` mode the fixed network cost falls slightly, by $7.30 per month
  in `us-west-2` with one task, because the load balancer spans two zones
  instead of every default subnet. Optional flow logs add a small,
  volume-based charge.
- Tasks in `public` mode keep public IPv4 addresses, as today. Their only
  inbound path remains the load balancer security group on the container
  port. Removing the addresses requires option C, D, or E.
- The load balancer spans two zones instead of every zone in the Region, so a
  zone failure leaves one. With the default minimum of one task, recovery
  from losing the task's zone still depends on Amazon ECS replacing the task,
  as it does today.
- `private` mode adds about $55 per month plus NAT data processing, and it
  makes the UI internal-only.
- The deployment gains a stack, a setting, read-only pre-flight checks, and a
  teardown wait in both shells, with matching tests.
- Existing deployments are unaffected until an operator migrates. Migration
  changes the frontend URL.
- The implementation must update `docs/ARCHITECTURE.md`,
  `docs/DEPLOYMENT_GUIDE.md`, the `README.md` cost table (public IPv4
  addresses and, for `private`, NAT), the network section of `SECURITY.md`,
  and the internet entry point in `docs/THREAT_MODEL.md`, including the
  internal scheme in `private` mode.

## Validation Plan

Static checks, in the implementation pull request:

1. Run `cfn-lint` on `02-frontend-ecs-express.yaml` and the new template. The
   repository runs no CloudFormation linter in CI or pre-commit today, so add
   one. cfn-lint 1.57.0 accepts `AvailabilityMode`, `AvailabilityZoneId`, and
   a conditional Express `NetworkConfiguration`, and `02` passes it today.
   `05-security-infrastructure.yaml` reports E3012 because its
   `ExcludedRules` entry is a string rather than a `Name` object. A deployed
   web ACL shows the exclusion applied, but the entry needs the object form
   before a lint gate can pass.
2. Add template unit tests beside
   `backend/tests/unit/test_frontend_least_privilege_iam_unit.py`. They check
   that there are exactly two zones, that the solution's templates declare no
   ingress rule from `0.0.0.0/0` or `::/0`, that flow logs are on by default,
   and that `NetworkConfiguration` is absent when `SubnetIds` is empty.
3. Add settings-loader tests for every mode and invalid combination, Bash
   tests in `scripts/test/`, and Pester tests for the same cases
   (`./test-powershell.sh`).
4. Run `python3 scripts/check_public_content.py` and `git diff --check`.

Spikes in a disposable account, before acceptance:

- **S1.** Express Mode with private subnets: confirm the internal load
  balancer, how the service endpoint resolves inside and outside the VPC, and
  that its certificate is valid inside the VPC.
- **S2.** An in-place `NetworkConfiguration` change to another VPC on a
  running service: record the effect on the endpoint, certificate, WAF
  association, and idle-timeout attribute, and any downtime.
- **S3.** Mixed public and private subnets in one network configuration:
  record whether Express Mode rejects them, or which scheme and task placement
  result.
- **S4.** Supplied security groups: record whether Express Mode still attaches
  its own service group, and what ingress a supplied group needs.
- **S5.** Deletion: record whether the frontend stack reaches
  `DELETE_COMPLETE` before the load balancer, security groups, and network
  interfaces are gone, and how long they persist. The result sizes the
  teardown wait.

Deployed validation, through both shells:

1. **Fresh account without a default VPC.** Use an account that Control
   Tower Account Factory created, or a disposable account whose default VPC
   was deleted. `public` mode deploys end to end, and nothing is created in
   any other VPC.
2. **Existing deployment upgrade.** Re-running without a mode makes no
   frontend change. The opt-in migration creates the new service before the
   old one drains, and the security stack update succeeds after the old load
   balancer is gone. The project web ACL and its required rules are active on
   the new load balancer, and its idle timeout is 190 seconds.
3. **Live shakedown.** Run `ui/tests/live-shakedown.spec.ts` against the new
   endpoint, plus an authenticated request through each specialist.
4. **Anonymous denial.** An unauthenticated same-origin `POST
   /api/copilot/chat` returns the proxy's 401, a cross-origin one returns the
   CSRF 403, and neither returns 200. `GET /api/health` succeeds. The load
   balancer has only the HTTPS 443 listener, and a task's public address
   accepts no connection on the container port. In `private` mode, the
   endpoint is unreachable from outside the VPC.
5. **Teardown.** Both shells delete the network stack after the frontend and
   leave no network interfaces, security groups, Elastic IP addresses, or NAT
   gateways behind. `existing` mode leaves the operator's networking
   unchanged.
6. **Cost.** After a day, the billed public IPv4 hours, and NAT hours in
   `private` mode, match the estimate.

## Open Questions for Maintainers

1. Should `public` become the default for new deployments now, or should the
   dedicated VPC be opt-in for one release?
2. Should `private` ship while it yields an internal-only UI, or wait for a
   decision on a public entry point (option E)? If S3 shows that Express Mode
   accepts public load balancer subnets with private task subnets, should this
   record be revised before acceptance?
3. For `private`, should NAT be one zonal NAT gateway per zone, one regional
   NAT gateway, or a single zonal NAT gateway? A regional NAT gateway needs no
   public subnets, but it can take up to 60 minutes to expand to a new zone
   and is not available in constrained zones [R19]. A single zonal NAT gateway
   halves the cost but makes one zone's egress depend on the other.
4. Should flow logs be on by default? Should they capture `ALL` or `REJECT`
   traffic, and with what retention? CloudTrail uses 90 days.
5. What should the default CIDR be? Should it reserve address space for
   runtime network interfaces, in case #588 later uses this VPC rather than an
   operator network?
6. Is keeping `default-vpc` for existing deployments, with a notice, the right
   upgrade default? How long should the mode remain before removal?
7. Should the PowerShell idle-timeout and image-tag gaps be fixed in the
   implementation pull request, or tracked and fixed separately first?
8. Are `06-frontend-network.yaml` and `<project>-network` acceptable template
   and stack names?

## Rejected Alternatives

- Keeping the default VPC as the only option.
- Creating a default VPC during deployment where none exists. Its components
  cannot be customized, it has a default subnet in every zone [R25], a service
  control policy can deny its creation, and it brings back the shared-VPC
  problem.
- Private subnets with NAT as the default. Express Mode would make the load
  balancer internal and remove the public entry point.
- Interface endpoints instead of NAT as a default. They have the highest fixed
  cost, JWKS retrieval through the Cognito endpoint is undocumented, and any
  egress they do not cover fails.
- Changing Express-managed resources outside Express Mode, such as disabling
  `assignPublicIp` on the underlying service or editing the managed security
  groups.
- Adopting CloudFront VPC origins [R27] or leaving Express Mode as part of
  this change.
- Moving the AgentCore Runtime into the VPC as part of this change.

## References

- [R1] [AWS::ECS::ExpressGatewayService](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ecs-expressgatewayservice.html)
- [R2] [AWS::ECS::ExpressGatewayService ExpressGatewayServiceNetworkConfiguration](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-ecs-expressgatewayservice-expressgatewayservicenetworkconfiguration.html)
- [R3] [Resources created by Amazon ECS Express Mode services](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-work.html)
- [R4] [Updating an Amazon ECS Express Mode service](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-update-full.html)
- [R5] [Delete Amazon ECS Express Mode services](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-delete-task.html)
- [R6] [`delete-express-gateway-service`, AWS CLI Command Reference](https://docs.aws.amazon.com/cli/latest/reference/ecs/delete-express-gateway-service.html)
- [R7] [Create your first Express Mode service using the AWS CLI](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-getting-started.html)
- [R8] [Amazon ECS task networking options for Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-networking.html)
- [R9] [Amazon ECS interface VPC endpoints (AWS PrivateLink)](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/vpc-endpoints.html)
- [R10] [Application Load Balancers: Availability Zone subnets](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/application-load-balancers.html)
- [R11] [How Elastic Load Balancing works: load balancer scheme](https://docs.aws.amazon.com/elasticloadbalancing/latest/userguide/how-elastic-load-balancing-works.html)
- [R12] [AWS WAF: Associating or disassociating protection with an AWS resource](https://docs.aws.amazon.com/waf/latest/developerguide/web-acl-associating-aws-resource.html)
- [R13] [Configure Amazon Bedrock AgentCore Runtime and tools for VPC](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)
- [R14] [Use interface VPC endpoints (AWS PrivateLink) with AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-interface-endpoints.html)
- [R15] [Access Amazon Cognito using an interface endpoint (AWS PrivateLink)](https://docs.aws.amazon.com/cognito/latest/developerguide/vpc-interface-endpoints.html)
- [R16] [Amazon VPC pricing: NAT gateway, regional NAT gateway, and public IPv4 address](https://aws.amazon.com/vpc/pricing/)
- [R17] [AWS PrivateLink pricing](https://aws.amazon.com/privatelink/pricing/)
- [R18] [Amazon CloudWatch pricing: vended logs](https://aws.amazon.com/cloudwatch/pricing/)
- [R19] [Regional NAT gateways for automatic multi-AZ expansion](https://docs.aws.amazon.com/vpc/latest/userguide/nat-gateways-regional.html)
- [R20] [AWS::EC2::NatGateway](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ec2-natgateway.html)
- [R21] [AWS::EC2::Subnet](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ec2-subnet.html)
- [R22] [Logging IP traffic using VPC Flow Logs](https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs.html)
- [R23] [Security Hub controls for Amazon EC2: EC2.6](https://docs.aws.amazon.com/securityhub/latest/userguide/ec2-controls.html)
- [R24] [AWS Control Tower: About enrolling existing accounts](https://docs.aws.amazon.com/controltower/latest/userguide/enroll-account.html)
- [R25] [Work with your default VPC and default subnets](https://docs.aws.amazon.com/vpc/latest/userguide/work-with-default-vpc.html)
- [R26] [`UpdateReplacePolicy` attribute](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-attribute-updatereplacepolicy.html)
- [R27] [Restrict access with CloudFront VPC origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html)
