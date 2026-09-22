"""E3 execute-phase components (issue #415).

This package holds the deployable E3 execution surface: the normalized GameLift
execution adapter (the single permitted provider write, UpdateFleetCapacity,
plus the DescribeFleetCapacity read used before and after it), the DynamoDB
execution store, the executor service, the dispatcher and executor Lambda
entries, and the execution metrics sink.
"""
