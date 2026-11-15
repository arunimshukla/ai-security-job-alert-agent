#!/usr/bin/env python3
"""Low-latency job watcher for first-party boards and global discovery feeds."""

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
import xml.etree.ElementTree as ET
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
USER_AGENT = (
    "AI-Security-Job-Watcher/2.0 "
    "(+https://github.com/arunimshukla/ai-security-job-alert-agent)"
)
DEFAULT_HTML_ROLE_REGEX = (
    r"(?i)(technical product marketing|product marketing|technical marketing|"
    r"security marketing|solutions marketing|developer marketing|product communications|"
    r"technical communications|content marketing|content strateg(?:ist|y)|technical content|"
    r"developer advocate|developer relations|founder brand|social media|"
    r"marketing communications)"
)
DISCOVERY_WINDOW_MINUTES = 15
PUBLIC_ATS_HOSTS = {"jobs.ashbyhq.com", "jobs.lever.co", "jobs.eu.lever.co", "job-boards.greenhouse.io"}
EMPLOYMENT_LABEL = re.compile(
    r"\b(?:employment type|job type)\s*:\s*"
    r"(full[- ]time|part[- ]time|fixed[- ]term|contract(?:or)?|temporary|freelance|internship)\b",
    re.IGNORECASE,
)
CONTRACT_ROLE = re.compile(
    r"\b(?:this(?: (?:role|position|job))?|the (?:role|position|job)) is (?:an? )?"
    r"(?:full[- ]time )?(?:\w+[- ](?:months?|weeks?|years?)\s+)?"
    r"(contract(?:or)?|fixed[- ]term|temporary|freelance|part[- ]time|internship)"
    r"(?:\s+(?:role|position|job|assignment)\b|(?=[.!,;:]|$))",
    re.IGNORECASE,
)
INDIA_EXCLUSION = re.compile(
    r"\b(?:except(?: for)? india|excluding india|india (?:is )?excluded|"
    r"(?:cannot|can not|can t|do not|don t|unable to) (?:currently )?(?:hire|employ) "
    r"(?:(?:candidates|people|remotely) )?(?:in|from|based in) india|"
    r"not (?:open|available) to (?:applicants|candidates) (?:in|from|based in) india)\b"
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
    discovery: bool = False
    company_priority: int = 0
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    eligible: bool = True
    workplace_type: str = ""


FULL_TIME = re.compile(r"\bfull[ -]time\b", re.IGNORECASE)
FULL_TIME_TRACK = re.compile(r"\s*\(([^()]*\bfull[ -]time\b[^()]*)\)\s*$", re.IGNORECASE)


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


class BoardPageParser(HTMLParser):
    """Read the job records embedded in YC's public, server-rendered pages."""

    def __init__(self) -> None:
        super().__init__()
        self.props: dict[str, Any] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs).get("data-page")
        if tag == "div" and data:
            self.props = json.loads(data).get("props", {})


class JobCardParser(HTMLParser):
    """Collect one record per career card, including boards with shared forms."""

    def __init__(self, card_class: str) -> None:
        super().__init__()
        self.card_class = card_class
        self.depth = 0
        self.in_title = False
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.anchor = ""
        self.cards: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div":
            if not self.depth and self.card_class in (attributes.get("class") or "").split():
                self.parts, self.title_parts, self.anchor = [], [], ""
                self.depth = 1
            elif self.depth:
                self.depth += 1
                self.anchor = self.anchor or attributes.get("id") or ""
        if self.depth and tag == "h4":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h4":
            self.in_title = False
        if tag == "div" and self.depth:
            self.depth -= 1
            if not self.depth:
                self.cards.append((clean_text(" ".join(self.title_parts)), clean_text(" ".join(self.parts)), self.anchor))

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.parts.append(data)
            if self.in_title:
                self.title_parts.append(data)


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def location_words(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold()))


def location_has(phrase: str, text: str) -> bool:
    return f" {location_words(phrase)} " in f" {text} "


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
        page = fetch_text(detail_url, attempts=2, timeout=8)
    except RuntimeError:
        return
    match = re.search(r'"applicationDeadline"\s*:\s*(?:"([^"]*)"|(null))', page)
    if match:
        deadline = match.group(1)
        if match.group(2) or (deadline and parse_datetime(deadline)):
            job.deadline_at = deadline or ""


def enrich_discovery_listing(job: Job) -> None:
    """Recover evidence from established public ATS hosts when a feed omits it."""
    page = VisibleTextParser()
    try:
        page.feed(fetch_text(job.url.removesuffix("/application"), attempts=1, timeout=6))
    except RuntimeError:
        return
    if page.text():
        job.description = clean_text(f"{job.description} {page.text()}")


