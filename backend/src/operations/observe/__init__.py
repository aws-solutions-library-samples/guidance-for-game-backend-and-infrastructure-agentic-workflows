"""Deployable read-only GameLift observation Lambda (issue #413, E1 Agent A).

This package wires the protocol-neutral :mod:`operations.observation` service to
its real runtime dependencies: a bounded ``boto3`` GameLift read-only adapter, a
DynamoDB observation store, a CloudWatch metrics sink, and the API Gateway JWT
handler. The Lambda entry point lives in :mod:`operations.observe.lambda_entry`.
"""
