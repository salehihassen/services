# Docker observability stack

This is a separate Compose project for Grafana, Loki, and Alloy. It deliberately
does not merge lifecycle or state into `/opt/docker-compose.yaml`.

## Local endpoints

- Grafana: `127.0.0.1:13000`
- Loki: `127.0.0.1:3100`
- Alloy UI: `127.0.0.1:12345`

The ports are loopback-only. Caddy can expose Grafana over the tailnet later;
remote Alloy agents should reach Loki through a protected tailnet endpoint.

## Retention and disk behavior

Loki keeps 14 days of logs. This is a time retention rule, not a hard byte cap.
On the host root filesystem, a warning/stop guard should be added before Loki
approaches 50 GiB. A future dedicated 50 GiB logical volume provides the cleanest
hard physical ceiling.

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
