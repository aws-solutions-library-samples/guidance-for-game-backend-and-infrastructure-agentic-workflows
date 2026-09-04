#!/usr/bin/env python3
"""Deploy prompts to Bedrock Prompt Management.

Creates or updates 4 managed prompts (orchestrator, gamelift, eks, cost).
Writes prompt ARNs to backend/.env.local for runtime consumption.
Idempotent: re-runs publish only when text, model, or inference settings change.
"""

import argparse
import os
import sys

# Add backend/src to path so we can import prompt definitions
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend", "src"))

import boto3
from agents.optimized_prompts import GAMELIFT_PROMPT, EKS_PROMPT, COST_PROMPT, ORCHESTRATOR_PROMPT
from config.settings import INFERENCE_CONFIG

# Prompt definitions keyed by env-var name
PROMPTS = {
    "GBAW_ORCHESTRATOR_PROMPT_ARN": ORCHESTRATOR_PROMPT,
    "GBAW_GAMELIFT_PROMPT_ARN": GAMELIFT_PROMPT,
    "GBAW_EKS_PROMPT_ARN": EKS_PROMPT,
    "GBAW_COST_PROMPT_ARN": COST_PROMPT,
}

PREFIX = "game-agent"


def _prompt_resource_name(vp):
    return f"{PREFIX}-{vp.name}"


def _list_prompt_summaries(client, **parameters):
    """Yield prompt summaries using the API's explicit nextToken contract."""
    next_token = None
    while True:
        request = dict(parameters)
        if next_token:
            request["nextToken"] = next_token
        response = client.list_prompts(**request)
        yield from response.get("promptSummaries", [])
        next_token = response.get("nextToken")
        if not next_token:
            break


def _find_existing(client, name):
    """Return prompt ID if a prompt with this name exists, else None."""
    for summary in _list_prompt_summaries(client):
        if summary["name"] == name:
            return summary["id"]
    return None


def _normalize_temperature(value):
    """Normalize Bedrock float32 temperatures for stable comparisons."""
    if value is None:
        return None
    return round(float(value), 6)


def _variant_signature(variant):
    """Return deployment-relevant fields for idempotency comparisons."""
    text_config = variant.get("templateConfiguration", {}).get("text", {})
    inference_text = variant.get("inferenceConfiguration", {}).get("text", {})
    return {
        "name": variant.get("name"),
        "modelId": variant.get("modelId"),
        "templateType": variant.get("templateType"),
        "temperature": _normalize_temperature(inference_text.get("temperature")),
        "text": text_config.get("text", ""),
    }


def _latest_published_version(client, prompt_id):
    """Return the highest numeric published version summary, if one exists."""
    published = []
    for summary in _list_prompt_summaries(client, promptIdentifier=prompt_id):
        version = summary.get("version")
        if version and version != "DRAFT":
            try:
                published.append((int(version), summary))
            except (TypeError, ValueError):
                continue
    return max(published, key=lambda item: item[0])[1] if published else None


def deploy_prompt(client, env_key, vp):
    """Create or update a single managed prompt. Returns the version ARN."""
    name = _prompt_resource_name(vp)
    agent_key = vp.name.replace("_specialist", "")
    inf = INFERENCE_CONFIG[agent_key]

    variant = {
        "name": "default",
        "modelId": inf["model_id"],
        "templateType": "TEXT",
        "inferenceConfiguration": {"text": {"temperature": inf.get("temperature", 0.1)}},
        "templateConfiguration": {"text": {"text": vp.text}},
    }

    existing_id = _find_existing(client, name)

    if existing_id:
        # Compare every field that affects the published prompt variant. Model-
        # or inference-only changes must publish a new version too.
        current = client.get_prompt(promptIdentifier=existing_id)
        current_variant = next(iter(current.get("variants", [])), {})

        if _variant_signature(current_variant) == _variant_signature(variant):
            # Unchanged — return the latest published version across all pages.
            latest_version = _latest_published_version(client, existing_id)
            if latest_version:
                print(f"  ✅ {name} unchanged (version {latest_version['version']})")
                return latest_version["arn"]
            # No published version yet — create one
            print(f"  📌 {name} exists but no version, creating...")
        else:
            print(f"  🔄 {name} configuration changed, updating...")
            client.update_prompt(
                promptIdentifier=existing_id,
                name=name,
                description=f"Game Agent {vp.name} prompt v{vp.version}",
                variants=[variant],
            )

        # Create new version from draft
        resp = client.create_prompt_version(promptIdentifier=existing_id)
        print(f"  ✅ {name} version {resp['version']} created")
        return resp["arn"]

    else:
        # Create new prompt
        print(f"  🆕 Creating {name}...")
        resp = client.create_prompt(
            name=name,
            description=f"Game Agent {vp.name} prompt v{vp.version}",
            variants=[variant],
        )
        prompt_id = resp["id"]

        # Publish version
        ver_resp = client.create_prompt_version(promptIdentifier=prompt_id)
        print(f"  ✅ {name} created (version {ver_resp['version']})")
        return ver_resp["arn"]


def update_env_file(env_file, updates):
    """Write/update prompt ARN entries in .env.local."""
    lines = []
    if os.path.exists(env_file):
        with open(env_file, "r") as f:
            lines = f.readlines()

    # Remove existing prompt ARN lines
    lines = [l for l in lines if not any(l.startswith(k + "=") for k in updates)]

    # Append new values
    if lines and not lines[-1].endswith("\n"):
        lines.append("\n")
    for key, val in updates.items():
        lines.append(f"{key}={val}\n")

    with open(env_file, "w") as f:
        f.writelines(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()

    client = boto3.client("bedrock-agent", region_name=args.region)

    arns = {}
    for env_key, vp in PROMPTS.items():
        arn = deploy_prompt(client, env_key, vp)
        arns[env_key] = arn

    update_env_file(args.env_file, arns)
    print(f"\n📋 Prompt ARNs written to {args.env_file}")
    for k, v in arns.items():
        print(f"   {k}={v}")


if __name__ == "__main__":
    main()
