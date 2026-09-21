"""Reproducible, read-only measurement harness for the E0 latency spike (#412).

This is a *disposable* command-line harness. It measures the intended E1
synchronous GameLift observation — exactly three bounded, **read-only** GameLift
provider reads for one fleet, plus representative persistence and canonical
(RFC 8785) serialization — repeatedly, and emits a public-safe evidence JSON
document (latency percentiles, failure/timeout counts, and declared
assumptions).

It performs only ``describe``/``list`` GameLift calls. It never creates,
modifies, or deletes any AWS resource, and it never enables operations, deploys
infrastructure, or grants provider write permissions.

The three bounded reads mirror the GameLift specialist's read surface for a
single fleet (``agents.gamelift_specialist``):

* ``describe_fleet_utilization``
* ``describe_fleet_capacity``
* ``describe_scaling_policies``

Public-safe output contract: the emitted document carries latency statistics,
run configuration (region, sample size, concurrency, retry mode), and the
declared budget only. It NEVER carries account identifiers, fleet identifiers,
ARNs, or provider payloads. The measured fleet is referenced by a stable,
non-reversible short hash so repeated runs are comparable without disclosing the
resource name.

Usage (read-only; requires a fleet id to observe)::

    python -m operations.validation.e0_harness \\
        --profile demo --region us-west-2 \\
        --fleet-id <classic-fleet-id> --samples 200 --concurrency 4 \\
        --out docs/evidence/e0-latency-<date>.json

If no classic fleet is available in the target account, the harness cannot
produce representative live evidence for the three reads. It exits with a
distinct, non-zero status and a public-safe message naming the exact remaining
live step, rather than fabricating a measurement.
"""

from __future__ import annotations

# Standard library
import argparse
import concurrent.futures
import datetime as _dt
import hashlib
import json
import sys
from dataclasses import dataclass
from typing import Any

# Local modules
from operations.validation.e0_latency import (
    DEFAULT_BUDGET,
    DeadlineExceededError,
    LatencyBudget,
    ObservationRunner,
    PartialObservationError,
    summarize_latencies,
)

EXIT_OK = 0
EXIT_NO_REPRESENTATIVE_TARGET = 3
EXIT_ERROR = 1


def _short_target_ref(fleet_id: str) -> str:
    """Return a stable, non-reversible short reference for the measured fleet.

    Public-safe: a truncated SHA-256 digest cannot be reversed to the fleet id
    but lets a reader confirm that two runs measured the same target.
    """
    return "fleet-" + hashlib.sha256(fleet_id.encode("utf-8")).hexdigest()[:12]


@dataclass
class _RunConfig:
    region: str
    samples: int
    concurrency: int
    retry_mode: str
    max_attempts: int


def _build_reads(client: Any, fleet_id: str):
    """Return the three zero-argument, read-only GameLift reads for one fleet."""

    def read_utilization() -> Any:
        return client.describe_fleet_utilization(FleetIds=[fleet_id])

    def read_capacity() -> Any:
        return client.describe_fleet_capacity(FleetIds=[fleet_id])

    def read_scaling_policies() -> Any:
        return client.describe_scaling_policies(FleetId=fleet_id)

    return [read_utilization, read_capacity, read_scaling_policies]


def _has_classic_fleet(client: Any) -> bool:
    """Return True if at least one classic fleet exists (read-only check)."""
    fleet_ids = client.list_fleets().get("FleetIds", [])
    return bool(fleet_ids)


