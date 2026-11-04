"""Durable URL audit trail independent of disposable collector work directories."""
import json
import os
from datetime import datetime, timezone
from storage import read_json


class UrlHistory:
    """Append each observed transition once; repair only an interrupted final line."""
    def __init__(self, output):
        output.mkdir(parents=True, exist_ok=True)
        self.file = output / "url-history.jsonl"
        self.seen = set()
        self.snapshots = {}
        if self.file.exists():
            with self.file.open("r+b") as stream:
                while True:
                    start = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    try:
                        if not line.endswith(b"\n"):
                            raise ValueError("Incomplete last line")
                        event = json.loads(line)
                        self.seen.add(event["event_id"])
                    except (ValueError, KeyError):
                        if stream.read(1):
                            raise ValueError("URL history is corrupt before its last line")
                        stream.truncate(start)
                        stream.flush()
                        os.fsync(stream.fileno())
                        break

    def append(self, records):
        """Flush new records before source metadata can be deleted or marked complete."""
        fresh = []
        pending = set()
        for record in records:
            key = record["event_id"]
            if key not in self.seen and key not in pending:
                fresh.append({"recorded_at": datetime.now(timezone.utc).isoformat(), **record})
                pending.add(key)
        if fresh:
            with self.file.open("a") as stream:
                for record in fresh:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.seen.update(pending)

    def queued(self, batch):
        """Preserve entry URLs and original Curlie categories before collection begins."""
        self.append([{"event_id": f"{batch['id']}/{item['id']}/queued", "type": "site_queued",
                      "batch": batch["id"], "source": item["id"], "url": item["url"],
                      "category": item["category"], "category_path": item["category_path"],
                      "status": "queued"} for item in batch["candidates"]])

    def sync(self, work, batch):
        """Read atomic discovery checkpoints during collection and before cleanup.

        This is exploration history, not every image/font/analytics HTTP request.
        Old visit statuses keep stable IDs across a resumed collector process.
        """
        file = work / "discovery-index.json"
        if not file.exists():
            return
        stat = file.stat()
        signature = (stat.st_mtime_ns, stat.st_size, batch.get("attempt", 0))
        if self.snapshots.get(str(file)) == signature:
            return
        discovery = read_json(file)
        sources = {item["id"]: item for item in batch["candidates"]}
        records = []
        for site in discovery.get("sites", []):
            source = sources[site["sourceId"]]
            common = {"batch": batch["id"], "source": source["id"],
                      "category": source["category"], "category_path": source["category_path"]}
            visits = site.get("visits", [])
            records.append({**common,
                "event_id": f"{batch['id']}/{source['id']}/attempt-{batch.get('attempt', 0)}/site/{site['status']}/{len(visits)}",
                "type": "site_status", "url": site["entryUrl"], "status": site["status"],
                "error": site.get("error"), "stop_reason": site.get("stopReason"),
                "accepted_pages": len(site.get("acceptedPageIds", []))})
            for index, visit in enumerate(visits):
                candidate = visit["candidate"]
                records.append({**common,
                    "event_id": f"{batch['id']}/{source['id']}/visit-{index}/{visit['startedAt']}/{visit['status']}",
                    "type": "url_visit", "url": candidate["url"], "from_url": candidate.get("fromUrl"),
                    "method": candidate["method"], "status": visit["status"],
                    "started_at": visit["startedAt"], "elapsed_ms": visit.get("elapsedMs"), "timings_ms": visit.get("timings"),
                    "final_url": visit.get("finalUrl"), "page_id": visit.get("pageId"),
                    "duplicate_of": visit.get("duplicateOf"), "similarity": visit.get("similarity"),
                    "error": visit.get("error"), "viewport_results": visit.get("viewportResults")})
        self.append(records)
        self.snapshots[str(file)] = signature

    def exported(self, batch, pages):
        """Distinguish visited/accepted URLs from pages actually kept in the training output."""
        self.append([{"event_id": f"{batch['id']}/{page['id']}/exported", "type": "page_exported",
                      "batch": batch["id"], "source": page["source"], "page_id": page["id"],
                      "url": page["url"], "category": page["category"],
                      "category_path": page["category_path"], "status": "exported",
                      "annotation_rule_version": page.get("annotation_rule_version", "legacy_unmarked"),
                      "annotation_rules": page.get("annotation_rules"),
                      "viewports": page["viewports"]} for page in pages])
