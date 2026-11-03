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
DEFAULT_HTML_ROLE_REGEX = (
    r"(?i)(technical product marketing|product marketing|technical marketing|"
    r"security marketing|solutions marketing|developer marketing|product communications|"
    r"technical communications|content marketing|content strategist|technical content|"
    r"developer advocate|developer relations|founder brand|social media|"
    r"marketing communications)"
)


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
    deadline_at: str = ""
    employment_type: str = ""
    compensation: str = ""
    allow_unclear_location: bool = False
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


class VisibleTextParser(HTMLParser):
    """Extract stable visible page text while ignoring scripts and styling."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
    PRIMARY_TAGS = {"main", "article"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._primary_depth = 0
        self._visible: list[str] = []
        self._primary: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in self.SKIP_TAGS:
            self._skip_depth += 1
        elif lowered in self.PRIMARY_TAGS and self._skip_depth == 0:
            self._primary_depth += 1

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif lowered in self.PRIMARY_TAGS and self._primary_depth:
            self._primary_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        self._visible.append(data)
        if self._primary_depth:
            self._primary.append(data)

    def text(self) -> str:
        return clean_text(" ".join(self._primary or self._visible))


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_timestamp(value: Any) -> str:
    """Return provider timestamps as readable UTC ISO-8601 strings."""
    if value in (None, ""):
        return ""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric > 10_000_000_000:
        numeric /= 1000
    return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat(timespec="seconds")


def parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def job_deadline_expired(job: Job, now: datetime | None = None) -> bool:
    deadline = parse_datetime(job.deadline_at)
    if deadline is None:
        return False
    return deadline < (now or datetime.now(timezone.utc))


def job_fingerprint(job: Job) -> str:
    """Track material listing changes without keying off provider timestamps."""
    material = "\n".join(
        clean_text(value).lower()
        for value in [
            job.title,
            job.location,
            job.description,
            job.url,
            job.deadline_at,
            job.employment_type,
            job.compensation,
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def hydrate_candidate_metadata(job: Job) -> None:
    """Fetch metadata omitted by list APIs only for new/updated candidates."""
    if job.source != "Ashby" or not job.url:
        return
    if urllib.parse.urlparse(job.url).hostname != "jobs.ashbyhq.com":
        return
    detail_url = job.url.removesuffix("/application")
    try:
        page = fetch_text(detail_url, attempts=2)
    except RuntimeError:
        return
    match = re.search(r'"applicationDeadline":"([^"]+)"', page)
    if match:
        job.deadline_at = match.group(1)


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
        workplace = item.get("workplaceType", "")
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
                description=clean_text(f"{item.get('descriptionPlain', '')} {compensation} {workplace}"),
                url=job_url,
                posted_at=item.get("publishedAt", ""),
                employment_type=clean_text(item.get("employmentType", "")),
                compensation=clean_text(compensation),
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
                posted_at=normalize_timestamp(item.get("createdAt", "")),
                employment_type=clean_text(categories.get("commitment", "")),
                compensation=clean_text(salary),
                company_priority=int(target.get("priority", 0)),
            )
        )
    return jobs


def fetch_html_board(target: dict[str, Any]) -> list[Job]:
    page_url = target["url"]
    parser = LinkParser(page_url)
    parser.feed(fetch_text(page_url))
    href_pattern = re.compile(target.get("include_href_regex", r"(?i)(jobs?|careers?|positions?|openings?)"))
    text_pattern = re.compile(target.get("include_text_regex", DEFAULT_HTML_ROLE_REGEX))
    exclude_pattern = re.compile(target.get("exclude_href_regex", r"(?i)(#|javascript:|mailto:)$"))
    hydrate_pattern = re.compile(
        target.get("hydrate_title_regex", r"(?i)(product marketing|technical communications)")
    )
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
        description = ""
        employment_type = ""
        compensation = ""
        location = clean_text(target.get("default_location", "See listing"))
        if target.get("hydrate_job_pages") and hydrate_pattern.search(title):
            detail_parser = VisibleTextParser()
            detail_parser.feed(fetch_text(normalized_url, attempts=2))
            description = detail_parser.text()
            for field_name, pattern_name in [
                ("location", "location_regex"),
                ("employment_type", "employment_type_regex"),
                ("compensation", "compensation_regex"),
            ]:
                pattern = target.get(pattern_name)
                if not pattern:
                    continue
                match = re.search(pattern, description)
                if not match:
                    continue
                value = clean_text(match.groupdict().get(field_name) or match.group(1))
                if field_name == "location":
                    location = value
                elif field_name == "employment_type":
                    employment_type = value
                else:
                    compensation = value
        jobs.append(
            Job(
                key=stable_key("html", target["name"], "", normalized_url),
                company=target["name"],
                source="Career page",
                title=title,
                location=location,
                description=description,
                url=normalized_url,
                employment_type=employment_type,
                compensation=compensation,
                company_priority=int(target.get("priority", 0)),
                allow_unclear_location=bool(target.get("allow_unclear_location", False)),
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


def compensation_below_floor(job: Job, rules: dict[str, Any]) -> str:
    """Return a hard-rejection reason only for explicit structured salary ranges."""
    text = clean_text(job.compensation)
    if not text or re.search(r"(?i)(per hour|/hour|/hr|hourly)", text):
        return ""

    thresholds = [
        (r"(?:US\$|USD|\$)", int(rules.get("minimum_usd_salary", 0)), "USD"),
        (r"(?:INR|₹)", int(rules.get("minimum_inr_salary", 0)), "INR"),
    ]
    for marker, minimum, currency in thresholds:
        if not minimum:
            continue
        amounts: list[float] = []
        range_pattern = (
            rf"(?i){marker}\s*([\d,.]+)\s*([km]?)\s*(?:-|–|—|to)\s*"
            rf"(?:{marker}\s*)?([\d,.]+)\s*([km]?)"
        )
        for low_raw, low_suffix, high_raw, high_suffix in re.findall(range_pattern, text):
            for raw, suffix in [(low_raw, low_suffix), (high_raw, high_suffix)]:
                value = float(raw.replace(",", ""))
                if suffix.lower() == "k":
                    value *= 1_000
                elif suffix.lower() == "m":
                    value *= 1_000_000
                amounts.append(value)
        single_pattern = rf"(?i){marker}\s*([\d,.]+)\s*([km]?)"
        for raw, suffix in re.findall(single_pattern, text):
            value = float(raw.replace(",", ""))
            if suffix.lower() == "k":
                value *= 1_000
            elif suffix.lower() == "m":
                value *= 1_000_000
            amounts.append(value)
        if amounts and max(amounts) < minimum:
            formatted = f"${minimum:,}" if currency == "USD" else f"₹{minimum:,}"
            return f"Explicit {currency} compensation tops out below {formatted}"
    return ""


def score_job(job: Job, rules: dict[str, Any]) -> Job:
    title = job.title.lower()
    combined = (
        f"{job.title} {job.location} {job.employment_type} "
        f"{job.compensation} {job.description}"
    ).lower()
    score = job.company_priority
    reasons: list[str] = []
    cautions: list[str] = []
    hard_excluded = False

    role_hits = phrase_hits(title, rules["role_phrases"])
    if role_hits:
        best_role = max(role_hits, key=lambda item: item[1])
        score += best_role[1]
        reasons.append(f"Role match: {best_role[0]}")
    else:
        score -= 35
        cautions.append("No preferred role phrase in the title")

    skill_hits = phrase_hits(combined, rules["skill_phrases"])
    skill_points = min(sum(points for _, points in skill_hits), 15)
    score += skill_points
    if skill_hits:
        reasons.append("Relevant scope: " + ", ".join(phrase for phrase, _ in skill_hits[:4]))

    location_value = job.location.lower()
    location_text = f"{job.location} {job.description}".lower()
    explicit_restrictions = [p for p in rules["blocked_location_phrases"] if p.lower() in location_text]
    location_restrictions = [
        p for p in rules.get("blocked_location_only_phrases", []) if p.lower() in location_value
    ]
    allowed_hits = [p for p in rules["allowed_location_phrases"] if p.lower() in location_text]
    india_hits = [p for p in rules["india_location_phrases"] if p.lower() in location_value]
    india_eligibility_hits = [
        p for p in rules.get("india_eligibility_phrases", []) if p.lower() in location_text
    ]

    if india_hits or india_eligibility_hits:
        score += 15
        reasons.append("India/Delhi-NCR location match")
    elif explicit_restrictions or location_restrictions:
        score -= 45
        restrictions = [*explicit_restrictions, *location_restrictions]
        cautions.append("Explicit location restriction: " + ", ".join(dict.fromkeys(restrictions[:3])))
        hard_excluded = True
    elif allowed_hits:
        score += 20
        reasons.append("Worldwide or geography-flexible remote language")
    elif "remote" in location_text:
        cautions.append("Remote role, but India eligibility is unclear")
        hard_excluded = hard_excluded or (
            bool(rules.get("require_explicit_india_or_worldwide", True))
            and not job.allow_unclear_location
        )
    else:
        cautions.append("Location eligibility is unclear")
        hard_excluded = hard_excluded or (
            bool(rules.get("require_explicit_india_or_worldwide", True))
            and not job.allow_unclear_location
        )

    # Job-family negatives are title-scoped. A PMM description may legitimately
    # mention the security engineers or account executives it supports.
    negative_hits = phrase_hits(title, rules["negative_phrases"])
    if negative_hits:
        penalty = min(sum(points for _, points in negative_hits), 50)
        score -= penalty
        cautions.append("Possible mismatch: " + ", ".join(phrase for phrase, _ in negative_hits[:4]))

    if any(phrase.lower() in combined for phrase in rules["management_phrases"]):
        score -= 15
        cautions.append("Explicit people-management responsibility")
        hard_excluded = hard_excluded or bool(rules.get("hard_exclude_people_management", True))

    employment_value = job.employment_type.lower()
    blocked_employment = [
        phrase
        for phrase in rules.get("blocked_employment_phrases", [])
        if phrase.lower() in employment_value or phrase.lower() in title
    ]
    if blocked_employment:
        cautions.append("Not a full-time role: " + ", ".join(blocked_employment[:2]))
        hard_excluded = True
    elif not job.employment_type:
        cautions.append("Employment type is not explicitly stated")

    salary_rejection = compensation_below_floor(job, rules)
    if salary_rejection:
        cautions.append(salary_rejection)
        hard_excluded = True
    elif not job.compensation:
        cautions.append("Compensation is not stated; verify the salary floor")

    blocked_titles = [
        phrase for phrase in rules.get("blocked_title_phrases", []) if phrase.lower() in title
    ]
    if blocked_titles:
        cautions.append("Non-IC seniority: " + ", ".join(blocked_titles[:2]))
        hard_excluded = True

    if job_deadline_expired(job):
        cautions.append(f"Application deadline passed: {job.deadline_at}")
        hard_excluded = True

    job.score = max(0, min(100, score))
    job.reasons = reasons
    job.cautions = cautions
    job.eligible = job.score >= int(rules.get("minimum_score", 60)) and not hard_excluded
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
        if job.employment_type:
            lines.append(f"- **Employment:** {job.employment_type}")
        if job.compensation:
            lines.append(f"- **Compensation:** {job.compensation}")
        if job.deadline_at:
            lines.append(f"- **Application deadline:** {job.deadline_at}")
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
    def linkify(match: re.Match[str]) -> str:
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,;:!?)]}":
            trailing = url[-1] + trailing
            url = url[:-1]
        return f'<a href="{url}">Open listing</a>{trailing}'

    escaped = re.sub(r"https?://[^\s<]+", linkify, escaped)
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
    state = load_json(args.state, {"version": 2, "initialized": False, "seen": {}})
    detected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scanned_count = 0
    new_count = 0
    updated_count = 0
    candidate_count = 0
    blocked_count = 0
    original_seen: dict[str, Any] = {}

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
        scanned_count = len(all_jobs)
        for job in all_jobs:
            score_job(job, config["rules"])
        is_baseline = not state.get("initialized", False)
        original_seen = json.loads(json.dumps(state.get("seen", {})))
        candidates: list[Job] = []
        next_seen = json.loads(json.dumps(original_seen))

        for job in all_jobs:
            previous = original_seen.get(job.key)
            fingerprint = job_fingerprint(job)
            is_new = previous is None
            is_updated = bool(previous and previous.get("fingerprint") and previous["fingerprint"] != fingerprint)
            if is_new:
                new_count += 1
            elif is_updated:
                updated_count += 1
            if not is_baseline and (is_new or is_updated):
                candidates.append(job)

            # Preserve byte-stable state for unchanged jobs. Updating a
            # timestamp on every poll would create a Git commit every five
            # minutes and cause pointless downstream deployments.
            needs_record_update = (
                is_new
                or is_updated
                or not (previous or {}).get("fingerprint")
                or (previous or {}).get("company") != job.company
            )
            if needs_record_update:
                next_seen[job.key] = {
                    **(previous or {}),
                    "company": job.company,
                    "title": job.title,
                    "url": job.url,
                    "fingerprint": fingerprint,
                    "first_seen_at": (previous or {}).get("first_seen_at", detected_at),
                    "last_seen_at": detected_at,
                }

        candidate_count = len(candidates)
        for job in candidates:
            if job.eligible:
                hydrate_candidate_metadata(job)
                score_job(job, config["rules"])
                next_seen[job.key]["fingerprint"] = job_fingerprint(job)
                if job.deadline_at:
                    next_seen[job.key]["deadline_at"] = job.deadline_at

        jobs = [] if is_baseline else [job for job in candidates if job.eligible]
        blocked_count = 0 if is_baseline else sum(not job.eligible for job in candidates)
        state["version"] = 2
        state["seen"] = next_seen
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

    # Never consume a new or materially updated match unless its email was
    # delivered. Persist unrelated state while leaving failed/dry-run alerts
    # pending for the next execution.
    if not args.test_alert:
        if jobs and not email_sent:
            for job in jobs:
                if job.key in original_seen:
                    state["seen"][job.key] = original_seen[job.key]
                else:
                    state["seen"].pop(job.key, None)
        elif email_sent:
            for job in jobs:
                state["seen"][job.key]["last_alerted_at"] = detected_at
        save_json(args.state, state)

    summary = {
        "baseline": is_baseline,
        "scanned": scanned_count,
        "new": new_count,
        "materially_updated": updated_count,
        "candidates": candidate_count,
        "blocked": blocked_count,
        "alerts": len(jobs),
        "email_sent": email_sent,
        "email_error": email_error,
        "source_errors": errors,
    }
    print(json.dumps(summary, indent=2))
    write_github_output("alert_count", str(len(jobs)))
    write_github_output("email_sent", str(email_sent).lower())
    write_github_output("baseline", str(is_baseline).lower())
    write_github_output("source_error_count", str(len(errors)))
    write_github_output("scanned_count", str(scanned_count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