def _measure(
    client: Any,
    fleet_id: str,
    budget: LatencyBudget,
    config: _RunConfig,
) -> dict[str, Any]:
    """Run the observation ``samples`` times and return a public-safe document."""
    runner = ObservationRunner(budget=budget)
    successes: list[float] = []
    failures = 0
    timeouts = 0

    def one_run(_: int) -> tuple[str, float]:
        reads = _build_reads(client, fleet_id)
        try:
            outcome = runner.run(reads)
            return ("ok", outcome.total_s)
        except (DeadlineExceededError, PartialObservationError):
            return ("timeout", 0.0)
        except Exception:  # noqa: BLE001 - any provider failure is a failed sample
            return ("failure", 0.0)

    if config.concurrency > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config.concurrency) as pool:
            outcomes = list(pool.map(one_run, range(config.samples)))
    else:
        outcomes = [one_run(i) for i in range(config.samples)]

    for kind, value in outcomes:
        if kind == "ok":
            successes.append(value)
        elif kind == "timeout":
            timeouts += 1
        else:
            failures += 1

    summary = summarize_latencies(successes, failures=failures, timeouts=timeouts)
    p99_ms = summary.p99_ms
    acceptance_ceiling_ms = budget.acceptance_ceiling_s * 1000.0
    passes = summary.successes > 0 and p99_ms <= acceptance_ceiling_ms

    return {
        "spike": "e0-synchronous-observation-latency",
        "issue": 412,
        "measured_at_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "target_ref": _short_target_ref(fleet_id),
        "assumptions": {
            "region": config.region,
            "provider": "gamelift",
            "provider_reads": [
                "describe_fleet_utilization",
                "describe_fleet_capacity",
                "describe_scaling_policies",
            ],
            "read_count": budget.read_count,
            "sample_size": config.samples,
            "concurrency": config.concurrency,
            "boto3_retry_mode": config.retry_mode,
            "boto3_max_attempts": config.max_attempts,
            "persistence": "representative in-memory sink + RFC 8785 canonical serialization",
        },
        "budget_ms": {
            "gateway_integration_timeout": budget.ceiling_s * 1000.0,
            "per_read": budget.per_read_s * 1000.0,
            "persistence_and_serialization": budget.persistence_s * 1000.0,
            "cancellation_margin": budget.cancellation_margin_s * 1000.0,
            "total_request_deadline": budget.total_deadline_s * 1000.0,
            "acceptance_ceiling": acceptance_ceiling_ms,
        },
        "results_ms": summary.as_public_dict(),
        "evaluation": {
            "rule": "p99 <= gateway_integration_timeout - cancellation_margin",
            "acceptance_ceiling_ms": acceptance_ceiling_ms,
            "p99_ms": p99_ms,
            "synchronous_accepted": passes,
        },
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="E0 read-only synchronous-observation latency harness")
    parser.add_argument("--profile", default=None, help="AWS profile (read-only credentials)")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--fleet-id", default=None, help="Classic GameLift fleet id to observe (read-only)")
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--out", default=None, help="Write the evidence JSON here (default: stdout)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # Third-party import kept local so unit tests importing this module do not
    # require boto3 to be configured.
    # Third-party packages
    import boto3
    from botocore.config import Config as BotocoreConfig

    retry_mode = "adaptive"
    max_attempts = 3
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client(
        "gamelift",
        config=BotocoreConfig(retries={"mode": retry_mode, "max_attempts": max_attempts}),
    )

    if not args.fleet_id:
        if not _has_classic_fleet(client):
            sys.stderr.write(
                "No classic GameLift fleet available in the target account/region. "
                "Representative live measurement of the three fleet reads "
                "(describe_fleet_utilization, describe_fleet_capacity, "
                "describe_scaling_policies) cannot be completed without one. "
                "Remaining live step: run this harness against a classic fleet id "
                "with --fleet-id in a demo/non-production account.\n"
            )
            return EXIT_NO_REPRESENTATIVE_TARGET
        sys.stderr.write("--fleet-id is required (pick one from list-fleets).\n")
        return EXIT_ERROR

    config = _RunConfig(
        region=args.region,
        samples=args.samples,
        concurrency=args.concurrency,
        retry_mode=retry_mode,
        max_attempts=max_attempts,
    )
    document = _measure(client, args.fleet_id, DEFAULT_BUDGET, config)
    rendered = json.dumps(document, indent=2, sort_keys=True)

    if args.out:
        # Standard library
        from pathlib import Path

        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
        sys.stderr.write(f"Wrote evidence to {args.out}\n")
    else:
        sys.stdout.write(rendered + "\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
