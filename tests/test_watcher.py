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

    def test_us_only_role_is_penalised(self):
        job = watcher.Job(
            key="x",
            company="Example Security",
            source="test",
            title="Product Marketing Manager",
            location="Remote - US only",
            description="Cybersecurity positioning and messaging.",
            url="https://example.com",
            company_priority=10,
        )
        watcher.score_job(job, self.rules)
        self.assertFalse(job.eligible)
        self.assertTrue(any("restriction" in caution.lower() for caution in job.cautions))

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


if __name__ == "__main__":
    unittest.main()
