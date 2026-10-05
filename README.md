# Services

Git repo tracking compose stack for my 24/7 mini pc server at home hosting some services I depend on. 

## Serving stack

Docker Compose, Caddy, Porkbun DNS, and Tailscale to host the services on one machine and access it with other machines in my tailscale network.

## Local CI validation

Run the same checks as the GitHub Actions `validate` job without using Actions
minutes:

```bash
bash scripts/validate-stack.sh
```

Requires Bash, Python 3, Docker, and Docker Compose **v5.5.1**, which is also
pinned in the workflow. Older Compose versions can still read required service
`env_file` paths even when `config --no-env-resolution` is used. The script checks
the version before validating so local and CI behavior match.

For just the fast Compose checks:

```bash
bash scripts/validate-stack.sh compose
```

Individual checks are `caddy-build`, `caddy`, `alloy`, and `loki`; run
`caddy-build` before `caddy`. The full command builds the CI Caddy image and runs
disposable validation containers. Alloy and Loki receive only their configuration
files, without the live stack's data directories or Docker socket. No services
are started or restarted. Image downloads and the Caddy build need network access;
the validation containers run with networking disabled.

These commands cover the workflow's configuration-validation job. The separate
Gitleaks Git-history scan remains a separate GitHub Actions job.

The Caddy check also checks the adapted route configuration: reverse proxies
must use Tailscale listeners, Loki must retain its authenticated push/readiness
boundary, and direct CPA management paths must return 403 before proxying.
Redirect-only listeners are reported separately for review.

## CPAMP management access

CPA Manager Plus is available through Caddy at the `CPAMP_DOMAIN` HTTPS
hostname from Tailscale devices, using its existing security-key login.
Caddy binds the route to `CADDY_BIND_ADDRESSES` and proxies to
`CPAMP_MANAGEMENT_IP:18317`; CPAMP has no host-published port. Its allowed origins
include this HTTPS origin and the existing localhost SSH tunnels. DNS A/AAAA
records point to c3's Tailscale addresses, with Porkbun DNS-01 certificates.

Provider OAuth callbacks still use the existing SSH forwards. See the private
`/opt/cpa/docs/README.md` runbook for the callback ports and key location.

## Claude usage exporter

`scripts/export-claude-usage.py` writes the Claude quota sample used by Home
Assistant. Configure `CLAUDE_USAGE_CREDENTIALS_FILE`, `CLAUDE_USAGE_OUTPUT`, and
`CLAUDE_USAGE_STATE_FILE` in the systemd user service using the paths in
`scripts/usage-export.env.example`. Keep the state file in a private host directory;
if its path is omitted, it defaults to `.usage-export-state.json` beside the
credential file. State and lock files use mode `0600`.

The `claude-usage-export.timer` uses `OnUnitInactiveSec=10min` so the next run
starts ten minutes after the previous run completes. The exporter also
enforces a ten-minute minimum interval in persisted state and locks against
overlapping runs. On rate limits or transient failures, cooldowns double from
10 minutes through 20, 40, 80, and 160 minutes to a three-hour cap, with up to
60 seconds of positive jitter within that cap. A longer `Retry-After` always
takes priority; both seconds and HTTP-date headers are supported. Cooldowns
survive service and timer restarts, including when the access token changes.

Configure `CLAUDE_USAGE_EXECUTABLE` with the absolute path to Claude Code to
enable automatic renewal of expired tokens. Once the persisted cooldown has
elapsed, the exporter invokes `claude auth status` once, with a 45-second timeout,
closed stdin, discarded output, and nonessential CLI traffic disabled. Claude Code
owns the OAuth refresh and credential writes. Renewal uses the directory of
`CLAUDE_USAGE_CREDENTIALS_FILE`, which must be named `.credentials.json`.
The installed Claude Code 2.1.286 refresh behavior was verified with fake tokens
against a local mock server; the command's exit status alone does not prove renewal.

The exporter re-reads credentials and fetches quota only after the expired token
has changed to an unexpired one. Failed or timed-out renewal uses the same
10-minute-to-three-hour backoff, persisted before launching the CLI so restarts
cannot bypass it. Existing usage cooldowns, including `Retry-After`, prevent both
renewal and quota requests. Without `CLAUDE_USAGE_EXECUTABLE`, expired tokens wait
for external renewal. If login is revoked, sign in again with Claude Code's `/login`.

A 401 blocks further quota requests with the same token until it changes or
expires and becomes eligible for renewal. The only immediate quota retry is when
re-reading the credential file finds a different, unexpired token, and the server
hasn't requested a wait. Unexpired tokens never trigger the renewal command.

