"""E4 operations control-plane runtime (issue #416).

This package holds the deployable, protocol-neutral runtime for the E4 control
plane: the fail-closed kill-switch gate over the AWS AppConfig Lambda extension,
the bounded workspace-scoped list/detail/ledger projections, the admin-only
control service and its AppConfig publisher, the immutable control audit store,
the periodic expiry sweeper, capability discovery, and the E4 route handlers and
metrics. It builds strictly on the additive, immutable
:mod:`operations.contracts.control_plane` contract prelude and adds no new
provider write path — E3 remains the sole action.
"""
