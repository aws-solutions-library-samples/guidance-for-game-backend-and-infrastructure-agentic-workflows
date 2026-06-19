"""Unit tests for production-safe logger configuration (#152).

loguru's `diagnose=True` annotates tracebacks with local variable VALUES, which
can leak secrets/tokens/PII into logs — it must be off in production. The stdout
handler previously hardcoded level=DEBUG + diagnose=True regardless of env. These
tests assert the decision logic resolves to safe values when debug logging is off
and verbose only when explicitly enabled.
"""

# Standard library
import importlib

# Third-party packages
import pytest

pytestmark = pytest.mark.unit


def _reload_logger_with(monkeypatch, *, enable_debug, log_level="INFO"):
    """Reload utils.logger with patched settings flags and return the module."""
    # Local modules
    from config import settings

    monkeypatch.setattr(settings, "ENABLE_DEBUG_LOGGING", enable_debug, raising=False)
    monkeypatch.setattr(settings, "LOG_LEVEL", log_level, raising=False)

    # Local modules
    import utils.logger as logger_mod

    return importlib.reload(logger_mod)


def test_production_stdout_is_not_diagnostic(monkeypatch):
    """With debug logging OFF, stdout logging must be non-diagnostic at LOG_LEVEL."""
    logger_mod = _reload_logger_with(monkeypatch, enable_debug=False, log_level="INFO")
    assert logger_mod._DEBUG_LOGGING is False
    assert logger_mod._STDOUT_LEVEL == "INFO"  # never DEBUG in prod


def test_debug_logging_opt_in_is_verbose(monkeypatch):
    """With the explicit dev debug flag ON, DEBUG + diagnose are allowed."""
    logger_mod = _reload_logger_with(monkeypatch, enable_debug=True, log_level="INFO")
    assert logger_mod._DEBUG_LOGGING is True
    assert logger_mod._STDOUT_LEVEL == "DEBUG"


def test_log_level_is_honored_when_debug_off(monkeypatch):
    """Prod stdout level follows LOG_LEVEL (e.g. WARNING), not a hardcoded DEBUG."""
    logger_mod = _reload_logger_with(monkeypatch, enable_debug=False, log_level="WARNING")
    assert logger_mod._STDOUT_LEVEL == "WARNING"
