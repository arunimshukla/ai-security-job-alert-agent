import json
import tempfile
import unittest
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

    def test_material_update_is_alerted_once_and_recorded(self):
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
            send_email.assert_called_once()
            saved = json.loads(state_path.read_text())["seen"][changed.key]
            self.assertEqual(saved["fingerprint"], watcher.job_fingerprint(changed))
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
            self.assertEqual(saved["version"], 2)
            self.assertIn("fingerprint", saved["seen"][job.key])
            self.assertIn("0 high-signal", alerts_path.read_text())

    def test_html_linkifier_does_not_include_trailing_colon(self):
        rendered = watcher.markdown_to_html("Source: https://example.com/jobs/:")
        self.assertIn('href="https://example.com/jobs/"', rendered)
        self.assertNotIn('href="https://example.com/jobs/:"', rendered)


if __name__ == "__main__":
    unittest.main()
