#!/usr/bin/env bash
set -e
: "${OLLAMA_USAGE_SECRETS_FILE:?OLLAMA_USAGE_SECRETS_FILE is missing}"
source "$OLLAMA_USAGE_SECRETS_FILE"
exec python3 "$(dirname "$0")/export-ollama-usage.py"
