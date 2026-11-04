"""Small local regressions for pool refill, strict page quotas and compact publication."""
import functools
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
import unittest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "scripts"))
from pool import CandidatePool, build_pool
from export_pages import export_pages
from history import UrlHistory
from prior_history import load_excluded_hosts
from runner import choose_batch
from storage import publish_batch, identify_annotation_version


class PipelineTests(unittest.TestCase):
    """Use local HTML and an in-memory-sized archive; never contact real websites."""
    def test_publication_uses_actual_version_and_reuses_legacy_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work, output = root / "work", root / "output"
            folder = work / "pages/p/desktop"
            folder.mkdir(parents=True)
            (folder / "full.png").write_bytes(b"original-image")
            (folder / "hierarchy.json").write_text(json.dumps({"rule_version": "old_v5", "capture": {"image": "full.png"}}))
            (work / "batch-index.json").write_text(json.dumps({"pages": [{"id": "p", "source": "s", "status": "captured",
                "url": "https://example.com/", "observations": [{"viewport": "desktop", "status": "captured", "directory": "pages/p/desktop"}]}]}))
            candidates = [{"id": "s", "host": "example.com", "category": "Arts", "category_path": "Arts/Test"}]
            records, _ = publish_batch(work, output, candidates, ["desktop"], 1)
            self.assertEqual(records[0]["annotation_rule_version"], "old_v5")
            self.assertEqual(records[0]["viewports"]["desktop"]["image"], "versions/old_v5/pages/p/desktop/full.png")
            legacy = output / "pages/p"
            legacy.parent.mkdir()
            (output / "versions/old_v5/pages/p").rename(legacy)
            before = (legacy / "desktop/full.png").stat().st_mtime_ns
            restored, _ = publish_batch(work, output, candidates, ["desktop"], 1)
            self.assertEqual(restored[0]["viewports"]["desktop"]["image"], "pages/p/desktop/full.png")
            self.assertEqual((legacy / "desktop/full.png").stat().st_mtime_ns, before)
            del restored[0]["annotation_rule_version"]
            del restored[0]["annotation_rules"]
            identify_annotation_version(output, restored[0])
            self.assertEqual(restored[0]["annotation_rule_version"], "old_v5")
            self.assertEqual((legacy / "desktop/full.png").stat().st_mtime_ns, before)
    def test_imported_history_excludes_all_statuses_and_inherited_hosts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = {"schema": "curlie_page_target_v1", "excluded_hosts": ["www.inherited.test"],
                     "batches": [{"status": status, "candidates": [{"url": f"https://WWW.{status}.test/path"}]}
                                 for status in ["completed", "failed", "interrupted"]]}
            file = root / "collection-index.json"
            file.write_text(json.dumps(index))
            before = file.read_bytes()
            hosts = load_excluded_hosts([root, file])
            self.assertEqual(hosts, ["completed.test", "failed.test", "inherited.test", "interrupted.test"])
            self.assertEqual(file.read_bytes(), before)
            class Pool:
                def next(self, category, offsets, seen):
                    self_seen.update(seen)
                    return None
            self_seen = set()
            state = {"batches": [], "offsets": {}, "settings": {"categories": ["Arts"]}, "excluded_hosts": hosts}
            self.assertEqual(choose_batch(Pool(), state, 2, 1), [])
            self.assertEqual(self_seen, set(hosts))
            with self.assertRaises(FileNotFoundError):
                load_excluded_hosts([root / "missing.json"])
            file.write_text('{}')
            with self.assertRaisesRegex(ValueError, "Not a Curlie"):
                load_excluded_hosts([file])

    def test_history_dedup_and_partial_line_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = {"event_id": "one", "type": "url_visit", "url": "https://example.com/", "status": "failed"}
            history = UrlHistory(root)
            history.append([record, record])
            with history.file.open("ab") as stream:
                stream.write(b'{"event_id": "incomplete')
            restored = UrlHistory(root)
            restored.append([record, {**record, "event_id": "two"}])
            self.assertEqual(len(restored.file.read_text().splitlines()), 2)
    def test_pool_keeps_native_categories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with tarfile.open(root / "input.tar.gz", "w:gz") as archive:
                for name, text in {
                    "rdf-Arts-s.tsv": "1\tArts/Music\t2\tMusic\n",
                    "rdf-News-s.tsv": "2\tNews/Media\t1\tNews\n",
                    "rdf-Arts-c.tsv": "https://www.example.com/\tExample\tDescription\t1\nhttps://example.com/other\tOther\tDescription\t1\n",
                    "rdf-News-c.tsv": "https://example.com/\tExample\tDescription\t2\n"}.items():
                    value = text.encode()
                    item = tarfile.TarInfo(name)
                    item.size = len(value)
                    archive.addfile(item, io.BytesIO(value))
            build_pool(root / "input.tar.gz", root / "pool.sqlite")
            pool = CandidatePool(root / "pool.sqlite")
            try:
                self.assertEqual(pool.counts, {"Arts": 1, "News": 1})
                offsets = {}
                candidate = pool.next("Arts", offsets, set())
                self.assertEqual(candidate["category_path"], "Arts/Music")
                self.assertIsNone(pool.next("News", offsets, {candidate["host"]}))
            finally:
                pool.close()

    def test_export_full_canonical_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "input/pages/a/desktop"
            folder.mkdir(parents=True)
            (folder / "full.png").write_bytes(b"test-image")
            (folder / "hierarchy.json").write_text(json.dumps({"capture": {"image": "full.png"}}))
            (folder / "nodes.json").write_text("{}")
            export_pages(root / "input", root / "output")
            result = root / "output/pages/a/desktop"
            self.assertEqual({p.name for p in result.iterdir()}, {"full.png", "hierarchy.json"})

    def test_real_collector_refill_quota_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            site.mkdir()
            for name, color in [("index.html", "#aa1133"), ("second.html", "#1133aa")]:
                (site / name).write_text(f'<style>body{{margin:0;background:{color};color:white}}</style><h1>{name}</h1><a href="/second.html">Second</a>')
            class QuietHandler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *_args):
                    pass
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(site)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                archive = root / "input.tar.gz"
                port = server.server_port
                text = ("https://files.example.test/article.pdf?token2=expired\tBad PDF\tNot an HTML page\t1\n"
                        f"http://127.0.0.2:{port}/\tBad\tUnavailable\t1\n"
                        f"http://127.0.0.1:{port}/\tGood\tAvailable\t1\n"
                        f"http://localhost:{port}/\tGood2\tAvailable\t1\n")
                with tarfile.open(archive, "w:gz") as bundle:
                    for name, value in [("rdf-Arts-s.tsv", "1\tArts/Test\t4\tFixture\n"), ("rdf-Arts-c.tsv", text)]:
                        info = tarfile.TarInfo(name)
                        info.size = len(value.encode())
                        bundle.addfile(info, io.BytesIO(value.encode()))
                pool_file = root / "pool.sqlite"
                build_pool(archive, pool_file)
                with sqlite3.connect(pool_file) as db:
                    db.execute("UPDATE candidates SET priority=id")
                config = {"viewports": [{"id": label, "width": width, "height": 240} for label, width in [("mobile", 320), ("desktop", 480), ("tablet", 400)]],
                          "concurrency": 1, "navigationTimeoutMs": 1500, "settleMs": 10,
                          "maxNodes": 1000, "maxHeight": 300, "captureMode": "full",
                          "artifacts": {"initial": False, "overlayPng": False, "overlaySvg": False},
                          "discovery": {"enabled": True, "minPages": 2, "maxPages": 2, "maxVisits": 3,
                                        "maxCandidates": 10, "menuInteractions": 0,
                                        "similarityThreshold": .95, "viewport": "desktop"}}
                (root / "config.json").write_text(json.dumps(config))
                output = root / "output"
                command = [sys.executable, str(PROJECT / "scripts/curlie/cli.py"), "--pool", str(pool_file),
                           "--config", str(root / "config.json"), "--output", str(output), "--target-pages", "3",
                           "--batch-sites", "1", "--concurrency", "1", "--max-pages-per-site", "2", "--categories", "Arts"]
                environment = {key: value for key, value in os.environ.items() if not key.lower().endswith("_proxy")}
                interrupted = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               text=True, env=environment)
                self.assertIn('"type": "batch_started"', interrupted.stdout.readline())
                interrupted.send_signal(signal.SIGINT)
                stdout, stderr = interrupted.communicate(timeout=15)
                self.assertEqual(interrupted.returncode, 130, stdout + stderr)
                first = subprocess.run(command + ["--resume"], capture_output=True, text=True, timeout=90, env=environment)
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                index = json.loads((output / "collection-index.json").read_text())
                self.assertEqual(index["totals"]["pages"], 3)
                self.assertEqual(index["totals"]["images"], 9)
                self.assertEqual(index["totals"]["sites"]["failed"], 2)
                self.assertEqual(len(index["batches"]), 4)
                self.assertEqual(len(list((output / "versions").rglob("*.png"))), 9)
                self.assertEqual(len(list((output / "versions").rglob("hierarchy.json"))), 9)
                self.assertFalse((output / "pages").exists())
                self.assertFalse(any((output / ".work").iterdir()))
                self.assertFalse(list((output / "versions").rglob("initial.png")))
                log = output / "url-history.jsonl"
                history = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(len([row for row in history if row["type"] == "page_exported"]), 3)
                self.assertTrue(any(row["type"] == "url_visit" and row["status"] == "accepted" for row in history))
                self.assertTrue(any(row["type"] == "site_status" and row["status"] == "failed" and row["error"] for row in history))
                old_history = log.read_bytes()
                first_page = next(batch["pages"][0] for batch in index["batches"] if batch["pages"])
                self.assertEqual(first_page["annotation_rule_version"], "dom_visual_rules_6_child_geometry")
                self.assertIn(first_page["annotation_rule_version"], first_page["viewports"]["desktop"]["image"])
                before = (output / first_page["viewports"]["desktop"]["image"]).stat().st_mtime_ns
                resumed = subprocess.run(command + ["--resume"], capture_output=True, text=True, timeout=15, env=environment)
                self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                self.assertEqual(log.read_bytes(), old_history)
                self.assertEqual(before, (output / first_page["viewports"]["desktop"]["image"]).stat().st_mtime_ns)
                # The invalid PDF must not abort a healthy neighbor in the same batch.
                with sqlite3.connect(pool_file) as db:
                    db.execute("UPDATE candidates SET priority=CASE id WHEN 1 THEN 0 WHEN 3 THEN 1 WHEN 2 THEN 2 ELSE 3 END")
                mixed_command = command.copy()
                mixed_command[mixed_command.index("--output") + 1] = str(root / "mixed")
                mixed_command[mixed_command.index("--target-pages") + 1] = "4"
                mixed_command[mixed_command.index("--batch-sites") + 1] = "2"
                mixed = subprocess.run(mixed_command, capture_output=True, text=True, timeout=90, env=environment)
                self.assertEqual(mixed.returncode, 0, mixed.stdout + mixed.stderr)
                mixed_index = json.loads((root / "mixed/collection-index.json").read_text())
                self.assertEqual(mixed_index["totals"]["pages"], 4)
                self.assertEqual({site["status"] for site in mixed_index["batches"][0]["sites"]}, {"completed", "failed"})
                # Default-off CLI override captures only entry URLs, even with many links.
                homepage_command = command.copy() + ["--no-explore-pages"]
                homepage_command[homepage_command.index("--output") + 1] = str(root / "homepage")
                homepage_command[homepage_command.index("--target-pages") + 1] = "2"
                homepage_command[homepage_command.index("--batch-sites") + 1] = "4"
                homepage_command[homepage_command.index("--concurrency") + 1] = "4"
                homepage = subprocess.run(homepage_command, capture_output=True, text=True, timeout=90, env=environment)
                self.assertEqual(homepage.returncode, 0, homepage.stdout + homepage.stderr)
                home_index = json.loads((root / "homepage/collection-index.json").read_text())
                self.assertEqual(home_index["totals"]["pages"], 2)
                self.assertEqual(home_index["totals"]["images"], 6)
                self.assertTrue(all(batch["maxPages"] == 1 for batch in home_index["batches"]))
                home_history = [json.loads(line) for line in (root / "homepage/url-history.jsonl").read_text().splitlines()]
                visits = [row for row in home_history if row["type"] == "url_visit"]
                self.assertTrue(visits)
                self.assertTrue(all(row["method"] == "entry" for row in visits))
                home_config = json.loads((root / "homepage/configs/00001.json").read_text())
                self.assertFalse(home_config["discovery"]["enabled"])
                self.assertEqual(home_config["concurrency"], 4)
                # A new output may change modes but must exclude every old claimed host.
                skipped_command = homepage_command.copy() + ["--skip-history", str(output)]
                skipped_command[skipped_command.index("--output") + 1] = str(root / "skipped")
                skipped = subprocess.run(skipped_command, capture_output=True, text=True, timeout=15, env=environment)
                self.assertEqual(skipped.returncode, 2, skipped.stdout + skipped.stderr)
                skipped_index = json.loads((root / "skipped/collection-index.json").read_text())
                self.assertEqual(skipped_index["status"], "pool_exhausted")
                self.assertEqual(skipped_index["batches"], [])
                self.assertEqual(skipped_index["totals"]["pages"], 0)
                self.assertIn("127.0.0.1", skipped_index["excluded_hosts"])
                # Resume trusts the saved exclusion snapshot, without rereading its source.
                output.rename(root / "moved-history")
                resumed_skip = subprocess.run(skipped_command + ["--resume"], capture_output=True, text=True, timeout=15, env=environment)
                self.assertEqual(resumed_skip.returncode, 2, resumed_skip.stdout + resumed_skip.stderr)

            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
