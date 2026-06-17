#!/usr/bin/env python3
"""Deploy prompts to Bedrock Prompt Management.

Creates or updates 4 managed prompts (orchestrator, gamelift, eks, cost).
Writes prompt ARNs to backend/.env.local for runtime consumption.
Idempotent: re-runs detect existing prompts and only update if text changed.
"""

import argparse
import hashlib
import sys
import os

# Add backend/src to path so we can import prompt definitions
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend", "src"))

import boto3
from agents.optimized_prompts import GAMELIFT_PROMPT, EKS_PROMPT, COST_PROMPT, ORCHESTRATOR_PROMPT
from config.settings import BEDROCK_MODEL_ID, INFERENCE_CONFIG

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


def _find_existing(client, name):
    """Return prompt ID if a prompt with this name exists, else None."""
    paginator = client.get_paginator("list_prompts")
    for page in paginator.paginate():
        for summary in page.get("promptSummaries", []):
            if summary["name"] == name:
                return summary["id"]
    return None


def _text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def deploy_prompt(client, env_key, vp):
    """Create or update a single managed prompt. Returns the version ARN."""
    name = _prompt_resource_name(vp)
    agent_key = vp.name.replace("_specialist", "")
    inf = INFERENCE_CONFIG.get(agent_key, {"temperature": 0.1})

    variant = {
        "name": "default",
        "modelId": BEDROCK_MODEL_ID,
        "templateType": "TEXT",
        "inferenceConfiguration": {"text": {"temperature": inf.get("temperature", 0.1)}},
        "templateConfiguration": {"text": {"text": vp.text}},
    }

    existing_id = _find_existing(client, name)

    if existing_id:
        # Check if text changed
        current = client.get_prompt(promptIdentifier=existing_id)
        current_text = ""
        for v in current.get("variants", []):
            tc = v.get("templateConfiguration", {}).get("text", {})
            current_text = tc.get("text", "")
            break

        if _text_hash(current_text) == _text_hash(vp.text):
            # Unchanged — get latest version ARN
            versions = client.list_prompts(promptIdentifier=existing_id)
            for s in versions.get("promptSummaries", []):
                if s.get("version") and s["version"] != "DRAFT":
                    print(f"  ✅ {name} unchanged (version {s['version']})")
                    return s["arn"]
            # No published version yet — create one
            print(f"  📌 {name} exists but no version, creating...")
        else:
            # Text changed — update draft
            print(f"  🔄 {name} text changed, updating...")
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
