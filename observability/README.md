# Docker observability stack

This is a separate Compose project for Grafana, Loki, and Alloy. It deliberately
does not merge lifecycle or state into `/opt/docker-compose.yaml`.

## Local endpoints

- Grafana: `127.0.0.1:13000`
- Loki: `127.0.0.1:3100`
- Alloy UI: `127.0.0.1:12345`

The ports are loopback-only. Caddy already exposes Grafana through the configured
`GRAFANA_DOMAIN`, with Grafana's login, and Loki through `LOKI_DOMAIN` on the
host's Tailscale addresses. Loki ingress requires Basic authentication and allows
only `/loki/api/v1/push` and `/ready`; other paths return 403. Add remote writers
to the existing authenticated handler, preserving its path restriction. Never
replace it with an unrestricted Loki proxy or publish port 3100 on another
interface. Reads use Grafana over the private Docker network.

## Retention and disk behavior

Loki keeps 14 days of logs. This is a time retention rule, not a hard byte cap.
The current host mounts a dedicated 20 GiB ext4 logical volume at the Loki data
directory. Its size is the physical ceiling; monitor free space independently
of retention. Recheck the actual mount with `findmnt` before changing placement.

Each container in this project also rotates its own Docker JSON logs at three
10 MiB files. Existing containers require recreation after adding equivalent
Compose `logging` settings; doing so does not delete named volumes or bind-mounted
application data.

## Security note

Alloy mounts Docker's socket read-only so it can discover containers, labels, and
their log streams. A read-only mount still exposes powerful Docker API metadata;
only trusted images/configuration should receive it. If Alloy were used solely as
an OTLP receiver for applications that actively push logs, it would not need the
socket. Here it is needed specifically for automatic collection of existing
Docker stdout/stderr logs.
