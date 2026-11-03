# AI Security Job Alert Agent

This private job watcher checks selected first-party career feeds every five minutes and alerts Arunim by email when a newly discovered or materially updated role matches his profile. A Netlify scheduled function provides the reliable trigger; GitHub Actions runs the scanner, tests, deduplication and email delivery.

## What it monitors

- [Ashby](https://www.ashbyhq.com/), [Greenhouse](https://www.greenhouse.com/) and [Lever](https://www.lever.co/) public job-board APIs
- Selected first-party careers pages that do not expose one of those feeds
- Technical/product/solutions marketing, technical communications, content strategy and social/founder-brand roles
- AI, cybersecurity, developer-tooling and infrastructure companies
- Explicitly worldwide-remote roles as the highest location priority, followed by India and Delhi-NCR
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

## Test the scanner

1. Open **Actions → Five-minute AI security job watcher → Run workflow**.
2. Leave `Send a synthetic test alert` off for the first run. This creates the baseline.
3. Run it again with the test option on. You should receive one synthetic email.
4. Confirm that GitHub Actions are enabled for the repository.

GitHub's native schedule remains as a temporary fallback, but GitHub documents scheduled workflows as best-effort: they can start late or be dropped. It should not be the primary trigger for a first-mover job search.

## Automatic five-minute monitoring

### Recommended architecture

Netlify runs a tiny scheduled function at minutes `02, 07, 12, ...` of every hour. The function immediately dispatches `watch.yml` through GitHub's API. It does not scan jobs, hold Gmail credentials or expose findings publicly.

This split is deliberate:

- Netlify Scheduled Functions are available on all plans and can run standard cron expressions.
- The dispatcher normally finishes in under a second, well inside Netlify's 30-second scheduled-function limit.
- Vercel Hobby is unsuitable because its cron jobs can run only once per day.
- Railway is a viable later migration for the entire scanner, but its cron service warns that execution can drift by a few minutes. A continuously running Railway worker would be more reliable, but it adds a paid service and persistent-volume administration.

### Deploy the scheduler on Netlify

1. Create a [fine-grained GitHub token](https://github.com/settings/personal-access-tokens/new?name=Netlify%20Job%20Watcher&description=Dispatches%20the%20private%20job%20watcher&target_name=arunimshukla&expires_in=366&actions=write).
2. Give it access to **only** `ai-security-job-alert-agent` and set **Repository permissions → Actions → Read and write**. No Contents, Administration or account permissions are required.
3. In [Netlify](https://app.netlify.com/start), choose **Import an existing project → GitHub**, then select this private repository and the `main` branch.
4. Keep the build settings from `netlify.toml` and deploy. The public site intentionally contains only a no-index `Not found` page; the scheduled function cannot be invoked through a public URL.
5. Open **Project configuration → Environment variables** and add `GITHUB_ACTIONS_TOKEN` with the token from step 1. Keep it secret and scoped to Functions, then trigger a new production deploy.
6. Open **Functions → dispatch-job-watch**. Confirm that it has a **Scheduled** badge and a next-run time, then click **Run now** once.
7. In GitHub, confirm that a new `Five-minute AI security job watcher` run appears and finishes successfully. Then observe two automatic Netlify-triggered runs, five minutes apart.
8. After verification, create a GitHub Actions repository variable named `EXTERNAL_SCHEDULER_ENABLED` with value `true`. This makes GitHub's unreliable native cron a disabled fallback while still allowing Netlify and manual dispatches.

The only secret Netlify needs is `GITHUB_ACTIONS_TOKEN`. Gmail credentials remain in GitHub. Never put either token or the Gmail App Password in source code, a commit, an issue or a chat message.

### Expected monthly usage

Five-minute monitoring produces 288 scans per day, or about 8,640 scans in a 30-day month. GitHub rounds each job up to a full billed minute. On the `ubuntu-slim` runner at $0.002/minute, a one-minute scan cadence represents about **$17.28/month** of compute usage before GitHub's included allowance. GitHub Free currently includes $12/month and GitHub Pro includes $18/month, so the expected overage is approximately $5.28 on Free and $0 on Pro, assuming each run remains within one billed minute.

The Netlify dispatcher itself is small: even at one second per invocation, its monthly function compute plus one production deploy is well below the Free plan's 300-credit allowance. Check both providers' usage dashboards after the first week because retries or two-minute GitHub runs increase the estimate.

## Editing the search

- `config.json` contains companies, role phrases, location rules and the score threshold.
- `.state/seen.json` is generated automatically and prevents repeat alerts.
- `alerts.md` contains the latest run's alert summary.

To add a company, add an entry under `targets` using its public ATS board name. The watcher supports [Ashby](https://www.ashbyhq.com/) (`ashby`), [Greenhouse](https://www.greenhouse.com/) (`greenhouse`), [Lever](https://www.lever.co/) (`lever`) and first-party career pages (`html`).

The current first-party watchlist includes:

- [Arcanna.ai](https://arcanna.ai/)
- [Harmonic Security](https://www.harmonic.security/)
- [Sola Security](https://sola.security/)
- [Dropzone AI](https://www.dropzone.ai/)
- [Chainguard](https://www.chainguard.dev/)
- [Abnormal Security](https://abnormal.ai/)
- [SentinelOne](https://www.sentinelone.com/)
- [OpenAI](https://openai.com/)
- [Anthropic](https://www.anthropic.com/)
- [CloudSEK](https://www.cloudsek.com/)
- [JumpCloud](https://jumpcloud.com/)
- [Cyara](https://cyara.com/)
- [Kantiv](https://www.kantiv.com/)
- [OpenZeppelin](https://www.openzeppelin.com/)
- [Cloudflare](https://www.cloudflare.com/)
- [Canonical](https://canonical.com/)
- [Prophet Security](https://www.prophetsecurity.ai/)
- [HiddenLayer](https://www.hiddenlayer.com/)
- [Wiz](https://www.wiz.io/)
- [ElevenLabs](https://elevenlabs.io/)

Arcanna.ai is configured as verified worldwide-remote from its first-party careers policy. Its Technical Product Marketing Manager page is content-fingerprinted so a material page edit can trigger a new alert even when the title and URL do not change.

## Delivery resilience

If a matching job is found but SMTP delivery fails, it remains pending for the next run. A dry run also leaves matches pending. The workflow fails visibly when email delivery or any monitored source fails, instead of silently claiming full coverage.

Unchanged listings no longer rewrite `.state/seen.json`. This prevents a five-minute scan from creating thousands of meaningless Git commits and prevents those state-only commits from consuming Netlify production-deploy credits.