def fetch_json(url: str) -> Any:
    return json.loads(fetch_text(url))


def fetch_text(url: str, attempts: int = 3, *, timeout: int = 25) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"},
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def stable_key(source: str, company: str, raw_id: str, url: str) -> str:
    identity = raw_id or url
    return f"{source}:{company}:{hashlib.sha256(identity.encode()).hexdigest()[:20]}"


def posting_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if host == "jobs.ashbyhq.com":
        path = path.removesuffix("/application")
    elif host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        path = path.removesuffix("/apply")
    query = urllib.parse.urlencode(sorted(
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"gh_src", "lever-source"}
    ))
    fragment = "" if host in PUBLIC_ATS_HOSTS else parsed.fragment
    return urllib.parse.urlunsplit((parsed.scheme.lower(), host, path, query, fragment))


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
                workplace_type=clean_text(workplace),
            )
        )
    return jobs


def fetch_greenhouse(target: dict[str, Any]) -> list[Job]:
    board = target["board"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}/jobs?content=true"
    payload = fetch_json(url)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("Greenhouse response is missing its job list")
    jobs: list[Job] = []
    for item in payload["jobs"]:
        job_url = item.get("absolute_url", "")
        description = clean_text(item.get("content"))
        employment = CONTRACT_ROLE.search(description) or EMPLOYMENT_LABEL.search(description)
        jobs.append(
            Job(
                key=stable_key("greenhouse", target["name"], str(item.get("id", "")), job_url),
                company=target["name"],
                source="Greenhouse",
                title=clean_text(item.get("title")),
                location=clean_text((item.get("location") or {}).get("name")),
                description=description,
                url=job_url,
                posted_at=item.get("updated_at", ""),
                employment_type=employment.group(1) if employment else "",
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
        salary_range = item.get("salaryRange") or {}
        bounds = [
            f"{salary_range[key]:,}" for key in ("min", "max")
            if isinstance(salary_range.get(key), (int, float))
        ]
        if bounds and salary_range.get("currency"):
            interval = salary_range.get("interval", "")
            unit = re.search(r"\bper-(year|month|week|day|hour)\b", interval or "")
            period = f" per {unit.group(1)}" if unit else ""
            salary = f"{salary_range['currency']} {'–'.join(bounds)}{period}"
        jobs.append(
            Job(
                key=stable_key("lever", target["name"], item.get("id", ""), job_url),
                company=target["name"],
                source="Lever",
                title=clean_text(item.get("text")),
                location=clean_text(", ".join(x for x in [*locations, item.get("workplaceType", "")] if x)),
                description=clean_text(f"{item.get('descriptionPlain', '')} {salary}"),
                url=job_url,
                posted_at=normalize_timestamp(item.get("createdAt", "")),
                employment_type=clean_text(categories.get("commitment", "")),
                compensation=clean_text(salary),
                company_priority=int(target.get("priority", 0)),
                workplace_type=clean_text(item.get("workplaceType")),
            )
        )
    return jobs


def fetch_html_board(target: dict[str, Any]) -> list[Job]:
    page_url = target["url"]
    page = fetch_text(page_url)
    if target.get("card_class"):
        parser = JobCardParser(target["card_class"])
        parser.feed(page)
        if not parser.cards:
            raise ValueError("No career cards found in the public board")
        jobs = []
        for title, details, anchor in parser.cards:
            if not re.search(target.get("include_text_regex", DEFAULT_HTML_ROLE_REGEX), title):
                continue
            match = re.search(target["location_regex"], details)
            location = clean_text(match.group(1)) if match else ""
            if re.match(r"^Remote\b", details.removeprefix(title).strip(), re.IGNORECASE):
                location = f"Remote {location}".strip()
            jobs.append(Job(
                key=stable_key("html", target["name"], anchor or title, page_url),
                company=target["name"], source="Career page", title=title, location=location,
                description=details, url=f"{page_url}#{anchor}" if anchor else page_url,
                employment_type="Full-time track" if FULL_TIME_TRACK.search(title) else "",
                company_priority=int(target.get("priority", 0)),
            ))
        return jobs
    parser = LinkParser(page_url)
    parser.feed(page)
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


def discovery_due(target: dict[str, Any], now: datetime | None = None) -> bool:
    """Place slower feeds into deterministic 15-minute windows without state churn."""
    cadence = max(DISCOVERY_WINDOW_MINUTES, int(target.get("cadence_minutes", 60)))
    offset = int(target.get("offset_minutes", 2)) % cadence
    minute = int((now or datetime.now(timezone.utc)).timestamp() // 60)
    return (minute - offset) % cadence < DISCOVERY_WINDOW_MINUTES


def discovery_title_matches(target: dict[str, Any], title: str) -> bool:
    pattern = target.get("include_title_regex")
    if pattern:
        return bool(re.search(pattern, title))
    roles = target.get("role_phrases")
    if roles is not None:
        return any(phrase.casefold() in title.casefold() for phrase in roles)
    return bool(re.search(DEFAULT_HTML_ROLE_REGEX, title))


def normalize_discovery_location(value: Any, default: str = "") -> str:
    location = clean_text(value) or clean_text(default)
    lowered = location.lower().strip(" .,-")
    if lowered in {"worldwide", "global", "anywhere", "anywhere worldwide"}:
        return "Remote worldwide"
    return location


def build_discovery_job(
    target: dict[str, Any],
    *,
    raw_id: Any,
    company: Any,
    title: Any,
    location: Any,
    description: Any,
    url: Any,
    posted_at: Any = "",
    employment_type: Any = "",
    compensation: Any = "",
) -> Job | None:
    clean_title = clean_text(title)
    if not clean_title or not discovery_title_matches(target, clean_title):
        return None
    clean_url = clean_text(url)
    if not clean_url:
        return None
    source_name = clean_text(target["name"])
    clean_company = clean_text(company) or source_name
    key = stable_key("discovery", "global", "", posting_url(clean_url))
    if target.get("type") == "yc_jobs":
        key = stable_key("yc", "global", clean_text(raw_id), clean_url)
    return Job(
        key=key,
        company=clean_company,
        source=source_name,
        title=clean_title,
        location=normalize_discovery_location(location, target.get("default_location", "")),
        description=clean_text(description),
        url=clean_url,
        posted_at=normalize_timestamp(posted_at) if isinstance(posted_at, (int, float)) else clean_text(posted_at),
        employment_type=clean_text(employment_type),
        compensation=clean_text(compensation),
        company_priority=int(target.get("priority", 0)),
        discovery=True,
    )


def deduplicate_jobs(jobs: list[Job]) -> list[Job]:
    """Prefer first-party copies when multiple sources link to the same posting."""
    selected: dict[str, Job] = {}
    for job in jobs:
        identity = posting_url(job.url) or job.key
        existing = selected.get(identity)
        if (
            existing is None
            or (existing.discovery, -len(existing.description)) > (job.discovery, -len(job.description))
        ):
            selected[identity] = job
    return list(selected.values())


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def rss_values(item: ET.Element, names: set[str]) -> list[str]:
    values: list[str] = []
    lowered_names = {name.lower() for name in names}
    for child in item.iter():
        if child is item or xml_local_name(child.tag) not in lowered_names:
            continue
        value = clean_text(child.text or child.attrib.get("href", ""))
        if value and value not in values:
            values.append(value)
    return values


def split_feed_title(target: dict[str, Any], raw_title: str, company: str) -> tuple[str, str]:
    title = clean_text(raw_title)
    company = clean_text(company)
    pattern = target.get("title_company_regex")
    if not company and pattern:
        match = re.match(pattern, title)
        if match:
            company = clean_text(match.groupdict().get("company", ""))
            title = clean_text(match.groupdict().get("title", title))
    if company:
        title = re.sub(rf"(?i)^{re.escape(company)}\s*[:|—-]\s*", "", title)
        title = re.sub(rf"(?i)\s+at\s+{re.escape(company)}$", "", title)
    return title, company


def fetch_rss_market(target: dict[str, Any]) -> list[Job]:
    root = ET.fromstring(fetch_text(target["url"]))
    entries = [node for node in root.iter() if xml_local_name(node.tag) in {"item", "entry"}]
    jobs: list[Job] = []
    for item in entries:
        raw_title = next(iter(rss_values(item, {"title"})), "")
        company = next(iter(rss_values(item, {"company", "companyName", "creator"})), "")
        if not company and target.get("summary_job_metadata"):
            author = next((node for node in item if xml_local_name(node.tag) == "author"), None)
            if author is not None:
                company = next(iter(rss_values(author, {"name"})), "")
        title, company = split_feed_title(target, raw_title, company)
        links = rss_values(item, {"link"})
        descriptions = rss_values(item, {"description", "encoded", "content", "summary"})
        categories = rss_values(item, {"category"})
        locations = rss_values(
            item,
            {"location", "locationRestriction", "jobGeo", "region", "country"},
        )
        employment = rss_values(item, {"employmentType", "jobType"})
        compensation = rss_values(item, {"salary", "compensation"})
        if target.get("summary_job_metadata"):
            summary = next((node.text or "" for node in item if xml_local_name(node.tag) == "summary"), "")
            header, _, body = summary.strip().partition("\n\n")
            fields = [clean_text(value) for value in header.split("·", 2)]
            if len(fields) >= 2:
                locations = locations or [fields[0]]
                employment = employment or [fields[1]]
                compensation = compensation or fields[2:]
                descriptions = [clean_text(body)]
        published = rss_values(item, {"pubDate", "published", "updated", "date"})
        identities = rss_values(item, {"guid", "id"})
        job = build_discovery_job(
            target,
            raw_id=next(iter(identities), ""),
            company=company,
            title=title,
            location=", ".join(locations),
            description=" ".join([*descriptions, *categories]),
            url=next(iter(links), ""),
            posted_at=next(iter(published), ""),
            employment_type=", ".join(employment),
            compensation=" ".join(compensation),
        )
        if job:
            jobs.append(job)
    return jobs


def fetch_hn_jobs_market(target: dict[str, Any]) -> list[Job]:
    ids = fetch_json(target["url"])
    if not isinstance(ids, list):
        raise ValueError("Expected a list of Hacker News job story IDs")
    item_urls = [
        urllib.parse.urljoin(target["url"], f"item/{item_id}.json")
        for item_id in ids[:12]
        if isinstance(item_id, int)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(lambda url: json.loads(fetch_text(url, attempts=1)), item_urls))

    jobs: list[Job] = []
    for item in items:
        if not item or item.get("type") != "job" or item.get("dead") or item.get("deleted"):
            continue
        headline = clean_text(item.get("title"))
        match = re.fullmatch(
            r"(.+?)(?:\s+\(YC [A-Z]\d+\))?\s+is hiring\s+(?:(?:a|an)\s+)?(.+)",
            headline,
            re.IGNORECASE,
        )
        company, title = match.groups() if match else ("", headline)
        job = build_discovery_job(
            target,
            raw_id=item["id"],
            company=company,
            title=title,
            location="",
            description=f"{title} {item.get('text') or ''}",
            url=item.get("url") or f"https://news.ycombinator.com/item?id={item['id']}",
            posted_at=item.get("time", ""),
        )
        if job:
            jobs.append(job)
    return jobs


def fetch_yc_jobs_market(target: dict[str, Any]) -> list[Job]:
    board = BoardPageParser()
    board.feed(fetch_text(target["url"], attempts=1))
    records = board.props.get("jobPostings")
    if not isinstance(records, list):
        raise ValueError("YC's public job listings were missing from the page")

    jobs: list[Job] = []
    for item in records:
        path = item.get("url") or ""
        if not re.fullmatch(r"/companies/[\w-]+/jobs/[\w-]+", path):
            continue
        location = clean_text(item.get("location"))
        location = re.sub(r"\bRemote \(IN\)", "Remote India", location)
        if item.get("visa") == "US citizen/visa only":
            location = f"{location}; US only"
        job = build_discovery_job(
            target, raw_id=item.get("id"), company=item.get("companyName"),
            title=item.get("title"), location=location, description=item.get("companyOneLiner"),
            url=urllib.parse.urljoin(target["url"], path), employment_type=item.get("type"),
            compensation=item.get("salaryRange"),
        )
        if job:
            jobs.append(job)

    def enrich(job: Job) -> None:
        detail = BoardPageParser()
        try:
            detail.feed(fetch_text(job.url, attempts=1))
        except (RuntimeError, ValueError):
            return  # The board record still provides structured location and job type.
        description = clean_text(detail.props.get("job", {}).get("description"))
        if description:
            job.description = clean_text(f"{job.description} {description}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(enrich, jobs))
    return jobs


def fetch_remoteok_market(target: dict[str, Any]) -> list[Job]:
    payload = fetch_json(target["url"])
    jobs: list[Job] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or not item.get("position"):
            continue
        salary = ""
        if item.get("salary_min") or item.get("salary_max"):
            salary = f"${item.get('salary_min', '')}-${item.get('salary_max', '')}"
        job = build_discovery_job(
            target,
            raw_id=item.get("id", ""),
            company=item.get("company", ""),
            title=item.get("position", ""),
            location=item.get("location", ""),
            description=f"{item.get('description', '')} {' '.join(item.get('tags') or [])}",
            url=item.get("apply_url") or item.get("url", ""),
            posted_at=item.get("date") or item.get("epoch", ""),
            employment_type=item.get("job_type", ""),
            compensation=salary,
        )
        if job:
            jobs.append(job)
    return jobs


def fetch_jobicy_market(target: dict[str, Any]) -> list[Job]:
    payload = fetch_json(target["url"])
    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        salary = item.get("annualSalary", "")
        if not salary and (item.get("annualSalaryMin") or item.get("annualSalaryMax")):
            currency = item.get("salaryCurrency", "USD")
            salary = f"{currency} {item.get('annualSalaryMin', '')}-{item.get('annualSalaryMax', '')}"
        job = build_discovery_job(
            target,
            raw_id=item.get("id", ""),
            company=item.get("companyName", ""),
            title=item.get("jobTitle", ""),
            location=item.get("jobGeo", ""),
            description=f"{item.get('jobDescription', '')} {item.get('jobIndustry', '')}",
            url=item.get("url", ""),
            posted_at=item.get("pubDate", ""),
            employment_type=item.get("jobType", ""),
            compensation=salary,
        )
        if job:
            jobs.append(job)
    return jobs


def fetch_arbeitnow_market(target: dict[str, Any]) -> list[Job]:
    payload = fetch_json(target["url"])
    jobs: list[Job] = []
    for item in payload.get("data", []):
        if target.get("remote_only", True) and not item.get("remote"):
            continue
        job = build_discovery_job(
            target,
            raw_id=item.get("slug", ""),
            company=item.get("company_name", ""),
            title=item.get("title", ""),
            location=item.get("location", ""),
            description=f"{item.get('description', '')} {' '.join(item.get('tags') or [])}",
            url=item.get("url", ""),
            posted_at=item.get("created_at", ""),
            employment_type=", ".join(item.get("job_types") or []),
        )
        if job:
            jobs.append(job)
    return jobs


def fetch_remotive_market(target: dict[str, Any]) -> list[Job]:
    payload = fetch_json(target["url"])
    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        job = build_discovery_job(
            target,
            raw_id=item.get("id", ""),
            company=item.get("company_name", ""),
            title=item.get("title", ""),
            location=item.get("candidate_required_location", ""),
            description=f"{item.get('description', '')} {item.get('category', '')}",
            url=item.get("url", ""),
            posted_at=item.get("publication_date", ""),
            employment_type=item.get("job_type", ""),
            compensation=item.get("salary", ""),
        )
        if job:
            jobs.append(job)
    return jobs


FETCHERS = {
    "ashby": fetch_ashby,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "html": fetch_html_board,
}

DISCOVERY_FETCHERS = {
    "rss": fetch_rss_market,
    "hn_jobs": fetch_hn_jobs_market,
    "yc_jobs": fetch_yc_jobs_market,
    "remoteok": fetch_remoteok_market,
    "jobicy": fetch_jobicy_market,
    "arbeitnow": fetch_arbeitnow_market,
    "remotive": fetch_remotive_market,
}


def phrase_hits(text: str, weighted_phrases: dict[str, int]) -> list[tuple[str, int]]:
    lowered = text.lower()
    return [(phrase, points) for phrase, points in weighted_phrases.items() if phrase.lower() in lowered]


def compensation_below_floor(job: Job, rules: dict[str, Any]) -> str:
    """Compare explicit salaries with the configured annual floors."""
    text = clean_text(job.compensation)
    if not text:
        return ""

    thresholds = [
        (r"(?:US\$|\bUSD\b|(?<![A-Z])\$)", int(rules.get("minimum_usd_salary", 0)), "USD"),
        (r"(?:INR|₹)", int(rules.get("minimum_inr_salary", 0)), "INR"),
    ]
    multipliers = {
        "k": 1_000, "m": 1_000_000, "lakh": 100_000, "lakhs": 100_000, "lpa": 100_000,
        "lac": 100_000, "lacs": 100_000, "crore": 10_000_000, "crores": 10_000_000,
    }
    number = r"\d+(?:,\d+)*(?:\.\d+)?"
    unit = rf"(?:{'|'.join(multipliers)})\b"
    interval = r"month(?:ly)?|mo|week(?:ly)?|day|daily|hour(?:ly)?|hr|year(?:ly)?|annum"
    for marker, minimum, currency in thresholds:
        if not minimum:
            continue
        amounts: list[float] = []
        pattern = re.compile(
            rf"{marker}\s*({number})(?:\s*({unit}))?"
            rf"(?:\s*(?:-|–|—|\bto\b)\s*(?:{marker}\s*)?({number})(?:\s*({unit}))?)?"
            rf"(?:\s*(?:/\s*|per\s+|a\s+)?({interval})\b)?",
            re.IGNORECASE,
        )
        for low, low_unit, high, high_unit, period in pattern.findall(text):
            period = period.lower()
            if period.startswith(("hour", "hr", "day", "dai")):
                continue
            annual_factor = 12 if period.startswith("mo") else 52 if period.startswith("week") else 1
            for raw, suffix in ((low, low_unit or high_unit), (high, high_unit or low_unit)):
                if raw:
                    amounts.append(float(raw.replace(",", "")) * multipliers.get(suffix.lower(), 1) * annual_factor)
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

    if job.discovery:
        discovery_signals = [
            phrase
            for phrase in rules.get("discovery_required_phrases", [])
            if phrase.lower() in combined
        ]
        if discovery_signals:
            reasons.append("Discovery-sector match: " + ", ".join(discovery_signals[:3]))
        else:
            cautions.append("Broad-market listing lacks an AI/cybersecurity signal")
            hard_excluded = True

    location_value = location_words(job.location)
    description_location = location_words(job.description)
    location_text = f"{location_value} {description_location}"
    explicit_restrictions = [p for p in rules["blocked_location_phrases"] if location_has(p, location_value)]
    description_restrictions = [
        p for p in rules["blocked_location_phrases"] if location_has(p, description_location)
    ]
    location_restrictions = [
        p for p in rules.get("blocked_location_only_phrases", []) if location_has(p, location_value)
    ]
    worldwide_text = re.sub(
        r"\banywhere (?:in|within|across|on) (?!the world\b|the globe\b|earth\b)",
        "within ",
        location_text,
    )
    allowed_hits = [p for p in rules["allowed_location_phrases"] if location_has(p, worldwide_text)]
    worldwide_location = location_value in {"worldwide", "global", "anywhere", "anywhere worldwide"}
    india_hits = [p for p in rules["india_location_phrases"] if location_has(p, location_value)]
    india_remote_phrases = [
        p for p in rules.get("india_eligibility_phrases", []) if "work in india" not in p.casefold()
    ]
    remote_scopes = re.findall(r"\bremote\s*\(([^)]+)\)", job.location, re.IGNORECASE)
    india_scopes = [
        scope for scope in remote_scopes
        if any(location_has(p, location_words(scope)) for p in rules["india_location_phrases"])
    ]
    other_scopes = [
        scope for scope in remote_scopes
        if scope not in india_scopes and location_words(scope) not in {"global", "worldwide", "anywhere"}
    ]
    india_remote = (
        bool(india_scopes)
        or any(location_has(p, description_location) for p in india_remote_phrases)
        or any(location_has(p, location_value) for p in india_remote_phrases) and not other_scopes
        or bool(re.search(r"\banywhere (?:in|within|across) india\b", description_location))
    )
    workplace = location_words(job.workplace_type)
    explicitly_local = workplace in {"on site", "onsite", "hybrid"} or any(
        location_has(p, location_value) for p in ("on site", "onsite", "hybrid")
    )
    location_remote = workplace == "remote" or location_has("remote", location_value)
    india_excluded = INDIA_EXCLUSION.search(location_text)

    if india_excluded or description_restrictions or ((explicit_restrictions or other_scopes) and not india_remote):
        score -= 45
        restrictions = [*description_restrictions, *explicit_restrictions, *other_scopes]
        if india_excluded:
            restrictions.insert(0, india_excluded.group(0))
        cautions.append("Explicit location restriction: " + ", ".join(dict.fromkeys(restrictions[:3])))
        hard_excluded = True
    elif india_hits or india_remote:
        score += 15
        reasons.append("India/Delhi-NCR location match")
        if rules.get("require_remote_outside_delhi_ncr") and not any(
            location_has(place, location_value)
            for place in ("noida", "delhi", "gurugram", "gurgaon")
        ) and (explicitly_local or not (india_remote or location_remote or allowed_hits)):
            cautions.append("India remote eligibility is unclear outside Delhi-NCR")
            hard_excluded = True
    elif location_restrictions:
        score -= 45
        cautions.append("Explicit location restriction: " + ", ".join(dict.fromkeys(location_restrictions[:3])))
        hard_excluded = True
    elif allowed_hits or worldwide_location:
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
    seniority_title = FULL_TIME_TRACK.sub("", title)
    negative_hits = phrase_hits(seniority_title, rules["negative_phrases"])
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
        if location_has(phrase, location_words(seniority_title))
        or (not FULL_TIME.search(employment_value) and location_has(phrase, location_words(employment_value)))
    ]
    if location_has("contract", location_words(employment_value)) and "contract" not in blocked_employment:
        blocked_employment.append("contract")
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


def collect_jobs(
    config: dict[str, Any],
    now: datetime | None = None,
    initialized_discovery: set[str] | None = None,
) -> tuple[list[Job], list[str], list[str], list[str]]:
    jobs: list[Job] = []
    errors: list[str] = []
    warnings: list[str] = []
    completed_discovery: list[str] = []
    initialized_discovery = initialized_discovery or set()
    targets = [target for target in config["targets"] if target.get("enabled", True)]
    discovery_targets = [
        {**target, "role_phrases": config.get("rules", {}).get("role_phrases")}
        for target in config.get("discovery_sources", [])
        if target.get("enabled", True)
        and (target["name"] not in initialized_discovery or discovery_due(target, now))
    ]
    all_targets = [(target, False) for target in targets] + [
        (target, True) for target in discovery_targets
    ]
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(all_targets)))) as pool:
        futures = {
            pool.submit(
                DISCOVERY_FETCHERS[target["type"]] if optional else FETCHERS[target["type"]],
                target,
            ): (target, optional)
            for target, optional in all_targets
        }
        for future in as_completed(futures):
            target, optional = futures[future]
            try:
                jobs.extend(future.result())
                if optional:
                    completed_discovery.append(target["name"])
            except Exception as exc:  # keep other sources alive
                message = f"{target['name']} ({target['type']}): {exc}"
                (warnings if optional else errors).append(message)
    return jobs, errors, warnings, completed_discovery


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


