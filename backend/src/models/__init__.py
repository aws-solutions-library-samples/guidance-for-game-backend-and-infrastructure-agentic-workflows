"""
Models module for the backend.

This module provides model configurations for the system.
"""

# Local modules
from models.cached_bedrock import create_cached_bedrock_model

__all__ = ["create_cached_bedrock_model"]
