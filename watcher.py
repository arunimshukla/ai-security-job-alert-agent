#!/usr/bin/env python3
"""Low-latency job watcher for public Ashby, Greenhouse, Lever and HTML boards."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_STATE = ROOT / ".state" / "seen.json"
DEFAULT_ALERTS = ROOT / "alerts.md"
USER_AGENT = "Arunim-AI-Security-Job-Watcher/1.0 (+private personal monitor)"


@dataclass
class Job:
    key: str
    company: str
    source: str
    title: str
    location: str
    description: str
    url: str
    posted_at: str = ""
    company_priority: int = 0
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    eligible: bool = True


class LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._href = urllib.parse.urljoin(self.base_url, href)
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            text = clean_text(" ".join(self._parts))
            self.links.append((self._href, text))
            self._href = None
            self._parts = []


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_json(url: str) -> Any:
    return json.loads(fetch_text(url))


def fetch_text(url: str, attempts: int = 3) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"},
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def stable_key(source: str, company: str, raw_id: str, url: str) -> str:
    identity = raw_id or url
    return f"{source}:{company}:{hashlib.sha256(identity.encode()).hexdigest()[:20]}"


def fetch_ashby(target: dict[str, Any]) -> list[Job]:
    board = target["board"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board)}?includeCompensation=true"
    payload = fetch_json(url)
    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        if not item.get("isListed", True):
            continue
        job_url = item.get("applyUrl") or item.get("jobUrl") or ""
        compensation = (item.get("compensation") or {}).get("compensationTierSummary", "")
        location = item.get("location", "")
        if item.get("secondaryLocations"):
            extra = [x.get("location", "") for x in item["secondaryLocations"]]
            location = ", ".join(x for x in [location, *extra] if x)
        jobs.append(
            Job(
                key=stable_key("ashby", target["name"], item.get("id", ""), job_url),
                company=target["name"],
                source="Ashby",
                title=clean_text(item.get("title")),
                location=clean_text(location),
                description=clean_text(f"{item.get('descriptionPlain', '')} {compensation}"),
                url=job_url,
                posted_at=item.get("publishedAt", ""),
                company_priority=int(target.get("priority", 0)),
            )
        )
    return jobs


def fetch_greenhouse(target: dict[str, Any]) -> list[Job]:
    board = target["board"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}/jobs?content=true"
    payload = fetch_json(url)
    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        job_url = item.get("absolute_url", "")
        jobs.append(
            Job(
                key=stable_key("greenhouse", target["name"], str(item.get("id", "")), job_url),
                company=target["name"],
                source="Greenhouse",
                title=clean_text(item.get("title")),
                location=clean_text((item.get("location") or {}).get("name")),
                description=clean_text(item.get("content")),
                url=job_url,
                posted_at=item.get("updated_at", ""),
                company_priority=int(target.get("priority", 0)),
            )
        )
    return jobs


def fetch_lever(target: dict[str, Any]) -> list[Job]:
    board = target["board"]
    region = "api.eu.lever.co" if target.get("region") == "eu" else "api.lever.co"
    url = f"https://{region}/v0/postings/{urllib.parse.quote(board)}?mode=json"
    payload = fetch_json(url)
    jobs: list[Job] = []
    for item in payload:
        categories = item.get("categories") or {}
        locations = categories.get("allLocations") or [categories.get("location", "")]
        job_url = item.get("applyUrl") or item.get("hostedUrl") or ""
        salary = item.get("salaryDescriptionPlain", "")
        jobs.append(
            Job(
                key=stable_key("lever", target["name"], item.get("id", ""), job_url),
                company=target["name"],
                source="Lever",
                title=clean_text(item.get("text")),
                location=clean_text(", ".join(x for x in locations if x)),
                description=clean_text(f"{item.get('descriptionPlain', '')} {salary}"),
                url=job_url,
                posted_at=str(item.get("createdAt", "")),
                company_priority=int(target.get("priority", 0)),
            )
        )
    return jobs


def fetch_html_board(target: dict[str, Any]) -> list[Job]:
    page_url = target["url"]
    parser = LinkParser(page_url)
    parser.feed(fetch_text(page_url))
    href_pattern = re.compile(target.get("include_href_regex", r"(?i)(jobs?|careers?|positions?|openings?)"))
    text_pattern = re.compile(target.get("include_text_regex", r"(?i).+"))
    exclude_pattern = re.compile(target.get("exclude_href_regex", r"(?i)(#|javascript:|mailto:)$"))
    seen_urls: set[str] = set()
    jobs: list[Job] = []
    for job_url, title in parser.links:
        normalized_url = job_url.split("#", 1)[0]
        if normalized_url in seen_urls:
            continue
        if not href_pattern.search(normalized_url) or exclude_pattern.search(normalized_url):
            continue
        if not title or not text_pattern.search(title):
            continue
        if normalized_url.rstrip("/") == page_url.rstrip("/"):
            continue
        seen_urls.add(normalized_url)
        jobs.append(
            Job(
                key=stable_key("html", target["name"], "", normalized_url),
                company=target["name"],
                source="Career page",
                title=title,
                location="See listing",
                description="",
                url=normalized_url,
                company_priority=int(target.get("priority", 0)),
            )
        )
    return jobs


FETCHERS = {
    "ashby": fetch_ashby,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "html": fetch_html_board,
}


def phrase_hits(text: str, weighted_phrases: dict[str, int]) -> list[tuple[str, int]]:
    lowered = text.lower()
    return [(phrase, points) for phrase, points in weighted_phrases.items() if phrase.lower() in lowered]


def score_job(job: Job, rules: dict[str, Any]) -> Job:
    title = job.title.lower()
    combined = f"{job.title} {job.location} {job.description}".lower()
    score = job.company_priority
    reasons: list[str] = []
    cautions: list[str] = []

    role_hits = phrase_hits(title, rules["role_phrases"])
    if role_hits:
        best_role = max(role_hits, key=lambda item: item[1])
        score += best_role[1]
        reasons.append(f"Role match: {best_role[0]}")
    else:
        score -= 35
        cautions.append("No preferred role phrase in the title")

    skill_hits = phrase_hits(combined, rules["skill_phrases"])
    skill_points = min(sum(points for _, points in skill_hits), 25)
    score += skill_points
    if skill_hits:
        reasons.append("Relevant scope: " + ", ".join(phrase for phrase, _ in skill_hits[:4]))

    location_text = f"{job.location} {job.description[:2500]}".lower()
    explicit_restrictions = [p for p in rules["blocked_location_phrases"] if p.lower() in location_text]
    allowed_hits = [p for p in rules["allowed_location_phrases"] if p.lower() in location_text]
    india_hits = [p for p in rules["india_location_phrases"] if p.lower() in location_text]

    if india_hits:
        score += 25
        reasons.append("India/Delhi-NCR location match")
    elif allowed_hits and not explicit_restrictions:
        score += 20
        reasons.append("Worldwide or geography-flexible remote language")
    elif explicit_restrictions:
        score -= 45
        cautions.append("Explicit location restriction: " + ", ".join(explicit_restrictions[:2]))
    elif "remote" in location_text:
        score += 5
        cautions.append("Remote role, but India eligibility is unclear")
    else:
        cautions.append("Location eligibility is unclear")

    # Job-family negatives are title-scoped. A PMM description may legitimately
    # mention the security engineers or account executives it supports.
    negative_hits = phrase_hits(title, rules["negative_phrases"])
    if negative_hits:
        penalty = min(sum(points for _, points in negative_hits), 50)
        score -= penalty
        cautions.append("Possible mismatch: " + ", ".join(phrase for phrase, _ in negative_hits[:4]))

    if any(phrase.lower() in combined for phrase in rules["management_phrases"]):
        score -= 15
        cautions.append("May include people-management responsibility")

    job.score = max(0, min(100, score))
    job.reasons = reasons
    job.cautions = cautions
    job.eligible = job.score >= int(rules.get("minimum_score", 60))
    return job


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect_jobs(config: dict[str, Any]) -> tuple[list[Job], list[str]]:
    jobs: list[Job] = []
    errors: list[str] = []
    targets = [target for target in config["targets"] if target.get("enabled", True)]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(targets)))) as pool:
        futures = {pool.submit(FETCHERS[target["type"]], target): target for target in targets}
        for future in as_completed(futures):
            target = futures[future]
            try:
                jobs.extend(future.result())
            except Exception as exc:  # keep other sources alive
                errors.append(f"{target['name']} ({target['type']}): {exc}")
    return jobs, errors


def choose_proof_points(job: Job) -> list[str]:
    private_points = [
        point.strip()
        for point in os.getenv("PROFILE_PROOF_POINTS", "").split("||")
        if point.strip()
    ]
    if private_points:
        return private_points[:3]
    text = f"{job.title} {job.description}".lower()
    points = ["Lead with the closest evidence-backed technical marketing result"]
    if any(x in text for x in ["product marketing", "positioning", "launch", "sales enablement"]):
        points.append("Use a positioning, launch, release-note or customer-documentation example")
    if any(x in text for x in ["security", "cyber", "developer", "technical", "infrastructure", "ai"]):
        points.append("Use a reproducible security or open-source contribution as technical proof")
    if any(x in text for x in ["social", "content", "brand", "editorial", "communications"]):
        points.append("Use a quantified organic distribution or founder-brand result")
    return points[:3]


def render_alerts(jobs: list[Job], errors: list[str], detected_at: str) -> str:
    lines = [f"# {len(jobs)} high-signal job alert(s)", "", f"Detected: {detected_at}", ""]
    for job in sorted(jobs, key=lambda item: item.score, reverse=True):
        lines += [
            f"## {job.score}/100 — {job.company}: {job.title}",
            "",
            f"- **Location:** {job.location or 'Not stated'}",
            f"- **Source:** {job.source}",
            f"- **Published/updated:** {job.posted_at or 'Not supplied by ATS'}",
            f"- **Apply:** {job.url}",
        ]
        if job.reasons:
            lines.append("- **Why it matched:** " + "; ".join(job.reasons))
        if job.cautions:
            lines.append("- **Check before applying:** " + "; ".join(job.cautions))
        lines += ["", "**Lead with:**"]
        lines += [f"- {point}" for point in choose_proof_points(job)]
        lines.append("")
    if errors:
        lines += ["---", "", "Source errors (other sources continued normally):"]
        lines += [f"- {error}" for error in errors]
    return "\n".join(lines).strip() + "\n"


def markdown_to_html(markdown: str) -> str:
    escaped = html.escape(markdown)
    escaped = re.sub(r"^# (.+)$", r"<h1>\1</h1>", escaped, flags=re.MULTILINE)
    escaped = re.sub(r"^## (.+)$", r"<h2>\1</h2>", escaped, flags=re.MULTILINE)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">Apply / open listing</a>', escaped)
    escaped = re.sub(r"^- (.+)$", r"<li>\1</li>", escaped, flags=re.MULTILINE)
    return f"<html><body style='font-family:Arial,sans-serif;line-height:1.5;max-width:760px'>{escaped.replace(chr(10), '<br>')}</body></html>"


def send_email(subject: str, markdown: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    recipient = os.getenv("ALERT_EMAIL_TO", "")
    sender = os.getenv("ALERT_EMAIL_FROM", username)
    if not all([username, password, recipient, sender]):
        raise RuntimeError("Email secrets are incomplete; configure SMTP_USERNAME, SMTP_PASSWORD and ALERT_EMAIL_TO")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(markdown)
    message.add_alternative(markdown_to_html(markdown), subtype="html")
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.send_message(message)


def write_github_output(name: str, value: str) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--alerts", type=Path, default=DEFAULT_ALERTS)
    parser.add_argument("--test-alert", action="store_true")
    parser.add_argument("--no-email", action="store_true")
    args = parser.parse_args(argv)

    config = load_json(args.config, {})
    state = load_json(args.state, {"version": 1, "initialized": False, "seen": {}})
    detected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.test_alert:
        jobs = [
            Job(
                key="test:synthetic",
                company="Test Company",
                source="Synthetic test",
                title="Technical Product Marketing Manager — AI Security",
                location="Remote — Worldwide",
                description="Product positioning, technical content and cybersecurity",
                url="https://example.com/test-job",
                posted_at=detected_at,
                score=95,
                reasons=["Test alert confirms the pipeline is working"],
                cautions=["Synthetic listing — do not apply"],
            )
        ]
        errors: list[str] = []
        is_baseline = False
    else:
        all_jobs, errors = collect_jobs(config)
        for job in all_jobs:
            score_job(job, config["rules"])
        is_baseline = not state.get("initialized", False)
        new_jobs = [job for job in all_jobs if job.key not in state.get("seen", {})]
        jobs = [] if is_baseline else [job for job in new_jobs if job.eligible]
        for job in all_jobs:
            if job.key not in state.setdefault("seen", {}):
                state["seen"][job.key] = {
                    "company": job.company,
                    "title": job.title,
                    "url": job.url,
                    "first_seen_at": detected_at,
                }
        state["initialized"] = True

    alert_markdown = render_alerts(jobs, errors, detected_at)
    args.alerts.write_text(alert_markdown, encoding="utf-8")
    email_sent = False
    email_error = ""
    if jobs and not args.no_email:
        try:
            top = max(jobs, key=lambda item: item.score)
            send_email(f"JOB ALERT: {top.score}/100 {top.company} — {top.title}", alert_markdown)
            email_sent = True
        except Exception as exc:
            email_error = str(exc)
            print(f"Email delivery failed: {email_error}", file=sys.stderr)

    # Never consume a newly matched job unless its email was delivered. This
    # keeps the job pending for the next five-minute retry if SMTP is down or
    # the repository secrets have not been configured yet.
    if not args.test_alert and (is_baseline or not jobs or args.no_email or email_sent):
        save_json(args.state, state)

    summary = {
        "baseline": is_baseline,
        "alerts": len(jobs),
        "email_sent": email_sent,
        "email_error": email_error,
        "source_errors": errors,
    }
    print(json.dumps(summary, indent=2))
    write_github_output("alert_count", str(len(jobs)))
    write_github_output("email_sent", str(email_sent).lower())
    write_github_output("baseline", str(is_baseline).lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
