import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import watcher


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.rules = json.loads((Path(__file__).parents[1] / "config.json").read_text())["rules"]

    def test_high_fit_india_role_passes(self):
        job = watcher.Job(
            key="x",
            company="Example Security",
            source="test",
            title="Technical Product Marketing Manager",
            location="Remote - India",
            description="Own positioning, messaging, launches and technical content for an AI cybersecurity product.",
            url="https://example.com",
            employment_type="Full-time",
            compensation="$80k-$100k",
            company_priority=18,
        )
        watcher.score_job(job, self.rules)
        self.assertTrue(job.eligible)
        self.assertGreaterEqual(job.score, 80)

    def test_us_only_role_is_hard_excluded_even_with_high_score(self):
        job = watcher.Job(
            key="x",
            company="Example Security",
            source="test",
            title="Product Marketing Manager",
            location="Remote - US only",
            description="Cybersecurity positioning and messaging.",
            url="https://example.com",
            company_priority=80,
        )
        watcher.score_job(job, self.rules)
        self.assertFalse(job.eligible)
        self.assertGreaterEqual(job.score, self.rules["minimum_score"])
        self.assertTrue(any("restriction" in caution.lower() for caution in job.cautions))

    def test_us_east_coast_wording_is_hard_excluded(self):
        job = watcher.Job(
            key="x",
            company="Example AI",
            source="test",
            title="Technical Communications",
            location="Remote",
            description="This role can be executed from anywhere on the US East Coast.",
            url="https://example.com",
            company_priority=50,
        )
        watcher.score_job(job, self.rules)
        self.assertFalse(job.eligible)
        self.assertTrue(any("us east coast" in caution.lower() for caution in job.cautions))

    def test_unclear_remote_location_is_not_alerted(self):
        job = watcher.Job(
            key="x",
            company="Example AI",
            source="test",
            title="Product Marketing Manager",
            location="Remote",
            description="Own AI positioning and launches.",
            url="https://example.com",
            company_priority=50,
        )
        watcher.score_job(job, self.rules)
        self.assertFalse(job.eligible)
        self.assertTrue(any("unclear" in caution.lower() for caution in job.cautions))

    def test_worldwide_remote_role_is_alerted(self):
        job = watcher.Job(
            key="x",
            company="Example AI",
            source="test",
            title="Product Marketing Manager",
            location="Remote worldwide",
            description="Own AI positioning and launches.",
            url="https://example.com",
            company_priority=20,
        )
        watcher.score_job(job, self.rules)
        self.assertTrue(job.eligible)

    def test_part_time_role_is_hard_excluded(self):
        job = watcher.Job(
            key="x",
            company="Example AI Security",
            source="test",
            title="Technical Product Marketing Manager",
            location="Remote worldwide",
            description="Own AI security positioning and launches.",
            url="https://example.com",
            employment_type="Part-time",
            company_priority=25,
        )
        watcher.score_job(job, self.rules)
        self.assertFalse(job.eligible)
        self.assertTrue(any("full-time" in caution.lower() for caution in job.cautions))

    def test_explicit_salary_below_floor_is_hard_excluded(self):
        job = watcher.Job(
            key="x",
            company="Example AI Security",
            source="test",
            title="Technical Product Marketing Manager",
            location="Remote worldwide",
            description="Own AI security positioning and launches.",
            url="https://example.com",
            employment_type="Full-time",
            compensation="$36k-$60k",
            company_priority=25,
        )
        watcher.score_job(job, self.rules)
        self.assertFalse(job.eligible)
        self.assertTrue(any("$65,000" in caution for caution in job.cautions))

    def test_salary_range_reaching_floor_is_not_rejected(self):
        job = watcher.Job(
            key="x",
            company="Example AI Security",
            source="test",
            title="Technical Product Marketing Manager",
            location="Remote worldwide",
            description="Own AI security positioning and launches.",
            url="https://example.com",
            employment_type="Full-time",
            compensation="$50k-$90k",
            company_priority=25,
        )
        watcher.score_job(job, self.rules)
        self.assertTrue(job.eligible)

    def test_worldwide_remote_outranks_equivalent_india_role(self):
        common = {
            "key": "x",
            "company": "Example AI Security",
            "source": "test",
            "title": "Technical Product Marketing Manager",
            "description": "Own AI cybersecurity positioning and launches.",
            "url": "https://example.com",
            "company_priority": 10,
        }
        worldwide = watcher.Job(**common, location="Remote worldwide")
        india = watcher.Job(**{**common, "key": "y"}, location="India")
        watcher.score_job(worldwide, self.rules)
        watcher.score_job(india, self.rules)
        self.assertTrue(worldwide.eligible)
        self.assertTrue(india.eligible)
        self.assertGreater(worldwide.score, india.score)

    def test_html_board_hydrates_priority_role_for_change_detection(self):
        target = {
            "name": "Arcanna.ai",
            "type": "html",
            "url": "https://arcanna.ai/careers",
            "priority": 22,
            "default_location": "Remote worldwide",
            "hydrate_job_pages": True,
            "hydrate_title_regex": "(?i)product marketing",
            "include_href_regex": "(?i)arcanna\\.ai/jobs/.+",
        }
        careers = (
            '<main><a href="/jobs/technical-product-marketing-manager">'
            "Technical Product Marketing Manager Remote</a></main>"
        )
        detail = (
            "<html><script>changing-build-id</script><main>"
            "Own Agentic AI cybersecurity positioning and sales enablement."
            "</main></html>"
        )
        with patch.object(watcher, "fetch_text", side_effect=[careers, detail]):
            jobs = watcher.fetch_html_board(target)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "Remote worldwide")
        self.assertIn("Agentic AI cybersecurity", jobs[0].description)
        self.assertNotIn("changing-build-id", jobs[0].description)
        watcher.score_job(jobs[0], self.rules)
        self.assertTrue(jobs[0].eligible)

    def test_html_board_extracts_structured_location_and_employment(self):
        target = {
            "name": "Simbian",
            "type": "html",
            "url": "https://example.com/careers",
            "priority": 24,
            "hydrate_job_pages": True,
            "hydrate_title_regex": "(?i)product marketing",
            "include_href_regex": "(?i)example\\.com/jobs/.+",
            "location_regex": "(?i)City\\s+(?P<location>.+?)\\s+State/Province",
            "employment_type_regex": (
                "(?i)Job Type\\s*:?\\s*(?P<employment_type>Full[- ]?time|Part[- ]?time)"
            ),
        }
        careers = '<main><a href="/jobs/pmm">Product Marketing Manager</a></main>'
        detail = (
            "<main>City Bangalore (Remote), India State/Province Karnataka "
            "Job Type Full time Own agentic AI cybersecurity positioning.</main>"
        )
        with patch.object(watcher, "fetch_text", side_effect=[careers, detail]):
            jobs = watcher.fetch_html_board(target)
        self.assertEqual(jobs[0].location, "Bangalore (Remote), India")
        self.assertEqual(jobs[0].employment_type, "Full time")
        watcher.score_job(jobs[0], self.rules)
        self.assertTrue(jobs[0].eligible)

    def test_watchlist_contains_only_ai_cybersecurity_targets(self):
        config = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        names = {target["name"] for target in config["targets"]}
        removed_irrelevant = {
            "OpenAI", "Anthropic", "ElevenLabs", "Canonical", "Cloudflare",
            "OpenZeppelin", "JumpCloud", "Cyara", "Kantiv", "Chainguard", "Wiz",
        }
        expected_core = {
            "Arcanna.ai", "Harmonic Security", "Dropzone AI", "CloudSEK",
            "Prophet Security", "HiddenLayer", "XBOW", "WitnessAI", "Simbian",
        }
        self.assertTrue(removed_irrelevant.isdisjoint(names))
        self.assertTrue(expected_core.issubset(names))

    def test_discovery_inventory_covers_global_remote_markets(self):
        config = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        names = {source["name"] for source in config["discovery_sources"]}
        expected = {
            "ZeroDay Careers", "Startup.jobs", "Himalayas", "Remote OK", "Jobicy",
            "We Work Remotely — Sales and Marketing", "Remotive",
        }
        self.assertTrue(expected.issubset(names))
        self.assertTrue(all(source["cadence_minutes"] >= 30 for source in config["discovery_sources"]))

    def test_discovery_cadence_reuses_quarter_hour_runs(self):
        hourly = {"cadence_minutes": 60, "offset_minutes": 2}
        self.assertTrue(
            watcher.discovery_due(hourly, datetime(2026, 11, 4, 12, 2, tzinfo=timezone.utc))
        )
        self.assertFalse(
            watcher.discovery_due(hourly, datetime(2026, 11, 4, 12, 17, tzinfo=timezone.utc))
        )
        staggered = {"cadence_minutes": 60, "offset_minutes": 32}
        self.assertTrue(
            watcher.discovery_due(staggered, datetime(2026, 11, 4, 12, 32, tzinfo=timezone.utc))
        )

    def test_rss_discovery_parses_company_location_and_description(self):
        target = {
            "name": "Example Remote Market",
            "type": "rss",
            "url": "https://example.com/jobs.rss",
            "title_company_regex": "^(?P<company>[^:]+):\\s*(?P<title>.+)$",
        }
        feed = """<?xml version="1.0"?><rss><channel><item>
          <title>Shield AI: Technical Product Marketing Manager</title>
          <link>https://example.com/jobs/42</link>
          <description>Own positioning for an AI security platform.</description>
          <region>Anywhere in the world</region>
          <guid>42</guid>
        </item></channel></rss>"""
        with patch.object(watcher, "fetch_text", return_value=feed):
            jobs = watcher.fetch_rss_market(target)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "Shield AI")
        self.assertEqual(jobs[0].title, "Technical Product Marketing Manager")
        self.assertEqual(jobs[0].location, "Anywhere in the world")
        self.assertTrue(jobs[0].discovery)

    def test_jobicy_discovery_normalizes_worldwide_location(self):
        target = {"name": "Jobicy", "type": "jobicy", "url": "https://example.com/api"}
        payload = {
            "jobs": [{
                "id": 7,
                "companyName": "Example Security",
                "jobTitle": "Product Marketing Manager",
                "jobGeo": "Worldwide",
                "jobDescription": "Cybersecurity positioning and launches",
                "url": "https://example.com/jobs/7",
                "jobType": "Full-time",
            }]
        }
        with patch.object(watcher, "fetch_json", return_value=payload):
            jobs = watcher.fetch_jobicy_market(target)
        self.assertEqual(jobs[0].location, "Remote worldwide")
        watcher.score_job(jobs[0], self.rules)
        self.assertTrue(jobs[0].eligible)

    def test_broad_market_role_requires_ai_or_cybersecurity_signal(self):
        job = watcher.Job(
            key="discovery:x",
            company="Generic SaaS",
            source="Global market",
            title="Technical Product Marketing Manager",
            location="Remote worldwide",
            description="Own positioning and launches for an accounting platform.",
            url="https://example.com/job",
            discovery=True,
        )
        watcher.score_job(job, self.rules)
        self.assertFalse(job.eligible)
        self.assertTrue(any("AI/cybersecurity" in caution for caution in job.cautions))

    def test_cross_market_duplicate_prefers_first_party_record(self):
        discovery = watcher.Job(
            key="discovery:global:1",
            company="Arcanna.ai",
            source="Remote market",
            title="Technical Product Marketing Manager",
            location="Remote worldwide",
            description="AI security role",
            url="https://remote.example/jobs/1",
            discovery=True,
        )
        direct = watcher.Job(
            key="html:arcanna:1",
            company="Arcanna AI",
            source="Career page",
            title="Technical Product Marketing Manager",
            location="Remote worldwide",
            description="Full employer description for this AI security role",
            url="https://arcanna.ai/jobs/technical-product-marketing-manager",
        )
        self.assertEqual(watcher.deduplicate_jobs([discovery, direct]), [direct])

    def test_optional_discovery_failure_does_not_fail_core_sources(self):
        config = {
            "targets": [{"name": "Core", "type": "ashby"}],
            "discovery_sources": [{
                "name": "Optional", "type": "rss", "cadence_minutes": 60,
                "offset_minutes": 2,
            }],
        }
        now = datetime(2026, 11, 4, 12, 2, tzinfo=timezone.utc)
        with patch.dict(watcher.FETCHERS, {"ashby": lambda _: []}), patch.dict(
            watcher.DISCOVERY_FETCHERS,
            {"rss": lambda _: (_ for _ in ()).throw(RuntimeError("temporary outage"))},
        ):
            jobs, errors, warnings, completed = watcher.collect_jobs(config, now=now)
        self.assertEqual(jobs, [])
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(completed, [])

    def test_people_manager_role_is_hard_excluded(self):
        job = watcher.Job(
            key="x",
            company="Example Security",
            source="test",
            title="Product Marketing Manager",
            location="India",
            description="Manage a team while owning cybersecurity positioning.",
            url="https://example.com",
            company_priority=50,
        )
        watcher.score_job(job, self.rules)
        self.assertFalse(job.eligible)
        self.assertTrue(any("people-management" in caution.lower() for caution in job.cautions))

    def test_expired_deadline_is_hard_excluded(self):
        job = watcher.Job(
            key="x",
            company="Example Security",
            source="test",
            title="Product Marketing Manager",
            location="India",
            description="Cybersecurity positioning.",
            url="https://example.com",
            deadline_at="2020-01-01T00:00:00Z",
            company_priority=50,
        )
        watcher.score_job(job, self.rules)
        self.assertFalse(job.eligible)
        self.assertTrue(any("deadline" in caution.lower() for caution in job.cautions))

    def test_first_run_is_a_quiet_baseline(self):
        config = {
            "rules": self.rules,
            "targets": [{"name": "Example", "type": "ashby", "board": "example", "priority": 20}],
        }
        job = watcher.Job(
            key="ashby:example:1",
            company="Example",
            source="Ashby",
            title="Product Marketing Manager",
            location="India",
            description="Cybersecurity",
            url="https://example.com/job",
            company_priority=20,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(watcher, "collect_jobs", return_value=([job], [])):
            root = Path(tmp)
            config_path = root / "config.json"
            state_path = root / "state.json"
            alerts_path = root / "alerts.md"
            config_path.write_text(json.dumps(config))
            result = watcher.main([
                "--config", str(config_path),
                "--state", str(state_path),
                "--alerts", str(alerts_path),
                "--no-email",
            ])
            self.assertEqual(result, 0)
            self.assertIn("0 high-signal", alerts_path.read_text())
            self.assertTrue(json.loads(state_path.read_text())["initialized"])

    def test_new_discovery_source_builds_its_own_quiet_baseline(self):
        config = {"rules": self.rules, "targets": [], "discovery_sources": []}
        job = watcher.Job(
            key="discovery-rss:market:1",
            company="Example Security",
            source="New Global Market",
            title="Technical Product Marketing Manager",
            location="Remote worldwide",
            description="AI cybersecurity positioning and launches",
            url="https://example.com/job",
            discovery=True,
        )
        collected = ([job], [], [], ["New Global Market"])
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            watcher, "collect_jobs", return_value=collected
        ), patch.object(watcher, "send_email") as send_email:
            root = Path(tmp)
            config_path = root / "config.json"
            state_path = root / "state.json"
            alerts_path = root / "alerts.md"
            config_path.write_text(json.dumps(config))
            state_path.write_text(json.dumps({
                "version": 3, "initialized": True, "seen": {},
            }))
            watcher.main([
                "--config", str(config_path), "--state", str(state_path),
                "--alerts", str(alerts_path),
            ])
            saved = json.loads(state_path.read_text())
            alert_text = alerts_path.read_text()
        send_email.assert_not_called()
        self.assertIn(job.key, saved["seen"])
        self.assertEqual(saved["discovery_initialized"], ["New Global Market"])
        self.assertIn("0 high-signal", alert_text)

    def test_undelivered_match_is_not_consumed(self):
        config = {
            "rules": self.rules,
            "targets": [{"name": "Example", "type": "ashby", "board": "example", "priority": 20}],
        }
        job = watcher.Job(
            key="ashby:example:new",
            company="Example",
            source="Ashby",
            title="Technical Product Marketing Manager",
            location="India",
            description="AI cybersecurity positioning and technical content",
            url="https://example.com/new-job",
            company_priority=20,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(watcher, "collect_jobs", return_value=([job], [])):
            root = Path(tmp)
            config_path = root / "config.json"
            state_path = root / "state.json"
            alerts_path = root / "alerts.md"
            config_path.write_text(json.dumps(config))
            state_path.write_text(json.dumps({"version": 1, "initialized": True, "seen": {}}))
            watcher.main([
                "--config", str(config_path),
                "--state", str(state_path),
                "--alerts", str(alerts_path),
            ])
            saved = json.loads(state_path.read_text())
            self.assertNotIn(job.key, saved["seen"])

    def test_no_email_dry_run_does_not_consume_match(self):
        config = {"rules": self.rules, "targets": []}
        job = watcher.Job(
            key="test:new",
            company="Example",
            source="test",
            title="Technical Product Marketing Manager",
            location="India",
            description="AI cybersecurity positioning",
            url="https://example.com/job",
            company_priority=20,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(watcher, "collect_jobs", return_value=([job], [])):
            root = Path(tmp)
            config_path = root / "config.json"
            state_path = root / "state.json"
            alerts_path = root / "alerts.md"
            config_path.write_text(json.dumps(config))
            state_path.write_text(json.dumps({"version": 2, "initialized": True, "seen": {}}))
            watcher.main([
                "--config", str(config_path), "--state", str(state_path),
                "--alerts", str(alerts_path), "--no-email",
            ])
            self.assertNotIn(job.key, json.loads(state_path.read_text())["seen"])

    def test_material_update_migrates_without_realerting_old_role(self):
        config = {"rules": self.rules, "targets": []}
        original = watcher.Job(
            key="test:updated",
            company="Example",
            source="test",
            title="Product Marketing Manager",
            location="India",
            description="Product positioning.",
            url="https://example.com/job",
            company_priority=20,
        )
        changed = watcher.Job(**{**original.__dict__, "description": "AI cybersecurity positioning and launches."})
        previous = {
            original.key: {
                "company": original.company,
                "title": original.title,
                "url": original.url,
                "fingerprint": watcher.job_fingerprint(original),
                "first_seen_at": "2026-01-01T00:00:00+00:00",
            }
        }
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(watcher, "collect_jobs", return_value=([changed], [])), \
                patch.object(watcher, "send_email") as send_email:
            root = Path(tmp)
            config_path = root / "config.json"
            state_path = root / "state.json"
            alerts_path = root / "alerts.md"
            config_path.write_text(json.dumps(config))
            state_path.write_text(json.dumps({"version": 2, "initialized": True, "seen": previous}))
            watcher.main([
                "--config", str(config_path), "--state", str(state_path),
                "--alerts", str(alerts_path),
            ])
            send_email.assert_not_called()
            saved = json.loads(state_path.read_text())["seen"][changed.key]
            self.assertEqual(saved["fingerprint"], watcher.job_fingerprint(changed))
            self.assertTrue(saved["eligible"])
            self.assertNotIn("last_alerted_at", saved)

    def test_previously_ineligible_role_alerts_when_it_becomes_eligible(self):
        config = {"rules": self.rules, "targets": []}
        original = watcher.Job(
            key="test:became-eligible",
            company="Example",
            source="test",
            title="Technical Product Marketing Manager",
            location="Remote",
            description="AI cybersecurity positioning and launches.",
            url="https://example.com/job",
            company_priority=20,
        )
        changed = watcher.Job(**{**original.__dict__, "location": "Remote worldwide"})
        previous = {
            original.key: {
                "company": original.company,
                "title": original.title,
                "url": original.url,
                "fingerprint": watcher.job_fingerprint(original),
                "eligible": False,
                "first_seen_at": "2026-01-01T00:00:00+00:00",
            }
        }
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(watcher, "collect_jobs", return_value=([changed], [])), \
                patch.object(watcher, "send_email") as send_email:
            root = Path(tmp)
            config_path = root / "config.json"
            state_path = root / "state.json"
            alerts_path = root / "alerts.md"
            config_path.write_text(json.dumps(config))
            state_path.write_text(json.dumps({"version": 3, "initialized": True, "seen": previous}))
            watcher.main([
                "--config", str(config_path), "--state", str(state_path),
                "--alerts", str(alerts_path),
            ])
            send_email.assert_called_once()
            saved = json.loads(state_path.read_text())["seen"][changed.key]
            self.assertTrue(saved["eligible"])
            self.assertIn("last_alerted_at", saved)

    def test_unchanged_listing_does_not_churn_persisted_state(self):
        config = {"rules": self.rules, "targets": []}
        job = watcher.Job(
            key="test:unchanged",
            company="Example",
            source="test",
            title="Product Marketing Manager",
            location="India",
            description="AI cybersecurity positioning.",
            url="https://example.com/job",
            company_priority=20,
        )
        previous = {
            job.key: {
                "company": job.company,
                "title": job.title,
                "url": job.url,
                "fingerprint": watcher.job_fingerprint(job),
                "eligible": True,
                "first_seen_at": "2026-01-01T00:00:00+00:00",
                "last_seen_at": "2026-01-01T00:00:00+00:00",
            }
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            watcher, "collect_jobs", return_value=([job], [])
        ):
            root = Path(tmp)
            config_path = root / "config.json"
            state_path = root / "state.json"
            alerts_path = root / "alerts.md"
            config_path.write_text(json.dumps(config))
            state_path.write_text(json.dumps({"version": 2, "initialized": True, "seen": previous}))
            watcher.main([
                "--config", str(config_path), "--state", str(state_path),
                "--alerts", str(alerts_path), "--no-email",
            ])
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved["seen"], previous)

    def test_legacy_state_migrates_without_alert_storm(self):
        config = {"rules": self.rules, "targets": []}
        job = watcher.Job(
            key="test:legacy",
            company="Example",
            source="test",
            title="Product Marketing Manager",
            location="India",
            description="AI cybersecurity positioning.",
            url="https://example.com/job",
            company_priority=20,
        )
        legacy_entry = {
            job.key: {"company": job.company, "title": job.title, "url": job.url,
                      "first_seen_at": "2026-01-01T00:00:00+00:00"}
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(watcher, "collect_jobs", return_value=([job], [])):
            root = Path(tmp)
            config_path = root / "config.json"
            state_path = root / "state.json"
            alerts_path = root / "alerts.md"
            config_path.write_text(json.dumps(config))
            state_path.write_text(json.dumps({"version": 1, "initialized": True, "seen": legacy_entry}))
            watcher.main([
                "--config", str(config_path), "--state", str(state_path),
                "--alerts", str(alerts_path), "--no-email",
            ])
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved["version"], 4)
            self.assertIn("fingerprint", saved["seen"][job.key])
            self.assertIn("eligible", saved["seen"][job.key])
            self.assertIn("0 high-signal", alerts_path.read_text())

    def test_html_linkifier_does_not_include_trailing_colon(self):
        rendered = watcher.markdown_to_html("Source: https://example.com/jobs/:")
        self.assertIn('href="https://example.com/jobs/"', rendered)
        self.assertNotIn('href="https://example.com/jobs/:"', rendered)


if __name__ == "__main__":
    unittest.main()