def render_alerts(
    jobs: list[Job], errors: list[str], detected_at: str, warnings: list[str] | None = None
) -> str:
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
    if warnings:
        lines += ["---", "", "Optional discovery-source warnings:"]
        lines += [f"- {warning}" for warning in warnings]
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
    parser.add_argument("--backfill", action="store_true", help="Preview live, unalerted matches from every source")
    args = parser.parse_args(argv)
    if args.backfill and (not args.no_email or args.test_alert):
        parser.error("--backfill requires --no-email and cannot be combined with --test-alert")

    config = load_json(args.config, {})
    state = load_json(
        args.state,
        {"version": 4, "initialized": False, "seen": {}, "discovery_initialized": []},
    )
    detected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scanned_count = 0
    new_count = 0
    updated_count = 0
    became_eligible_count = 0
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
        warnings: list[str] = []
        completed_discovery: list[str] = []
        is_baseline = False
    else:
        collected = collect_jobs(
            config,
            initialized_discovery=set() if args.backfill else set(state.get("discovery_initialized", [])),
        )
        if len(collected) == 2:  # backward-compatible seam for existing integrations/tests
            all_jobs, errors = collected
            warnings = []
            completed_discovery = []
        elif len(collected) == 3:
            all_jobs, errors, warnings = collected
            completed_discovery = []
        else:
            all_jobs, errors, warnings, completed_discovery = collected
        all_jobs = deduplicate_jobs(all_jobs)
        scanned_count = len(all_jobs)
        is_baseline = not state.get("initialized", False)
        original_seen = state.get("seen", {})
        history_by_url: dict[str, Any] = {}
        for record in original_seen.values():
            url = posting_url(record.get("url", ""))
            if not url:
                continue
            prior = history_by_url.get(url)
            rank = (record.get("last_alerted_at", ""), record.get("last_seen_at", ""))
            if prior is None or rank > (
                prior.get("last_alerted_at", ""), prior.get("last_seen_at", "")
            ):
                history_by_url[url] = record

        previous_records = {}
        expired_ashby = []
        for job in all_jobs:
            alias = history_by_url.get(posting_url(job.url), {})
            prior = original_seen.get(job.key, {})
            previous = dict(prior or alias)
            if alias.get("last_alerted_at") and not previous.get("last_alerted_at"):
                previous["last_alerted_at"] = alias["last_alerted_at"]
            previous_records[job.key] = previous
            if not job.deadline_at:
                job.deadline_at = previous.get("deadline_at", "")
            if job.source == "Ashby" and job_deadline_expired(job):
                expired_ashby.append(job)
        if expired_ashby:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(hydrate_candidate_metadata, expired_ashby))
        refreshed = {job.key for job in expired_ashby}

        for job in all_jobs:
            score_job(job, config["rules"])
        to_enrich = [
            job for job in all_jobs
            if job.discovery and any("eligibility is unclear" in c.lower() for c in job.cautions)
            and urllib.parse.urlparse(job.url).scheme == "https"
            and urllib.parse.urlparse(job.url).netloc in PUBLIC_ATS_HOSTS
        ]
        if to_enrich:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(enrich_discovery_listing, to_enrich))
        for job in to_enrich:
            score_job(job, config["rules"])
        alert_candidates: list[Job] = []
        migration_candidates: list[Job] = []
        next_seen = json.loads(json.dumps(original_seen))
        initialized_discovery = set(state.get("discovery_initialized", []))
        discovery_baselines = set(completed_discovery) - initialized_discovery

        for job in all_jobs:
            previous = previous_records[job.key]
            fingerprint = job_fingerprint(job)
            is_new = not previous
            is_updated = bool(previous and previous.get("fingerprint") and previous["fingerprint"] != fingerprint)
            previous_eligible = previous.get("eligible")
            became_eligible = previous_eligible is False and job.eligible
            if is_new:
                new_count += 1
            elif is_updated:
                updated_count += 1
            if became_eligible:
                became_eligible_count += 1

            # Email only for a genuinely unseen posting or when a posting that
            # we previously recorded as ineligible becomes eligible. A content
            # or fingerprint schema change must never re-alert an old role.
            is_discovery_baseline = job.discovery and job.source in discovery_baselines
            if args.backfill and not previous.get("last_alerted_at"):
                alert_candidates.append(job)
            elif not is_baseline and not is_discovery_baseline and (
                is_new or (became_eligible and not previous.get("last_alerted_at"))
            ):
                alert_candidates.append(job)
            elif previous and "eligible" not in previous and job.eligible:
                # Hydrate old state once so the eligibility migration is
                # accurate, but deliberately keep it out of the alert queue.
                migration_candidates.append(job)

            # Preserve byte-stable state for unchanged jobs. Updating a
            # timestamp on every poll would create a Git commit every fifteen
            # minutes and cause pointless downstream deployments.
            needs_record_update = (
                is_new
                or is_updated
                or not previous.get("fingerprint")
                or previous.get("company") != job.company
                or previous != original_seen.get(job.key)
                or previous_eligible is None
                or previous_eligible != job.eligible
            )
            if needs_record_update:
                next_seen[job.key] = {
                    **previous,
                    "company": job.company,
                    "title": job.title,
                    "url": job.url,
                    "fingerprint": fingerprint,
                    "eligible": job.eligible,
                    "first_seen_at": previous.get("first_seen_at", detected_at),
                    "last_seen_at": detected_at,
                }
                if job.deadline_at:
                    next_seen[job.key]["deadline_at"] = job.deadline_at
                else:
                    next_seen[job.key].pop("deadline_at", None)

        candidate_count = len(alert_candidates)
        enrichment_candidates = alert_candidates + migration_candidates
        for job in enrichment_candidates:
            if job.eligible:
                if job.key not in refreshed:
                    hydrate_candidate_metadata(job)
                score_job(job, config["rules"])
                next_seen[job.key]["fingerprint"] = job_fingerprint(job)
                next_seen[job.key]["eligible"] = job.eligible
                if job.deadline_at:
                    next_seen[job.key]["deadline_at"] = job.deadline_at
                else:
                    next_seen[job.key].pop("deadline_at", None)

        jobs = [job for job in alert_candidates if job.eligible]
        blocked_count = 0 if is_baseline else sum(not job.eligible for job in alert_candidates)
        state["version"] = 4
        state["seen"] = next_seen
        state["initialized"] = True
        state["discovery_initialized"] = sorted(initialized_discovery | set(completed_discovery))

    alert_markdown = render_alerts(jobs, errors, detected_at, warnings)
    if args.backfill:
        ambiguous = [
            job for job in alert_candidates
            if not job.eligible and job.score >= config["rules"]["minimum_score"] - 20
            and any("eligibility is unclear" in caution.lower() for caution in job.cautions)
            and not any(caution.startswith(("Broad-market", "No preferred", "Non-IC", "Not a full-time",
                                             "Explicit", "Application deadline")) for caution in job.cautions)
        ]
        if ambiguous:
            alert_markdown += "\n## Geography needs verification\n\n"
            for job in sorted(ambiguous, key=lambda item: item.score, reverse=True)[:10]:
                alert_markdown += f"- {job.company} — {job.title} ({job.location or 'location not stated'}): {job.url}\n"
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

    # Never consume a new or newly eligible match unless its email was
    # delivered. Persist unrelated state while leaving failed/dry-run alerts
    # pending for the next execution.
    if not args.test_alert and not args.backfill:
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
        "became_eligible": became_eligible_count,
        "candidates": candidate_count,
        "blocked": blocked_count,
        "alerts": len(jobs),
        "email_sent": email_sent,
        "email_error": email_error,
        "source_errors": errors,
        "source_warnings": warnings,
        "discovery_sources_checked": completed_discovery,
    }
    print(json.dumps(summary, indent=2))
    write_github_output("alert_count", str(len(jobs)))
    write_github_output("email_sent", str(email_sent).lower())
    write_github_output("baseline", str(is_baseline).lower())
    write_github_output("source_error_count", str(len(errors)))
    write_github_output("source_warning_count", str(len(warnings)))
    write_github_output("scanned_count", str(scanned_count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
