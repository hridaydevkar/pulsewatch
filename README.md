# uptime-monitor

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
- Automatic incident creation when a service crosses its failure threshold,
  and automatic resolution on recovery
- Discord webhook alerts on both down and recovery events
- Live dashboard (`/`) showing current status, 24h uptime %, and incident
  history
- JSON API (`/api/status`, `/api/services/{id}/history`) for scripting or
  integration elsewhere

## Running it locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000

## Configuring services

Edit `config.yaml`:

```yaml
services:
  - name: "My API"
    url: "https://api.example.com/health"
    check_interval_seconds: 30
    failure_threshold: 3

discord_webhook_url: "https://discord.com/api/webhooks/..."
```

`failure_threshold` is how many consecutive failed checks are needed before
an incident opens — this avoids firing an alert on a single network blip.

To get a Discord webhook URL: Server Settings → Integrations → Webhooks →
New Webhook → Copy URL. Leave it blank to just log alerts to the console
instead.

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
app/
├── main.py       # FastAPI routes, dashboard rendering, startup wiring
├── monitor.py    # scheduler: pings services, manages incident state
├── models.py     # Service / PingLog / Incident tables
├── database.py   # SQLite engine + session
├── alerts.py     # Discord webhook sending
└── static/
    └── dashboard.html
```

## Possible extensions

- Email/SMS alerting in addition to Discord
- Multi-region checks (ping from more than one location)
- Public-facing read-only status page (auth-gated admin view)
- Response time graphing over time, not just uptime %
- Postgres support for multi-instance deployments
