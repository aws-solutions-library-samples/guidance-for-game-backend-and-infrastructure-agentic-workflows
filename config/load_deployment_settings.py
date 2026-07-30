#!/usr/bin/env python3
"""Load deployment settings from the canonical backend configuration."""

# Standard library
import argparse
import json
import os
import pathlib
import shlex
import sys

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))


def load_env_file(env_file_path: pathlib.Path) -> None:
    """Load simple dotenv entries without overriding the process environment."""
    if not env_file_path.exists():
        return

    with env_file_path.open(encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def shell_exports(values: dict[str, str]) -> str:
    """Render shell-safe export statements."""
    return "\n".join(f"export {key}={shlex.quote(str(value))}" for key, value in values.items())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("shell", "json"), default="shell")
    parser.add_argument("--models-only", action="store_true")
    parser.add_argument("--default-models-only", action="store_true")
    args = parser.parse_args()

    env_file = PROJECT_ROOT / "ui" / ".env.local"
    load_env_file(env_file)

    # Local modules
    from config.model_settings import (  # pylint: disable=import-outside-toplevel
        DEFAULT_ORCHESTRATOR_MODEL_ID,
        DEFAULT_SPECIALIST_MODEL_ID,
        resolve_model_ids,
    )

    orchestrator_model_id, specialist_model_id = resolve_model_ids()
    resolved_models = {
        "GBAW_ORCHESTRATOR_MODEL_ID": orchestrator_model_id,
        "GBAW_SPECIALIST_MODEL_ID": specialist_model_id,
    }
    default_models = {
        "GBAW_ORCHESTRATOR_MODEL_ID": DEFAULT_ORCHESTRATOR_MODEL_ID,
        "GBAW_SPECIALIST_MODEL_ID": DEFAULT_SPECIALIST_MODEL_ID,
    }

    if args.default_models_only:
        values = default_models
    elif args.models_only:
        values = resolved_models
    else:
        values = {
            "PROJECT_NAME": os.getenv("GBAW_PROJECT_NAME", "game-agent"),
            "AWS_REGION": os.getenv("AWS_REGION", "us-west-2"),
            **resolved_models,
            "AGENTCORE_CPU": os.getenv("GBAW_AGENTCORE_CPU", "2048"),
            "AGENTCORE_MEMORY": os.getenv("GBAW_AGENTCORE_MEMORY", "4096"),
            "FRONTEND_CPU": os.getenv("GBAW_FRONTEND_CPU", "1024"),
            "FRONTEND_MEMORY": os.getenv("GBAW_FRONTEND_MEMORY", "2048"),
            "FRONTEND_PORT": os.getenv("GBAW_FRONTEND_PORT", "3000"),
            "IS_DEVELOPMENT": str(env_file.exists()).lower(),
            "ENABLE_DEBUG_LOGGING": str(
                env_file.exists() and os.getenv("GBAW_ENABLE_DEBUG_LOGGING", "true").lower() == "true"
            ).lower(),
            "SKIP_AUTH_IN_DEV": str(
                env_file.exists() and os.getenv("NEXT_PUBLIC_SKIP_AUTH", "false").lower() == "true"
            ).lower(),
        }

    if args.format == "json":
        print(json.dumps(values))
    else:
        print(shell_exports(values))

    if not args.models_only and not args.default_models_only:
        print(
            "Configuration loaded: "
            f"project={values['PROJECT_NAME']}, region={values['AWS_REGION']}, "
            f"orchestrator={orchestrator_model_id}, specialist={specialist_model_id}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
