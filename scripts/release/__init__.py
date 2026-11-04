"""Deterministic, provider-read-only milestone release tooling.

Standard-library only (Python 3.10+). These modules back the manually
dispatched ``.github/workflows/release.yml`` workflow. They never deploy,
never call AWS, and never create a tag or release outside the audited
publication ordering in :mod:`scripts.release.publish`.
"""
