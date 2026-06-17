#!/bin/bash
# Convenience wrapper for scripts/infrastructure/check-deployment.sh
exec scripts/infrastructure/check-deployment.sh status "$@"
