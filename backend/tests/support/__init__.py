"""Shared test support for the backend test suite.

This package holds reusable test doubles and helpers that multiple test modules
import. It is importable as a top-level package because ``pytest.ini`` places both
``src`` and ``tests`` on ``pythonpath`` (e.g. ``from support.fake_provider import
FakeProvider``).
"""
