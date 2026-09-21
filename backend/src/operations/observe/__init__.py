"""Synchronous, read-only E1 operations observation entrypoint package.

This package holds the AWS Lambda entrypoint for the optional, default-disabled
operations observation control plane (GitHub issue #413). The frozen handler is
``operations.observe.lambda_entry.handler``. The observation business logic
(three bounded GameLift reads, transactional persistence, canonical
serialization) is delivered by issue #413 core; this entrypoint is the stable
seam the infrastructure packages and invokes, and it fails closed until the core
implementation is wired in.
"""
