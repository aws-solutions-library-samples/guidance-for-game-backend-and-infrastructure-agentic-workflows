#!/usr/bin/env python3
"""
Performance Timing Benchmark

Measures response times for various query types against the deployed agent.
Used to compare before/after performance optimization.
"""

# Standard library
import json
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Dict

# Third-party packages
import boto3

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Local modules
from conftest import get_deployment_info

# Substring the AgentCore boundary emits when it rejects a request because it
# cannot establish a trusted cost-report actor (#365). A benchmark run that trips
# this is measuring an auth rejection, not agent latency, so it must never be
# counted as a successful timing sample.
_IDENTITY_REJECTION_MARKER = "identity verification"


class IdentityVerificationRejected(RuntimeError):
    """The deployed agent rejected the invocation at the identity boundary."""


def _is_identity_rejection(response_text: str) -> bool:
    """Return True if the response is the boundary's identity-verification refusal."""
    return _IDENTITY_REJECTION_MARKER in response_text.casefold()


# Test queries categorized by type
BENCHMARK_QUERIES = {
    "single_agent_gamelift": [
        "What GameLift fleets do I have?",
        "Show me the status of my game servers",
    ],
    "single_agent_eks": [
        "List my EKS clusters",
        "What Kubernetes clusters are running?",
    ],
    "single_agent_cost": [
        "What's my current AWS spending?",
        "Show me my cost breakdown by service",
    ],
    "multi_agent": [
        "Give me a complete infrastructure status report covering GameLift, EKS, and costs",
        "Analyze my entire AWS gaming infrastructure and provide cost optimization recommendations",
    ],
}


def invoke_agent(runtime_arn: str, region: str, query: str) -> tuple[str, float]:
    """
    Invoke the deployed agent and return response with timing.

    Shared cost-report reuse requires a trusted transport actor (#365): supply a
    synthetic ``runtimeUserId`` and a matching body ``user_context`` (user_id and
    session_id) so the AgentCore boundary sees one consistent identity and does
    not reject the request. A per-invocation unique actor also isolates memory so
    a poisoned session cannot cascade across benchmark queries.

    Returns:
        tuple: (response_text, elapsed_seconds)

    Raises:
        IdentityVerificationRejected: if the deployed agent refuses the request
            at the identity boundary. The caller must not record this as a
            timing sample.
    """
    client = boto3.client("bedrock-agentcore", region_name=region)

    # Synthetic, non-real identifiers. The runtimeUserId (trusted transport
    # identity) and the body user_id MUST match, or the boundary rejects the call.
    unique = uuid.uuid4().hex
    actor_id = f"benchmark-{unique}"
    payload = json.dumps(
        {
            "prompt": query,
            "user_context": {
                "user_id": actor_id,
                "session_id": f"benchmark-session-{unique}",
            },
        }
    )
    payload_bytes = payload.encode("utf-8")

    start_time = time.perf_counter()

    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        contentType="application/json",
        payload=payload_bytes,
        runtimeUserId=actor_id,
    )

    response_text = response["response"].read().decode("utf-8")

    end_time = time.perf_counter()
    elapsed = end_time - start_time

    # Parse response
    try:
        parsed = json.loads(response_text)
        if isinstance(parsed, str):
            response_text = parsed
    except Exception:
        pass

    # An identity-boundary rejection is not a latency measurement — surface it so
    # the caller records an error instead of a bogus "successful" timing.
    if _is_identity_rejection(response_text):
        raise IdentityVerificationRejected(
            "Deployed agent rejected the benchmark invocation at the identity boundary; " "not a valid timing sample."
        )

    return response_text, elapsed


def run_benchmark(num_iterations: int = 1) -> Dict:
    """
    Run the full benchmark suite.

    Args:
        num_iterations: Number of times to run each query (for averaging)

    Returns:
        Dict with timing results
    """
    info = get_deployment_info()

    if not info:
        print("ERROR: No deployment found. Deploy the application first.")
        sys.exit(1)

    print(f"Running benchmark against: {info['runtime_id']}")
    print(f"Region: {info['region']}")
    print(f"Iterations per query: {num_iterations}")
    print("=" * 60)

    results = {
        "timestamp": datetime.utcnow().isoformat(),
        "runtime_id": info["runtime_id"],
        "region": info["region"],
        "iterations": num_iterations,
        "queries": {},
        "summary": {},
    }

    all_timings = []

    for category, queries in BENCHMARK_QUERIES.items():
        print(f"\n{category.upper()}")
        print("-" * 40)

        category_timings = []

        for query in queries:
            query_timings = []

            for i in range(num_iterations):
                print(f"  Query: {query[:50]}... ", end="", flush=True)

                try:
                    response, elapsed = invoke_agent(info["runtime_arn"], info["region"], query)

                    query_timings.append(elapsed)
                    print(f"{elapsed:.2f}s ({len(response)} chars)")

                except Exception as e:
                    print(f"ERROR: {e}")
                    query_timings.append(None)

            # Calculate stats for this query
            valid_timings = [t for t in query_timings if t is not None]
            if valid_timings:
                avg_time = sum(valid_timings) / len(valid_timings)
                min_time = min(valid_timings)
                max_time = max(valid_timings)
            else:
                avg_time = min_time = max_time = None

            results["queries"][query] = {
                "category": category,
                "timings": query_timings,
                "avg_seconds": avg_time,
                "min_seconds": min_time,
                "max_seconds": max_time,
            }

            if valid_timings:
                category_timings.extend(valid_timings)
                all_timings.extend(valid_timings)

        # Category summary
        if category_timings:
            results["summary"][category] = {
                "avg_seconds": sum(category_timings) / len(category_timings),
                "min_seconds": min(category_timings),
                "max_seconds": max(category_timings),
                "total_queries": len(category_timings),
            }

    # Overall summary
    if all_timings:
        results["summary"]["overall"] = {
            "avg_seconds": sum(all_timings) / len(all_timings),
            "min_seconds": min(all_timings),
            "max_seconds": max(all_timings),
            "total_queries": len(all_timings),
        }

    return results


def print_summary(results: Dict):
    """Print a formatted summary of results."""
    print("\n" + "=" * 60)
    print("BENCHMARK SUMMARY")
    print("=" * 60)

    for category, stats in results["summary"].items():
        if stats:
            print(f"\n{category.upper()}:")
            print(f"  Average: {stats['avg_seconds']:.2f}s")
            print(f"  Min:     {stats['min_seconds']:.2f}s")
            print(f"  Max:     {stats['max_seconds']:.2f}s")
            print(f"  Queries: {stats['total_queries']}")


def save_results(results: Dict, filename: str):
    """Save results to JSON file."""
    filepath = os.path.join(os.path.dirname(__file__), filename)
    with open(filepath, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {filepath}")
    return filepath


if __name__ == "__main__":
    # Standard library
    import argparse

    parser = argparse.ArgumentParser(description="Run performance benchmark")
    parser.add_argument("--iterations", "-n", type=int, default=1, help="Number of iterations per query (default: 1)")
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="baseline_timings.json",
        help="Output filename (default: baseline_timings.json)",
    )

    args = parser.parse_args()

    results = run_benchmark(num_iterations=args.iterations)
    print_summary(results)
    save_results(results, args.output)
