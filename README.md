# pulsewatch

A self-hosted uptime & status-page tool. Pings the services you configure on
their own schedule, tracks response history, opens/resolves incidents
automatically, and alerts you on Discord the moment something goes down —
then again when it recovers.

Built with FastAPI + SQLite + APScheduler. No external database or paid
service required to run it.

## Why this exists

Every team running more than a couple of services eventually needs to know
*immediately* when one goes down, not when a user complains. Tools like
UptimeRobot and Statuspage.io solve this commercially — this is a from-scratch
implementation of the same core mechanics: scheduled health checks, failure
thresholds (so one blip doesn't page you), incident tracking, and alerting.

## Features

- Configurable list of services to monitor (`config.yaml`), each with its own
  check interval and failure threshold
- Monitor upstream **dependencies** as well as your own services: AWS region
  health, database connectivity (`SELECT 1`), or any third-party status API —
  the dashboard and alerts make clear which failures are yours vs an upstream's
- Automatic incident creation when a service crosses its failure threshold,
  and automatic resolution on recovery
- Pluggable alert channels (Discord, Slack, email) on both down and recovery
  events — enable as many as you like
- Live dashboard (`/`) showing current status, 24h uptime %, a per-service
  response-time sparkline (last hour, via Chart.js), and incident history
- JSON API (`/api/status`, `/api/services/{id}/history`,
  `/api/services/{id}/response-times`) for scripting or integration elsewhere

## Installing

pulsewatch is a proper installable package. Install it in editable mode from a
clone:

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -e .
```

This puts a `pulsewatch` command on your PATH. Run it with no arguments to see
the full help menu:

```bash
pulsewatch --help
```

## The CLI

```bash
pulsewatch init                              # write a default config.yaml here
pulsewatch add --name "My API" \
               --url https://api.example.com/health \
               --interval 30 --threshold 3   # append a service, no hand-editing
pulsewatch serve                             # start the app + background monitor
```

- `pulsewatch init` creates a `config.yaml` in the current directory (use
  `--force` to overwrite an existing one).
- `pulsewatch add` appends a service to the active `config.yaml` (`--interval`
  and `--threshold` are optional and default to 30s / 3 failures). If no config
  exists yet it creates one in the current directory.
- `pulsewatch serve` replaces the old `uvicorn app.main:app` command. It accepts
  `--host`, `--port`, and `--reload`.

Then open http://127.0.0.1:8000

## Configuring services

You can hand-edit `config.yaml` or use `pulsewatch add`. pulsewatch looks for
config in this order:

1. `./config.yaml` (the current working directory)
2. `~/.pulsewatch/config.yaml` (per-user fallback)

```yaml
services:
  - name: "My API"
    url: "https://api.example.com/health"
    check_interval_seconds: 30
    failure_threshold: 3

alerts:
  - type: discord
    webhook_url: "https://discord.com/api/webhooks/..."
  - type: slack
    webhook_url: "https://hooks.slack.com/services/..."
  - type: email
    to: "oncall@example.com"
    from: "pulsewatch@example.com"     # optional; defaults to the SMTP user
```

`failure_threshold` is how many consecutive failed checks are needed before
an incident opens — this avoids firing an alert on a single network blip.

## Monitoring upstream dependencies

Every service defaults to `check_type: http` (a plain URL ping, unchanged). Set
`check_type: dependency` with a `dependency_kind` to watch an upstream provider
instead. These are tagged `DEPENDENCY` on the dashboard and alert as *"Upstream
dependency … is degraded"* rather than *"Your service is down"*, so you can tell
your own outage from a provider's at a glance.

**`aws_status`** — polls the public AWS Health feed
(`https://health.aws.amazon.com/public/currentevents`) and reports down **only**
if your region (and optionally specific services) are impacted, not on any AWS
event anywhere. `region` is required; `services` is an optional filter.

```yaml
  - name: "AWS us-east-1"
    check_type: dependency
    dependency_kind: aws_status
    region: "us-east-1"
    services: ["EC2", "S3"]     # optional
    failure_threshold: 1
```

**`database`** — runs a lightweight `SELECT 1` over a SQLAlchemy connection
string instead of an HTTP request. Install the matching driver for your DB
(e.g. `psycopg2-binary` for Postgres, `PyMySQL` for MySQL; SQLite needs none).
Credentials are never shown on the dashboard or API — the target is redacted to
`scheme://host/db`.

```yaml
  - name: "Primary Postgres"
    check_type: dependency
    dependency_kind: database
    connection_string: "postgresql://user:pass@localhost:5432/mydb"
```

**`custom_api`** — fetches a JSON endpoint and asserts a nested field equals an
expected value (dotted path), not just the HTTP status. Great for a provider's
own status API (e.g. Stripe is healthy when `status.indicator == "none"`).

```yaml
  - name: "Stripe API"
    check_type: dependency
    dependency_kind: custom_api
    url: "https://status.stripe.com/api/v2/status.json"
    json_field: "status.indicator"
    expected_value: "none"
```

To add a new dependency kind, write a checker in
[checks.py](src/pulsewatch/checks.py) returning a `CheckResult` and register it
in `DEPENDENCY_CHECKERS`.

## Alert channels

Both down and recovery events are sent to every channel in the `alerts:` list.
With no channels enabled, alerts are logged to the console — so the project
still runs and is demoable with zero setup.

| Type      | Config keys                     | Notes                                             |
|-----------|---------------------------------|---------------------------------------------------|
| `discord` | `webhook_url`                   | Server Settings → Integrations → Webhooks → New   |
| `slack`   | `webhook_url`                   | A Slack [Incoming Webhook](https://api.slack.com/messaging/webhooks) URL |
| `email`   | `to`, `from` (optional)         | SMTP server/credentials come from env vars (below) |

Email keeps secrets out of `config.yaml` by reading the SMTP connection from
the environment:

| Variable                   | Default | Purpose                              |
|----------------------------|---------|--------------------------------------|
| `PULSEWATCH_SMTP_HOST`     | —       | SMTP server (required to send email) |
| `PULSEWATCH_SMTP_PORT`     | `587`   | SMTP port                            |
| `PULSEWATCH_SMTP_USER`     | —       | Username (enables login if set)      |
| `PULSEWATCH_SMTP_PASSWORD` | —       | Password (enables login if set)      |
| `PULSEWATCH_SMTP_FROM`     | user    | Fallback From address                |
| `PULSEWATCH_SMTP_STARTTLS` | `true`  | Set `false` to disable STARTTLS      |

To add a channel of your own, subclass `AlertChannel` in
[alerts.py](src/pulsewatch/alerts.py), implement `send(message)`, and register
it in `_CHANNEL_BUILDERS`.

## Demoing the failure/recovery flow

The included `config.yaml` ships with a "Flaky Demo Service" pointed at
`https://httpbin.org/status/200,500`, which randomly returns a failing
status — so you'll see real incidents open and resolve within a few minutes
of running it, without needing to break anything yourself.

For a live demo: run the app, wait for a couple of failed checks on the
flaky service, and watch the dashboard status flip to DOWN and an incident
appear — then watch it self-resolve once a check succeeds again.

## Architecture

```
pyproject.toml            # packaging, dependencies, `pulsewatch` entry point
src/pulsewatch/
├── cli.py        # click CLI: init / serve / add
├── config.py     # config discovery (CWD → ~/.pulsewatch) + default template
├── main.py       # FastAPI routes, dashboard rendering, startup wiring
├── monitor.py    # scheduler: runs checks, manages incident state
├── checks.py     # pluggable checks (http / aws_status / database / custom_api)
├── models.py     # Service / PingLog / Incident tables
├── database.py   # SQLite engine + session
├── alerts.py     # pluggable alert channels (Discord / Slack / email)
└── static/
    └── dashboard.html
```

## Possible extensions

- SMS / PagerDuty / webhook alert channels (drop-in `AlertChannel` subclasses)
- More dependency kinds (drop-in checkers in `DEPENDENCY_CHECKERS`)
- Multi-region checks (ping from more than one location)
- Public-facing read-only status page (auth-gated admin view)
- Postgres support for multi-instance deployments
