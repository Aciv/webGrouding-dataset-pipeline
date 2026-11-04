"""Infrastructure recovery budgets and conservative repair of historical false failures."""
import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "scripts/curlie"))
from recovery import restore_browser_failures
from history import UrlHistory
from scheduler import run_scheduler
from worker_process import BrowserExecutorFailure


class RecoveryTests(unittest.TestCase):
    def test_only_zero_output_browser_creation_failures_are_requeued(self):
        state = {"settings": {"targetPages": 10}, "batches": [{"status": "completed",
            "candidates": [{"id": name} for name in ["browser", "http", "good"]],
            "pages": [{"source": "good"}], "sites": [
                {"id": "browser", "error": "browser.newContext: Target page, context or browser has been closed"},
                {"id": "http", "error": "Navigation HTTP 403"},
                {"id": "good", "error": "browser.newContext: Target page, context or browser has been closed"}]}]}
        self.assertEqual(restore_browser_failures(state), 1)
        batch = state["batches"][0]
        self.assertEqual(batch["source_jobs"]["browser"]["status"], "pending")
        self.assertEqual(batch["source_jobs"]["http"]["status"], "completed")
        self.assertEqual(batch["source_jobs"]["good"]["status"], "completed")
        self.assertEqual(batch["pages"], [{"source": "good"}])
        self.assertEqual(restore_browser_failures(state), 0)

    def check_failure_budget(self, category, expected_executors, expected_restarts):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instances = []

            class Pool:
                def next(self, _category, _offsets, seen):
                    return None if "site.test" in seen else {"id": "source", "host": "site.test", "url": "http://site.test/",
                        "title": "Fixture", "category": "Arts", "category_path": "Arts/Test"}

            class FailedExecutor:
                def __init__(self, *_args):
                    instances.append(self)
                    self.events = queue.Queue()
                    self.failure = None

                def send(self, _message):
                    raise BrowserExecutorFailure("simulated executor failure", category)

                def close(self, **_kwargs):
                    pass

            template = {"viewports": [{"id": "desktop", "width": 400, "height": 300}],
                        "discovery": {"enabled": False, "minPages": 1}}
            settings = {"targetPages": 1, "categories": ["Arts"], "batchSites": 1, "concurrency": 1,
                        "maxPagesPerSite": 3, "exportOnly": True, "browserRestarts": 2}
            state = {"settings": settings, "batches": [], "offsets": {}, "status": "running"}
            with patch("scheduler.BrowserWorker", FailedExecutor):
                with self.assertRaises(ValueError):
                    run_scheduler(PROJECT, Pool(), root, state, template, UrlHistory(root))
            self.assertEqual(len(instances), expected_executors)
            self.assertEqual(len(state.get("browser_restart_events", [])), expected_restarts)
            saved = json.loads((root / "collection-index.json").read_text())
            self.assertEqual(saved["status"], "interrupted")
            self.assertEqual(len(saved["batches"]), 1, "Repeated infrastructure failure must not consume new URLs")
            self.assertEqual(saved["batches"][0]["source_jobs"]["source"]["status"], "pending")
            self.assertEqual(saved["totals"]["sites"], {})

    def test_repeated_browser_failure_stops_at_budget_without_burning_pool(self):
        self.check_failure_budget("browser_unavailable", 3, 2)

    def test_non_browser_executor_error_is_not_automatically_retried(self):
        self.check_failure_budget("executor_error", 1, 0)


if __name__ == "__main__":
    unittest.main()
