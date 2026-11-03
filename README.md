# AI Security Job Alert Agent

This GitHub Action checks selected first-party career feeds on a five-minute schedule and alerts Arunim by email when a newly discovered or materially updated role matches his profile.

## What it monitors

- Ashby, Greenhouse and Lever public job-board APIs
- Selected first-party careers pages that do not expose one of those feeds
- Technical/product/solutions marketing, technical communications, content strategy and social/founder-brand roles
- AI, cybersecurity, developer-tooling and infrastructure companies
- India, Delhi-NCR or explicitly worldwide-remote roles
- Strict exclusions for geography-restricted and people-management roles

The first run records a quiet baseline, so existing vacancies do not flood the inbox. Later runs alert only on unseen or materially changed jobs scoring at least 60/100 and passing the hard eligibility gates.

## One-time email setup

Gmail requires two-step verification before it offers App Passwords.

1. Open Google Account → Security → **2-Step Verification** → **App passwords**.
2. Create an app password named `GitHub Job Watcher`.
3. In this repository, open **Settings → Secrets and variables → Actions**.
4. Add these three repository secrets:

| Secret | Value |
|---|---|
| `SMTP_USERNAME` | Your Gmail address |
| `SMTP_PASSWORD` | The 16-character Google App Password, without spaces |
| `ALERT_EMAIL_TO` | The address where alerts should arrive |
| `PROFILE_PROOF_POINTS` | Optional: up to three private application proof points separated with `||` |

Never put the App Password in a file, issue, commit or chat message.

## Activate and test

1. Open **Actions → Five-minute AI security job watcher → Run workflow**.
2. Leave `Send a synthetic test alert` off for the first run. This creates the baseline.
3. Run it again with the test option on. You should receive one synthetic email.
4. Confirm that GitHub Actions are enabled for the repository.

GitHub schedules are best-effort. Five minutes is the shortest supported interval, but runs can start late or be dropped during heavy platform load. The cron is offset from the top of the hour to reduce that risk.

## Editing the search

- `config.json` contains companies, role phrases, location rules and the score threshold.
- `.state/seen.json` is generated automatically and prevents repeat alerts.
- `alerts.md` contains the latest run's alert summary.

To add a company, add an entry under `targets` using its public ATS board name. The watcher supports `ashby`, `greenhouse`, `lever` and `html` target types.

The current first-party watchlist includes Arcanna.ai, Harmonic Security, Sola Security, Dropzone AI, Chainguard, Abnormal Security, SentinelOne, OpenAI, Anthropic, CloudSEK, JumpCloud, Cyara, Kantiv, OpenZeppelin, Cloudflare, Canonical, Prophet Security, HiddenLayer, Wiz and ElevenLabs.

## Delivery resilience

If a matching job is found but SMTP delivery fails, it remains pending for the next run. A dry run also leaves matches pending. The workflow fails visibly when email delivery or any monitored source fails, instead of silently claiming full coverage.