Failures preserve the last successful quota sample and its original timestamp.
Home Assistant still marks it unavailable after 30 minutes. The service logs the
failure category and next permitted attempt. Handled failures exit normally so
systemd doesn't add another retry loop. Invalid state fails without a network
request instead of silently discarding a cooldown.

Run the regression checks without making network requests:

```bash
python3 -m unittest discover -s scripts -p 'test_export_claude_usage.py'
```

## Forgejo CI

`docker compose up -d` starts Forgejo, the `c3` runner, and its dedicated Docker
daemon along with the rest of the stack. No profile flag is needed.

Forgejo is initialized against Postgres with the web installer disabled. Its
configuration and generated secrets live in
`/opt/forgejo/data/custom/conf/app.ini`. Registration is disabled; an administrator
account must be created with `docker compose exec forgejo forgejo admin user create`.

The runner's persistent files are:

- `/opt/forgejo-runner/data/config.yml`: runner configuration, job labels, TLS
  Docker connection, and cache address.
- `/opt/forgejo-runner/data/.runner`: registered runner identity and credentials.
- `/opt/forgejo-runner/secrets/runner-token`: initial registration token.
- `/opt/forgejo-runner/certs`: TLS certificates generated by the CI Docker daemon.
- `/opt/forgejo-runner/docker`: CI images and container data.

The runner data and secret files are owned by UID 1000. Token and registration
files have mode `600`; the secret directory has mode `700`. These runtime files
are excluded from Git and must be included in private backups.

`FORGEJO_CI_IP`, `FORGEJO_RUNNER_IP`, and `FORGEJO_DOCKER_IP` in `/opt/.env`
reserve addresses on `FORGEJO_CI_SUBNET`. The runner registers against Forgejo's
internal HTTP address on port 3000 so CI does not depend on access to the host's
Tailscale HTTPS listener. If these addresses change, update `config.yml` and the
instance address in `.runner` too. The browser URL remains the configured HTTPS
URL.

Jobs may use `runs-on: ubuntu-latest` (Node 22 on Debian Bookworm) or
`runs-on: docker` (Docker CLI). Job containers receive the dedicated daemon's
TLS client certificates and connection settings. They do not mount the host
Docker socket.

For a fresh restore, restore the private runtime files before starting the full
stack. A missing `.runner` requires one-time runner registration; merely creating
a token file does not register the runner. See the [Forgejo runner installation
documentation](https://forgejo.org/docs/latest/admin/actions/installation/docker/).

## CondenseIt experiment

`condenseit` serves the digest reader at the `NEWS_DOMAIN` host through
Caddy on c3's Tailscale addresses. Its local image is built from upstream
v2.8.0 in `/home/saleh/repos/condenseit`, with an experimental Hacker News
collector change: new HN cards open the discussion and include sampled comments
along with the article excerpt in the summary input. The detail panel offers
separate original-article and HN-comment links; older HN entries were matched
to their discussion pages in SQLite. Saved summaries from earlier runs are
unchanged. The app runs with SQLite; the database, generated encryption
key, and digests live in
`/opt/condenseit/data`. The app configuration is `/opt/condenseit/config.yaml`,
and its login password, session secret, and dedicated AI proxy key are mode-600
files in `/opt/condenseit/secrets`. The password can be read locally with
`cat /opt/condenseit/secrets/auth-password`.

CondenseIt's configuration, data, and secrets under `/opt/condenseit` are included
in c3's nightly encrypted Restic backup. SQLite is exported with the online backup
API and checked before upload; restore the staged `condenseit.db` to its original
data directory alongside the remaining files. Jobsmith's configuration, browser
sessions, resumes, secrets, and data under `/opt/jobsmith` are also included, with
an online SQLite export of `data/jobsmith.db`. Live database files and WAL/SHM
sidecars are excluded in favor of these consistent exports. The normal
backup also includes `~/repos` and the tracked Compose and Caddy configuration.
The built-in digest scheduler is disabled;
add sources and trigger the first digest from the web UI. Its LLM is configured
for c3's `ai-policy/public` OpenAI-compatible proxy route. To stop only this
experiment, run `docker compose stop condenseit` from `/opt`.

## Speakable

`speakable` serves a small web UI and plain-text API at
the `SPEAK_DOMAIN` host. Source lives in `/home/saleh/repos/speakable`;
runtime configuration and its dedicated CPA client key live under
`/opt/speakable`. Normal Markdown cleanup and citation removal are local. Only
tables and fenced code use the AI policy proxy's
`ai-policy/public` model (over its private Compose address), where the model
must rewrite each block for speech or drop it. The container is stateless and
publishes port 8900 on loopback for Caddy.
