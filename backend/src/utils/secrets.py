"""
Secrets management utility.

Implements strong secrets management (BSC34) with support for:
- AWS Secrets Manager integration
- Environment variable fallback
- Secret caching with TTL
- Audit logging of secret access

Usage:
    from utils.secrets import get_secret

    # Get a secret (tries Secrets Manager first, falls back to env var)
    api_key = get_secret("MY_API_KEY")

    # Get from Secrets Manager specifically
    db_password = get_secret("prod/game-agent/database", source="secretsmanager")
"""

from __future__ import annotations

# Standard library
import os
import time
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Third-party packages
    from mypy_boto3_secretsmanager import SecretsManagerClient

# Cache for secrets with TTL
_secret_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes

# Lazy-loaded boto3 client
_secrets_client: SecretsManagerClient | None = None


def _get_secrets_client() -> SecretsManagerClient:
    """Get or create Secrets Manager client."""
    global _secrets_client
    if _secrets_client is None:
        # Third-party packages
        import boto3

        # Local modules
        from config.settings import AWS_REGION, BOTO3_CLIENT_CONFIG

        _secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION, config=BOTO3_CLIENT_CONFIG)
    return _secrets_client


def _get_logger():
    """Get logger instance."""
    # Local modules
    from utils.logger import logger

    return logger


def get_secret(
    secret_name: str,
    source: str = "auto",
    default: str | None = None,
    cache: bool = True,
) -> str | None:
    """
    Retrieve a secret value.

    Args:
        secret_name: Name of the secret (env var name or Secrets Manager secret name)
        source: Where to look for the secret:
            - "auto": Try Secrets Manager first, fall back to env var
            - "secretsmanager": Only use AWS Secrets Manager
            - "env": Only use environment variable
        default: Default value if secret not found
        cache: Whether to cache the secret value

    Returns:
        Secret value or default if not found
    """
    logger = _get_logger()

    # Check cache first
    if cache and secret_name in _secret_cache:
        value, timestamp = _secret_cache[secret_name]
        if time.time() - timestamp < _CACHE_TTL_SECONDS:
            logger.debug(f"Secret '{secret_name}' retrieved from cache")
            return value
        else:
            # Cache expired
            del _secret_cache[secret_name]

    value = None

    if source in ("auto", "secretsmanager"):
        value = _get_from_secrets_manager(secret_name)
        if value:
            logger.info(f"Secret '{secret_name}' retrieved from Secrets Manager")

    if value is None and source in ("auto", "env"):
        value = os.environ.get(secret_name)
        if value:
            logger.debug(f"Secret '{secret_name}' retrieved from environment")

    if value is None:
        if default is not None:
            logger.warning(f"Secret '{secret_name}' not found, using default")
            return default
        logger.warning(f"Secret '{secret_name}' not found")
        return None

    # Cache the value
    if cache:
        _secret_cache[secret_name] = (value, time.time())

    return value


def _get_from_secrets_manager(secret_name: str) -> str | None:
    """
    Retrieve a secret from AWS Secrets Manager.

    Args:
        secret_name: Name or ARN of the secret

    Returns:
        Secret value or None if not found
    """
    try:
        client = _get_secrets_client()
        response = client.get_secret_value(SecretId=secret_name)

        # Handle both string and binary secrets
        if "SecretString" in response:
            return response["SecretString"]
        elif "SecretBinary" in response:
            # Standard library
            import base64

            return base64.b64decode(response["SecretBinary"]).decode("utf-8")
        return None

    except client.exceptions.ResourceNotFoundException:
        return None
    except client.exceptions.AccessDeniedException:
        _get_logger().warning(f"Access denied to secret '{secret_name}' - check IAM permissions")
        return None
    except Exception as e:
        _get_logger().warning(f"Error retrieving secret '{secret_name}': {e}")
        return None


def clear_cache(secret_name: str | None = None) -> None:
    """
    Clear the secret cache.

    Args:
        secret_name: Specific secret to clear, or None to clear all
    """
    global _secret_cache
    if secret_name:
        _secret_cache.pop(secret_name, None)
    else:
        _secret_cache.clear()


@lru_cache(maxsize=1)
def get_secret_rotation_enabled() -> bool:
    """
    Check if secret rotation is enabled for this deployment.

    Returns:
        True if Secrets Manager is available and configured
    """
    try:
        client = _get_secrets_client()
        # Try to list secrets to verify access
        client.list_secrets(MaxResults=1)
        return True
    except Exception:
        return False


def create_secret(
    secret_name: str,
    secret_value: str,
    description: str = "",
    tags: dict[str, str] | None = None,
) -> bool:
    """
    Create a new secret in AWS Secrets Manager.

    Args:
        secret_name: Name for the secret
        secret_value: Value to store
        description: Optional description
        tags: Optional tags

    Returns:
        True if created successfully
    """
    logger = _get_logger()

    try:
        client = _get_secrets_client()

        create_params = {
            "Name": secret_name,
            "SecretString": secret_value,
            "Description": description or f"Game Agent secret: {secret_name}",
        }

        if tags:
            create_params["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]

        client.create_secret(**create_params)
        logger.info(f"Secret '{secret_name}' created successfully")
        return True

    except client.exceptions.ResourceExistsException:
        logger.warning(f"Secret '{secret_name}' already exists")
        return False
    except Exception as e:
        logger.error(f"Failed to create secret '{secret_name}': {e}")
        return False


def update_secret(secret_name: str, secret_value: str) -> bool:
    """
    Update an existing secret in AWS Secrets Manager.

    Args:
        secret_name: Name of the secret to update
        secret_value: New value

    Returns:
        True if updated successfully
    """
    logger = _get_logger()

    try:
        client = _get_secrets_client()
        client.update_secret(SecretId=secret_name, SecretString=secret_value)

        # Clear from cache
        clear_cache(secret_name)

        logger.info(f"Secret '{secret_name}' updated successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to update secret '{secret_name}': {e}")
        return False
