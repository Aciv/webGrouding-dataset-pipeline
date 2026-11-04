"""Real local browser regressions: no chunk barrier, one browser, exact quotas and isolated deadlines."""
import http.server
import io
import json
import os
import signal
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "scripts/curlie"))
from pool import build_pool


class SchedulerTests(unittest.TestCase):
    """A blocked first request cannot hold the next candidate behind a metadata chunk."""
    def run_fixture(self, permanent_slow=False, interrupt_and_resume=False, crash=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            third_started = threading.Event()
            release = threading.Event()
            third_before_slow_response = []
            controlled = interrupt_and_resume or crash is not None

            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_GET(self):
                    if self.path == "/slow":
                        if permanent_slow or controlled:
                            release.wait(timeout=30)
                        else:
                            third_started.wait(timeout=20)
                    if self.path == "/third":
                        if controlled:
                            release.wait(timeout=30)
                        third_before_slow_response.append(not third_started.is_set())
                        third_started.set()
                    body = b'<!doctype html><style>body{margin:0;min-height:300px;background:#246}</style><h1>Fixture</h1>'
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                archive = root / "input.tar.gz"
                port = server.server_port
                urls = [f"http://127.0.0.{index}:{port}/{name}" for index, name in enumerate(["slow", "fast", "third"], 1)]
                with tarfile.open(archive, "w:gz") as bundle:
                    for name, text in [("rdf-Arts-s.tsv", "1\tArts/Test\t3\tFixture\n"),
                                       ("rdf-Arts-c.tsv", "".join(f"{url}\tPage\tTest\t1\n" for url in urls))]:
                        info = tarfile.TarInfo(name)
                        info.size = len(text.encode())
                        bundle.addfile(info, io.BytesIO(text.encode()))
                pool = root / "pool.sqlite"
                build_pool(archive, pool)
                with sqlite3.connect(pool) as db:
                    db.execute("UPDATE candidates SET priority=id")
                config = {"viewports": [{"id": label, "width": width, "height": 240} for label, width in
                          [("mobile", 320), ("desktop", 480), ("tablet", 400)]],
                          "navigationTimeoutMs": 25000, "settleMs": 0, "maxNodes": 100,
                          "maxHeight": 400, "captureMode": "full", "discovery": {"enabled": False}}
                (root / "config.json").write_text(json.dumps(config))
                output = root / "output"
                command = [sys.executable, str(PROJECT / "scripts/curlie/cli.py"), "--pool", str(pool),
                           "--config", str(root / "config.json"), "--output", str(output),
                           "--target-pages", "2" if permanent_slow else "3", "--batch-sites", "2",
                           "--concurrency", "1" if permanent_slow else "2", "--categories", "Arts",
                           "--site-timeout-seconds", "3" if permanent_slow else "25"]
                environment = {key: value for key, value in os.environ.items() if not key.lower().endswith("_proxy")}
                if controlled:
                    with (root / "interrupted.log").open("w") as log:
                        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment)
                        try:
                            until = time.monotonic() + 15
                            while time.monotonic() < until:
                                file = output / "collection-index.json"
                                if file.exists() and json.loads(file.read_text()).get("totals", {}).get("pages") == 1:
                                    break
                                time.sleep(.1)
                            else:
                                self.fail("Fast source was not published while other sources remained active")
                            if crash:
                                snapshot = json.loads((output / "collection-index.json").read_text())
                                published = next(page for batch in snapshot["batches"] for page in batch["pages"])
                                published_image = output / published["viewports"]["desktop"]["image"]
                                published_mtime = published_image.stat().st_mtime_ns
                                ready = next(json.loads(line) for line in (root / "interrupted.log").read_text().splitlines()
                                             if line.startswith('{') and json.loads(line).get("type") == "stream_ready")
                                executor = ready["pid"]
                                if crash == "browser":
                                    rows = subprocess.check_output(["ps", "-eo", "pid=,ppid="], text=True)
                                    children = [int(line.split()[0]) for line in rows.splitlines() if int(line.split()[1]) == executor]
                                    chrome = [pid for pid in children if "chrome" in Path(f"/proc/{pid}/comm").read_text().lower() or
                                              "chromium" in Path(f"/proc/{pid}/comm").read_text().lower()]
                                    self.assertTrue(chrome)
                                    os.kill(chrome[0], signal.SIGKILL)
                                else:
                                    os.kill(executor, signal.SIGKILL)
                                release.set()
                                child.wait(timeout=60)
                                self.assertEqual(child.returncode, 0, (root / "interrupted.log").read_text())
                                self.assertEqual(published_image.stat().st_mtime_ns, published_mtime)
                            else:
                                child.send_signal(signal.SIGINT)
                                child.wait(timeout=15)
                                self.assertEqual(child.returncode, 130, (root / "interrupted.log").read_text())
                        finally:
                            if child.poll() is None:
                                child.kill()
                                child.wait()
                    if crash:
                        result = subprocess.CompletedProcess(command, child.returncode, (root / "interrupted.log").read_text(), "")
                    else:
                        saved = json.loads((output / "collection-index.json").read_text())
                        exported = next(page for batch in saved["batches"] for page in batch["pages"])
                        image = output / exported["viewports"]["desktop"]["image"]
                        image_mtime = image.stat().st_mtime_ns
                        # Simulate an unfinished old index with no source-level scheduler ledger.
                        for batch in saved["batches"]:
                            batch.pop("source_jobs", None)
                        (output / "collection-index.json").write_text(json.dumps(saved))
                        release.set()
                        result = subprocess.run(command + ["--resume"], capture_output=True, text=True, timeout=60, env=environment)
                        self.assertEqual(image.stat().st_mtime_ns, image_mtime)
                        self.assertNotIn('"type":"site_started","id":"curlie-00000002"', result.stdout)
                else:
                    result = subprocess.run(command, capture_output=True, text=True, timeout=60, env=environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
                self.assertEqual(sum(event.get("type") == "stream_ready" for event in events), 2 if crash else 1)
                if crash:
                    self.assertEqual(sum(event.get("type") == "browser_restart" for event in events), 1)
                index = json.loads((output / "collection-index.json").read_text())
                self.assertEqual(index["totals"]["pages"], 2 if permanent_slow else 3)
                self.assertEqual(index["scheduler"], "central_source_workers_v1")
                self.assertFalse(any((output / ".work").iterdir()))
                if permanent_slow:
                    self.assertEqual(index["totals"]["sites"]["failed"], 1)
                    self.assertTrue(any("Site deadline exceeded" in (site.get("error") or "")
                                        for batch in index["batches"] for site in batch["sites"]))
                elif not controlled:
                    # First source is still waiting for third URL: old chunk scheduling would deadlock here.
                    self.assertTrue(third_before_slow_response)
                    started = next(i for i, event in enumerate(events) if event.get("type") == "site_started" and event["id"] == "curlie-00000003")
                    finished = next(i for i, event in enumerate(events) if event.get("type") == "site_finished" and event["id"] == "curlie-00000001")
                    self.assertLess(started, finished)
                    running = maximum = 0
                    for event in events:
                        if event.get("type") == "site_started":
                            running += 1
                            maximum = max(maximum, running)
                        if event.get("type") == "site_finished":
                            running -= 1
                    self.assertLessEqual(maximum, 2)
                    self.assertEqual(running, 0)
                # Operational concurrency/prefetch changes resume without recapturing a successful page.
                resumed = command.copy() + ["--resume"]
                resumed[resumed.index("--concurrency") + 1] = "3"
                resumed[resumed.index("--batch-sites") + 1] = "4"
                again = subprocess.run(resumed, capture_output=True, text=True, timeout=15, env=environment)
                self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
                self.assertNotIn('"type":"site_started"', again.stdout)
            finally:
                release.set()
                third_started.set()
                server.shutdown()
                server.server_close()
                thread.join()

    def test_refill_across_chunks_before_slow_site_finishes(self):
        self.run_fixture()

    def test_chromium_crash_recovers_without_losing_successes(self):
        self.run_fixture(crash="browser")

    def test_executor_kill_recovers_without_consuming_other_sites(self):
        self.run_fixture(crash="executor")

    def test_site_deadline_releases_only_its_slot(self):
        self.run_fixture(permanent_slow=True)

    def test_interrupt_recovers_old_partial_chunk_without_recapturing_success(self):
        self.run_fixture(interrupt_and_resume=True)


if __name__ == "__main__":
    unittest.main()
