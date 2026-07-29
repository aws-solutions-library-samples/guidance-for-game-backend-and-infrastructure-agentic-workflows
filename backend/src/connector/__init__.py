"""Source Control Connector package.

Provides the opt-in, fail-closed GitOps write path that lets the agent propose
Infrastructure-as-Code (IaC) changes as change proposals instead of mutating live AWS
resources. See ``.kiro/specs/source-control-connector`` for the design.

Registration bootstrap: the provider-neutral core (``connector.service`` /
``connector.config``) selects an adapter only through ``connector.registry`` and never
imports a concrete adapter. Adapters only become "supported" once their module is imported
(self-registration at import time). To guarantee that ``registry.is_supported("github")``
is true during ``ConnectorConfig.load()`` — and to keep the core free of any static adapter
import (Req 4.1) — the package eagerly imports its bundled adapter(s) here. Importing any
connector submodule therefore runs this bootstrap first, so registration always happens
before enablement is evaluated or ``get_provider`` is called.
"""

# Import bundled provider adapters for their self-registration side effect (each adapter
# calls ``registry.register(...)`` at import time). This is the neutral bootstrap point:
# the core modules import ``connector.registry`` only, never these adapter modules.
from connector import github_provider  # noqa: E402,F401 (registers the bundled adapter)
