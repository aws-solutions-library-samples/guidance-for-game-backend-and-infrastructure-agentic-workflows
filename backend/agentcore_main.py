#!/usr/bin/env python3
"""
AgentCore entrypoint wrapper.
Delegates to the actual implementation in src/agentcore_main.py
"""

# Standard library
import importlib.util
import os
import sys

# Add src to path for imports
src_path = os.path.join(os.path.dirname(__file__), "src")
sys.path.insert(0, src_path)

# Load the actual module from src directory
spec = importlib.util.spec_from_file_location("src_agentcore_main", os.path.join(src_path, "agentcore_main.py"))
src_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(src_module)

# Export the app for opentelemetry-instrument
app = src_module.app

if __name__ == "__main__":
    # Delegate to the shared server entrypoint so the bind decision (loopback
    # for local, all-interfaces only inside the hosted container) is identical
    # to backend/src/agentcore_main.py and covered by one set of tests (#470).
    src_module.run_server()
