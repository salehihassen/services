#!/usr/bin/env bash
set -e
: "${SYNTHETIC_USAGE_SECRETS_FILE:?SYNTHETIC_USAGE_SECRETS_FILE is missing}"
source "$SYNTHETIC_USAGE_SECRETS_FILE"
exec python3 "$(dirname "$0")/export-synthetic-usage.py"
