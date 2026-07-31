# ADR 0001: Preserve Chat and Add Optional Operations

- **Status:** Accepted
- **Date:** 2026-07-30
- **Decision issue:** [#276](https://github.com/aws-solutions-library-samples/guidance-for-game-backend-and-infrastructure-agentic-workflows/issues/276)

## Context

Game Agent currently provides a read-only conversational experience through
Next.js, CopilotKit, and the AgentCore runtime. Existing deployments and users
must keep that path without a login, router, or user-interface framework
migration.

Operations need stronger guarantees than a conversational request: exact
capability selection, deterministic authorization, durable state, and a
separate write boundary. Adding those guarantees directly to the chat runtime
would expand its permissions and failure surface.

## Decision

Keep the existing chat path as the default experience. Add the operations
control plane as an optional deployment that is omitted from the default
deployment path.

Use one backend-enforced deployment ceiling:

```text
GBAW_OPERATIONS_MODE=disabled|observe|advise|remediate|operate
```

The default is `disabled`. The default deployment creates no operations
resources and adds no operations cost. Operations settings must use a
centralized configuration module and the existing deployment-settings
resolution pattern.

The backend calculates effective authority as the lowest of:

- deployment mode;
- tenant policy;
- workspace policy;
- principal authority;
- capability maximum; and
- operation risk policy.

The UI may display the effective authority but cannot increase it. A model may
propose an operation but cannot grant authority or lower its risk.

UI changes are delivered in small phases. Chat remains the first view, and
operations functions appear only after backend capability discovery confirms
that they are available.

## Consequences

- Existing deployments remain read-only after an upgrade unless an owner
  explicitly deploys and enables operations.
- Early operations work does not require a CopilotKit upgrade, Next.js router
  migration, or broad UI redesign.
- Each phase can add capabilities without changing the meaning of the
  deployment ceiling.
- Configuration, capability, and risk errors fail closed at the backend even
  if a UI control is incorrectly shown.

## Rejected Alternatives

- Replacing the current chat experience with an operations application.
- Treating a hidden UI control as authorization.
- Giving the chat runtime permission to perform provider writes.
