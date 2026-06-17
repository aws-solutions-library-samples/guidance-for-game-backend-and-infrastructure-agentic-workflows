#!/usr/bin/env python3
"""
Deployment configuration loader for Game Agent.
Replaces the bash YAML parser with Python settings system.
"""

import sys
import os
import pathlib

# Get project root and add backend source to path
PROJECT_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))

# Simple environment variable loading without external dependencies
def load_env_file(env_file_path):
    """Load environment variables from .env file."""
    if not os.path.exists(env_file_path):
        return

    with open(env_file_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                # Remove quotes if present
                value = value.strip('"').strip("'")
                os.environ[key] = value

# Load .env.local if it exists (development mode)
env_file = PROJECT_ROOT / "ui" / ".env.local"
if env_file.exists():
    load_env_file(str(env_file))
    IS_DEVELOPMENT = True
else:
    IS_DEVELOPMENT = False

# Configuration values with defaults
PROJECT_NAME = os.getenv("GBAW_PROJECT_NAME", "game-agent")
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
BEDROCK_MODEL_ID = os.getenv("GBAW_BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")
AGENTCORE_CPU = os.getenv("GBAW_AGENTCORE_CPU", "1024")
AGENTCORE_MEMORY = os.getenv("GBAW_AGENTCORE_MEMORY", "2048")
FRONTEND_CPU = os.getenv("GBAW_FRONTEND_CPU", "1024")
FRONTEND_MEMORY = os.getenv("GBAW_FRONTEND_MEMORY", "2048")
FRONTEND_PORT = os.getenv("GBAW_FRONTEND_PORT", "3000")

# Development features
ENABLE_DEBUG_LOGGING = IS_DEVELOPMENT and os.getenv("GBAW_ENABLE_DEBUG_LOGGING", "true").lower() == "true"
SKIP_AUTH_IN_DEV = IS_DEVELOPMENT and os.getenv("NEXT_PUBLIC_SKIP_AUTH", "false").lower() == "true"

# Export as shell variables
print(f"export PROJECT_NAME='{PROJECT_NAME}'")
print(f"export AWS_REGION='{AWS_REGION}'")
print(f"export BEDROCK_MODEL_ID='{BEDROCK_MODEL_ID}'")
print(f"export AGENTCORE_CPU='{AGENTCORE_CPU}'")
print(f"export AGENTCORE_MEMORY='{AGENTCORE_MEMORY}'")
print(f"export FRONTEND_CPU='{FRONTEND_CPU}'")
print(f"export FRONTEND_MEMORY='{FRONTEND_MEMORY}'")
print(f"export FRONTEND_PORT='{FRONTEND_PORT}'")
print(f"export IS_DEVELOPMENT={'true' if IS_DEVELOPMENT else 'false'}")
print(f"export ENABLE_DEBUG_LOGGING={'true' if ENABLE_DEBUG_LOGGING else 'false'}")
print(f"export SKIP_AUTH_IN_DEV={'true' if SKIP_AUTH_IN_DEV else 'false'}")

# Log configuration (to stderr so it doesn't interfere with shell exports)
print(f"echo '✅ Configuration loaded:'", file=sys.stderr)
print(f"echo '   Project: {PROJECT_NAME}'", file=sys.stderr)
print(f"echo '   Region: {AWS_REGION}'", file=sys.stderr)
print(f"echo '   Model: {BEDROCK_MODEL_ID}'", file=sys.stderr)
print(f"echo '   AgentCore: {AGENTCORE_CPU}/{AGENTCORE_MEMORY}'", file=sys.stderr)
print(f"echo '   Frontend: {FRONTEND_CPU}/{FRONTEND_MEMORY}'", file=sys.stderr)
print(f"echo '   Environment: {'Development' if IS_DEVELOPMENT else 'Production'}'", file=sys.stderr)
