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
    app.run(host="0.0.0.0", port=8080)
