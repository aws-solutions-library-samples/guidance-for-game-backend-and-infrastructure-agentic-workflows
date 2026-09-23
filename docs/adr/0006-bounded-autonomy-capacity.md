# ADR 0006: Add a bounded-autonomy capacity contract layer (v2)

Status: Proposed

## Context

[ADR 0001](0001-preserve-chat-and-add-optional-operations.md) reserved an
`operate` rung on the authority lattice for narrowly bounded autonomy — a single
capacity write authorized deterministically with no human in the loop — while
[the threat model](../THREAT_MODEL.md) O7 enumerates its hazards (poisoned or
stale observations, a model proposal mistaken for authorization, runaway
loops/oscillation/cost, autonomous policy bypass, missing emergency disablement,
and verification failure).

The published `gamelift.capacity-adjustment/1.0` contracts (E2 prepare/approve,
E3 execute) **deliberately forbid** autonomous authorization at the schema level:
the prepared-operation `authority.decision` enum is exactly
`["approval_required","denied"]`, the authorization semantics reject `authorized`
for a GameLift capacity change, and both carry `required_execution_authority`
`const "remediate"` with the E3 executor requiring a granted human approval bound
by hash before the single write. Encoding a no-human-approval `authorized`
decision at `operate` authority into those v1 documents would **change the
meaning** of published `1.0` schemas — forbidden by the
[compatibility rules](../OPERATIONS_CONTRACTS.md#compatibility-and-publication).

## Decision

Add bounded autonomy as a **new, additive v2 layer** —
`gamelift.capacity-adjustment/2.0` — delivered as three new strict schemas, a new
contract module, a new playbook definition, and a pure deterministic policy
evaluator. Every published v1 schema, vector, hash, and meaning is left
byte-for-byte unchanged.

1. **A new capability version, not a v1 edit.** The autonomy documents carry a
   `*_contract_version` of `"2.0"` and distinct `urn:...:v2:...` `$id`s. The
   schema and vector files live under physical `schemas/v2` and
   `fixtures/operations/v2` directories, separate from the frozen v1 artifacts.
   New `$defs` are defined *inline* in the v2 schema files; only the frozen v1
   `common` `$defs` are *referenced* unchanged, so no v1 schema re-hashes
   (`common` is not touched). The v2 schema names are disjoint from the published
   write-contract set and from the E2/E3 capacity/execution sets. A parallel
   `is_supported_autonomy_version("2.0")` is added; the v1 `== "1.0"` allowlist is
   not loosened.

2. **Three v2 contracts.**
   - **Immutable bounded-autonomy policy**
     (`gamelift-capacity-autonomy-policy`): the entirely server-owned guardrail
     envelope. It binds the capability `2.0`, a distinct trusted **automation**
     principal (`source_type: const "automation"`, distinct from the human
     `trusted_identity`), tenant/workspace/enrollment and the exact enrolled
     fleet/location, the exact `0/1/1` capacity envelope (`const`), the risk
     ceiling, observation freshness, an **integer micro-USD** per-action and
     per-window budget, cooldown, frequency, `max_in_flight: const 1`
     concurrency, anti-oscillation, decision expiry, and the exact
     playbook/executor identities/versions/hashes. `required_execution_authority`
     is `const "operate"`. Its `policy_hash` binds every field except itself.
   - **Deterministic autonomous decision**
     (`gamelift-capacity-autonomous-decision`): exactly one of
     `authorized`/`denied` at `operate` authority. A non-denied decision is
     `authorized` with the single reason code `APPROVED_AUTONOMOUS` and effective
     authority `operate` (the deterministic minimum of the six ADR 0001 inputs);
     any guardrail breach denies with the specific closed reason code(s). It binds
     the exact policy id/version/hash and observation id/hash and carries a
     `decision_expires_at` no later than either `observation.expires_at` or
     `evaluated_at + policy.decision_ttl_seconds`. Contract validation enforces
     the observation clamp; policy binding independently enforces the TTL clamp.
     `POLICY_DENIED` and `DECISION_EXPIRED` remain closed runtime reason codes for
     later policy-resolution and execute-time checks and are intentionally not
     emitted by the pure evaluator. It has **no human-approval field**: the
     decision itself is the time-boxed grant.
   - **Immutable autonomous prepared operation**
     (`gamelift-capacity-autonomous-operation`): the `authority.decision` enum is
     the `["authorized","denied"]` mirror of the v1 `["approval_required",
     "denied"]` prepared operation. It binds the autonomy policy id/version/hash,
     the decision id/hash, the observation id/hash, the exact desired/min/max
     change, the six authority inputs and their minimum, the calculated risk, the
     automation principal scope, the executor binding, `decision_expires_at`
     (which cannot exceed the bound observation expiry), and
     `required_execution_authority: const "operate"`. Its `prepared_hash` binds
     every field except itself. There is no approval field anywhere.

3. **Model/untrusted input supplies none of the above.** Identity, policy,
   limits, authorization, playbook, executor, and credentials are all server-owned
   and hash-bound. `additionalProperties` is `false` everywhere, so injecting any
   such field fails validation. The evaluator matches the automation principal
   against the policy and never trusts a request-body or model-supplied identity.

4. **A pure deterministic policy evaluator.** `evaluate_autonomy_policy` is a
   total function of trusted inputs only (resolved policy, six authority inputs,
   verified automation principal, trusted observation, proposed capacity triple,
   durable rolling-window state, and a supplied epoch clock). It performs no AWS,
   runtime, or infrastructure work, reads no environment, and holds no credential.
   Identical inputs always yield an identical reading; the caller binds that
   reading into the hash-bound decision. Money is integer micro-USD only.

5. **Preserve the executor identity; distinct autonomy playbook.** The v2
   playbook (`playbook.gamelift-capacity-autonomy` / `2.0.0`) **reuses** the
   E2/E3 executor binding (`executor.gamelift-capacity` / `1.0`) — the single
   `UpdateFleetCapacity` write machinery is unchanged — but its precondition set
   (the guardrail envelope instead of a granted human approval) and its
   `operate` execution authority make its hash **distinct** from the E2
   `playbook.gamelift-capacity` / `1.0.0` hash.

This ADR covers the **contracts and the pure evaluator only**. It defines no
infrastructure, no runtime service, no executor code path, no IAM, and no config
levers. The default chat path and default deployment are unchanged: nothing is
created, authorized, or executed. A future issue may wire a decision service,
an execute-time re-verifier, durable rolling-window state, config opt-in, and a
separate CloudFormation stack, and must not weaken the boundaries here.

## Consequences

- The `operate` rung is now realized as a *contract and a deterministic
  evaluator*, closing the O7 threats at the design level: freshness/skew
  (OP-AU1), deterministic authorization over an exact capability/playbook/executor
  (OP-AU2), risk/budget/cooldown/frequency/concurrency/anti-oscillation limits
  (OP-AU3), the six-ceiling minimum requiring `operate` (OP-AU4), and a distinct
  automation identity that is not authorization (OP-ID1).
- Because the executor id is preserved but the capability *version* and playbook
  *hash* differ, a v1 human-approved executor path and a v2 autonomous operation
  can never be confused: a future execute-time allowlist keys on the capability
  version and playbook/executor hashes.
- Emergency disablement (OP-AU5) still applies: a `disabled` deployment mode
  denies unconditionally and is the default; the evaluator denies with
  `DEPLOYMENT_DISABLED`. Execute-time re-verification of the rolling-window
  guardrails, kill-switch coverage, and the module-hash binding of the deployed
  executor are deferred to the future wiring issue and must fail closed.
