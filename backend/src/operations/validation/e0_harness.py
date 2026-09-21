"""Reproducible, read-only measurement harness for the E0 latency spike (#412).

This is a *disposable* command-line harness. It measures the intended E1
synchronous GameLift observation — exactly three bounded, **read-only** GameLift
provider reads for one fleet, plus representative persistence and canonical
(RFC 8785) serialization — repeatedly, and emits a public-safe evidence JSON
document (latency percentiles, failure/timeout/partial-denial counts, and
declared assumptions).

It performs only ``describe``/``list`` GameLift calls. It never creates,
modifies, or deletes any AWS resource, and it never enables operations, deploys
infrastructure, or grants provider write permissions.

The three bounded reads mirror the GameLift specialist's read surface for a
single fleet (``agents.gamelift_specialist``):

* ``describe_fleet_utilization``
* ``describe_fleet_capacity``
* ``describe_scaling_policies``

Public-safe output contract: the emitted document carries latency statistics,
run configuration (region, sample size, concurrency, arrival model, retry mode),
and the declared budget only. It NEVER carries account identifiers, fleet
identifiers, ARNs, or provider payloads. The measured fleet is referenced by a
stable, non-reversible short hash so repeated runs are comparable without
disclosing the resource name.

Concurrency arrival model
-------------------------

The harness runs a **closed-loop** load: ``concurrency`` worker threads each
issue observations back-to-back, a new one starting only when the previous
returns. This is a bounded-concurrency, zero-think-time model, not an open
(Poisson-arrival) one. A closed-loop p99 measures completion latency under a
fixed number of in-flight requests; it is a conservative, reproducible stand-in
for the single-request synchronous path and is declared in the evidence so the
percentile has a defensible meaning. It is *not* a model of production arrival
rate. This assumption is recorded in ``assumptions.arrival_model``.

Acceptance
----------

Synchronous acceptance is **strict**: the run is accepted only if the whole
sample is clean (zero failures, zero timeouts, zero partial denials) *and* the
measured p99 is at or below the acceptance ceiling. A single non-success sample
denies acceptance regardless of the p99 over the successful subset.

Usage (read-only; requires a classic fleet id to observe)::

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
EXIT_ERROR = 1
EXIT_NO_REPRESENTATIVE_TARGET = 3

# The only fleet compute type that supports describe_fleet_utilization /
# describe_fleet_capacity / describe_scaling_policies. Container fleets
# (ComputeType == "CONTAINER") reject describe_fleet_utilization.
CLASSIC_COMPUTE_TYPE = "EC2"

# The single documented arrival model this harness measures under.
ARRIVAL_MODEL = "closed-loop"


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
    arrival_model: str = ARRIVAL_MODEL


def _build_reads(client: Any, fleet_id: str):
    """Return the three zero-argument, read-only GameLift reads for one fleet."""

    def read_utilization() -> Any:
        return client.describe_fleet_utilization(FleetIds=[fleet_id])

    def read_capacity() -> Any:
        return client.describe_fleet_capacity(FleetIds=[fleet_id])

    def read_scaling_policies() -> Any:
        return client.describe_scaling_policies(FleetId=fleet_id)

    return [read_utilization, read_capacity, read_scaling_policies]


def _paginate_fleet_ids(client: Any) -> list[str]:
    """Return every fleet id from ``list_fleets``, paging through all results.

    ``list_fleets`` is paginated; a single call can silently truncate an account
    with many fleets. This returns the union of *all* pages (classic and
    container ids are both present here — they are separated by
    :func:`_classic_fleet_ids`).
    """
    fleet_ids: list[str] = []
    for page in client.get_paginator("list_fleets").paginate():
        fleet_ids.extend(fid for fid in page.get("FleetIds", []) if isinstance(fid, str))
    return fleet_ids


def _container_fleet_ids(client: Any) -> set[str]:
    """Return the set of container fleet ids (read-only), paged through fully."""
    container_ids: set[str] = set()
    for page in client.get_paginator("list_container_fleets").paginate():
        for fleet in page.get("ContainerFleets", []):
            fid = fleet.get("FleetId")
            if isinstance(fid, str):
                container_ids.add(fid)
    return container_ids


def _classic_fleet_ids(client: Any) -> list[str]:
    """Return only classic (EC2) fleet ids, paginated and container-excluded.

    ``list_fleets`` returns both classic and container fleet ids. Classic fleets
    are identified by ``ComputeType == "EC2"`` on ``describe_fleet_attributes``;
    container fleets (``ComputeType == "CONTAINER"``) are additionally excluded
    by set difference against ``list_container_fleets`` as a defensive
    cross-check. All calls are read-only. ``describe_fleet_attributes`` accepts
    at most 100 fleet ids per call, so ids are chunked.
    """
    all_ids = _paginate_fleet_ids(client)
    if not all_ids:
        return []
    container_ids = _container_fleet_ids(client)
    candidate_ids = [fid for fid in all_ids if fid not in container_ids]
    if not candidate_ids:
        return []

    classic: list[str] = []
    for start in range(0, len(candidate_ids), 100):
        chunk = candidate_ids[start : start + 100]
        try:
            resp = client.describe_fleet_attributes(FleetIds=chunk)
        except Exception:  # noqa: BLE001 - fall back to per-id probing below
            resp = {"FleetAttributes": []}
            for fid in chunk:
                try:
                    single = client.describe_fleet_attributes(FleetIds=[fid])
                except Exception:  # noqa: BLE001 - skip ids that cannot be described
                    continue
                resp["FleetAttributes"].extend(single.get("FleetAttributes", []))
        for attributes in resp.get("FleetAttributes", []):
            if attributes.get("ComputeType") == CLASSIC_COMPUTE_TYPE:
                fid = attributes.get("FleetId")
                if isinstance(fid, str):
                    classic.append(fid)
    return classic


def _run_samples(one_run, samples: int, concurrency: int) -> list[tuple[str, float]]:
    """Run ``one_run`` ``samples`` times under the closed-loop arrival model.

    Concurrency is bounded by a thread pool of ``concurrency`` workers; each
    worker pulls the next sample as soon as its previous one returns (closed
    loop, zero think time). Results are collected as they complete. The runner
    itself enforces every per-request deadline in wall-clock time, so no sample
    can block the pool past its budget; the harness therefore cannot wait past
    the deadline for the batch to finish.
    """
    if concurrency <= 1:
        return [one_run(i) for i in range(samples)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="e0-sample") as pool:
        return list(pool.map(one_run, range(samples)))


def _measure(
    client: Any,
    fleet_id: str,
    budget: LatencyBudget,
    config: _RunConfig,
) -> dict[str, Any]:
    """Run the observation ``samples`` times and return a public-safe document."""
    runner = ObservationRunner(budget=budget)

    def one_run(_: int) -> tuple[str, float]:
        reads = _build_reads(client, fleet_id)
        try:
            outcome = runner.run(reads)
            return ("ok", outcome.total_s)
        except DeadlineExceededError:
            return ("timeout", 0.0)
        except PartialObservationError:
            return ("partial_denial", 0.0)
        except Exception:  # noqa: BLE001 - any other provider failure is a failed sample
            return ("failure", 0.0)

    outcomes = _run_samples(one_run, config.samples, config.concurrency)

    successes: list[float] = []
    failures = 0
    timeouts = 0
    partial_denials = 0
    for kind, value in outcomes:
        if kind == "ok":
            successes.append(value)
        elif kind == "timeout":
            timeouts += 1
        elif kind == "partial_denial":
            partial_denials += 1
        else:
            failures += 1

    summary = summarize_latencies(
        successes,
        failures=failures,
        timeouts=timeouts,
        partial_denials=partial_denials,
    )
    p99_ms = summary.p99_ms
    acceptance_ceiling_ms = budget.acceptance_ceiling_s * 1000.0
    # Strict acceptance: the whole sample must be clean AND the p99 must pass.
    clean_run = summary.clean_run
    passes = clean_run and p99_ms <= acceptance_ceiling_ms

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
            "arrival_model": config.arrival_model,
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
            "rule": "clean_run AND p99 <= gateway_integration_timeout - cancellation_margin",
            "acceptance_ceiling_ms": acceptance_ceiling_ms,
            "p99_ms": p99_ms,
            "clean_run": clean_run,
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


def _build_client(profile: str | None, region: str, retry_mode: str, max_attempts: int) -> Any:
    """Construct a read-only GameLift client with bounded socket + retry behavior.

    Bounding botocore's connect/read timeouts and total attempts is what keeps a
    stuck TCP connection or a slow socket from consuming the whole request
    budget in a way the runner's thread-level deadline cannot observe until the
    call returns. The connect/read timeouts sit below the per-read budget so a
    dead socket surfaces as a fast error rather than a silent long wait.
    """
    # Third-party packages
    import boto3
    from botocore.config import Config as BotocoreConfig

    per_read_s = DEFAULT_BUDGET.per_read_s
    botocore_config = BotocoreConfig(
        connect_timeout=min(2.0, per_read_s / 2.0),
        read_timeout=per_read_s,
        retries={"mode": retry_mode, "max_attempts": max_attempts},
    )
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("gamelift", config=botocore_config)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    retry_mode = "adaptive"
    max_attempts = 3
    client = _build_client(args.profile, args.region, retry_mode, max_attempts)

    if not args.fleet_id:
        if not _classic_fleet_ids(client):
            sys.stderr.write(
                "No classic GameLift fleet available in the target account/region. "
                "Representative live measurement of the three fleet reads "
                "(describe_fleet_utilization, describe_fleet_capacity, "
                "describe_scaling_policies) cannot be completed without one. "
                "Container fleets are excluded because describe_fleet_utilization "
                "rejects them. Remaining live step: run this harness against a "
                "classic (EC2 compute type) fleet id with --fleet-id in a "
                "demo/non-production account.\n"
            )
            return EXIT_NO_REPRESENTATIVE_TARGET
        sys.stderr.write("--fleet-id is required (pick a classic fleet from list-fleets).\n")
        return EXIT_ERROR

    config = _RunConfig(
        region=args.region,
        samples=args.samples,
        concurrency=args.concurrency,
        retry_mode=retry_mode,
        max_attempts=max_attempts,
        arrival_model=ARRIVAL_MODEL,
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
