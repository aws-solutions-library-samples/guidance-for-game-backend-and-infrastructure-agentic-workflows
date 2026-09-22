"""Shared kill-switch gate bootstrap for the write-capable entrypoints (#416).

The three deployable write-capable entrypoints — E2 prepare (observe Lambda),
the E3 dispatcher, and the E3 executor — all construct the SAME fail-closed
:class:`~operations.control.kill_switch_gate.KillSwitchGate` over the SAME real
:class:`~operations.control.appconfig_extension.AppConfigExtensionClient`. This
module centralizes that construction so every write path enforces the switch
identically.

Backward compatibility (ADR / issue #416): a pre-E4 deployment that has NONE of
the AppConfig identifiers configured builds no gate (``None``), which the
services treat as "always allow" — the write path behaves exactly as it did
before E4. A configured deployment builds a real gate that reads the switch
fresh on every enforcement point and fails closed on any unavailable / invalid /
stale document. Partial configuration is rejected earlier, at settings
resolution (:func:`resolve_kill_switch_extension_settings`), so it never reaches
here.
"""

from __future__ import annotations

# Standard library
from datetime import datetime
from typing import Any, Callable

# Local modules
from operations.contracts.control_plane import CAPABILITY_ID
from operations.control.appconfig_extension import AppConfigExtensionClient
from operations.control.kill_switch_gate import KillSwitchGate
from operations.settings import KillSwitchExtensionSettings


def build_kill_switch_gate(
    *,
    extension_settings: KillSwitchExtensionSettings | None,
    static_authority: str,
    clock: Callable[[], datetime] | None = None,
    opener: Callable[[str, float], Any] | None = None,
    unavailable_callback: Callable[[], None] | None = None,
) -> KillSwitchGate | None:
    """Build the real gate over the real extension client, or ``None`` if absent.

    ``opener`` is injected only by tests to serve the localhost extension bytes
    in memory; production passes ``None`` so the client uses ``urlopen`` against
    the in-environment AppConfig extension sidecar.
    """
    if extension_settings is None:
        return None
    extension = AppConfigExtensionClient(
        application=extension_settings.application,
        environment=extension_settings.environment,
        profile=extension_settings.profile,
        port=extension_settings.extension_port,
        opener=opener,
    )
    return KillSwitchGate(
        extension=extension,
        capability_id=CAPABILITY_ID,
        static_authority=static_authority,
        clock=clock,
        unavailable_callback=unavailable_callback,
    )
