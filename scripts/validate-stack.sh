#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
repo_root="$PWD"

# Match CI: older Compose versions still load service env files even with
# --no-env-resolution. Never load the deployment host's private env files.
compose_version="$(docker compose version --short)"
if [[ "${compose_version#v}" != "5.5.1" ]]; then
  echo "Docker Compose v5.5.1 is required to match CI (found $compose_version)." >&2
  exit 1
fi

primary_compose() {
  docker compose --env-file .env.example --project-directory . \
    --file docker-compose.yaml "$@"
}

observability_compose() {
  docker compose --env-file observability/.env.example \
    --project-directory observability --file observability/compose.yaml "$@"
}

service_image() {
  observability_compose config --format json | python3 -c \
    'import json, sys; print(json.load(sys.stdin)["services"][sys.argv[1]]["image"])' "$1"
}

validate_compose() {
  primary_compose config --quiet --no-env-resolution
  observability_compose config --quiet --no-env-resolution
}

build_caddy() {
  docker build --tag services-caddy-ci --file caddy/Dockerfile caddy
}

validate_caddy() (
  ci_secrets="$(mktemp -d)"
  trap 'rm -rf "$ci_secrets"' EXIT
  printf '%s\n' 'ci-placeholder' > "$ci_secrets/porkbun_api_key"
  printf '%s\n' 'ci-placeholder' > "$ci_secrets/porkbun_api_secret_key"
  docker run --rm --network none --read-only \
    --env-file "$repo_root/.env.example" \
    --env ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory \
    --env "CADDY_BIND_ADDRESSES=100.64.0.10 [fd7a:0000:0000::10]" \
    --mount "type=bind,src=$repo_root/caddy/Caddyfile,dst=/etc/caddy/Caddyfile,readonly" \
    --mount "type=bind,src=$ci_secrets/porkbun_api_key,dst=/run/secrets/porkbun_api_key,readonly" \
    --mount "type=bind,src=$ci_secrets/porkbun_api_secret_key,dst=/run/secrets/porkbun_api_secret_key,readonly" \
    --entrypoint caddy services-caddy-ci validate --config /etc/caddy/Caddyfile
)

validate_alloy() {
  docker run --rm --network none --read-only \
    --mount "type=bind,src=$repo_root/observability/alloy-config.alloy,dst=/etc/alloy/config.alloy,readonly" \
    "$(service_image alloy)" fmt --test /etc/alloy/config.alloy
}

validate_loki() {
  docker run --rm --network none --read-only \
    --mount "type=bind,src=$repo_root/observability/loki-config.yaml,dst=/etc/loki/config.yaml,readonly" \
    "$(service_image loki)" -verify-config=true -config.file=/etc/loki/config.yaml
}

case "${1:-all}" in
  compose) validate_compose ;;
  caddy-build) build_caddy ;;
  caddy) validate_caddy ;;
  alloy) validate_alloy ;;
  loki) validate_loki ;;
  all)
    validate_compose
    build_caddy
    validate_caddy
    validate_alloy
    validate_loki
    ;;
  *) echo "Usage: bash scripts/validate-stack.sh [all|compose|caddy-build|caddy|alloy|loki]" >&2; exit 2 ;;
esac
