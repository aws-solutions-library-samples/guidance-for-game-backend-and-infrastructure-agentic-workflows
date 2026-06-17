"""
Deployment Detection Fixture

Re-exports shared deployment detection from conftest.py for backward compatibility.
"""

# Standard library
import os
import sys

# Add tests directory to path for conftest import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Local modules
from conftest import get_deployment_info

# Re-export deployment_info fixture - pytest will find it in conftest.py automatically
__all__ = ["get_deployment_info"]
