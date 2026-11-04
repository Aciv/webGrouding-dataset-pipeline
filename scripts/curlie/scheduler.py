"""Central source queue: refill each freed slot immediately, independent of chunk completion."""
import json
import queue
import shutil
import signal
import time
from datetime import datetime, timezone
from storage import publish_batch, save_json
from worker_process import BrowserWorker, BrowserExecutorFailure, memory_status


def now():
    """UTC timestamps for durable scheduler transitions."""
    return datetime.now(timezone.utc).isoformat()


def run_scheduler(project, pool, output, state, template, history):
    """Schedule source jobs globally while retaining the existing chunk/index/export format."""
    from runner import choose_batch, totals
    settings = state["settings"]
    index_file = output / "collection-index.json"
    active = {}
    worker = None
    stopped = False
    exhausted = False
    finished = False
    started_batches = set()
    last_heartbeat = time.monotonic()
    restarts = 0
    for batch in state["batches"]:
        if batch["status"] != "completed":
            jobs = batch.setdefault("source_jobs", {})
            terminal = {site["id"] for site in batch.get("sites", [])}
            for candidate in batch["candidates"]:
                job = jobs.setdefault(candidate["id"], {"status": "completed" if candidate["id"] in terminal else "pending", "attempt": 0})
                if job["status"] == "running":
                    job["status"] = "pending"

    def save():
        """Persist admissions/completions before dispatch or disposable-file cleanup."""
        state["totals"] = totals(state)
        state["updatedAt"] = now()
        save_json(index_file, state)

    def interrupt(_number, _frame):
        nonlocal stopped
        stopped = True
        if worker and worker.child.poll() is None:
            worker.child.send_signal(signal.SIGINT)

    def pending():
        """Find an unclaimed source; running slow sources never block other chunks."""
        for batch in state["batches"]:
            if batch["status"] == "completed":
                continue
            for candidate in batch["candidates"]:
                if batch["source_jobs"][candidate["id"]]["status"] == "pending":
                    return batch, candidate
        return None

    def recover(error):
        """Stop admission, close the broken executor, and requeue in-flight sites without website failures."""
        nonlocal worker, restarts
        if error.category not in ("browser_unavailable", "process_exit"):
            raise ValueError(f"Executor error: {error}; use --resume") from error
        memory = memory_status()
        for batch, candidate, _ in active.values():
            batch["source_jobs"][candidate["id"]].update(status="pending", lastInfrastructureError=str(error))
            batch["status"] = "interrupted"
        active.clear()
        save()
        if worker:
            worker.close(interrupt=True)
        worker = None
        if stopped or totals(state)["pages"] >= settings["targetPages"]:
            return
        if restarts >= settings.get("browserRestarts", 2):
            raise ValueError(f"Browser restart limit exhausted; in-flight sites remain pending: {error}; use --resume")
        restarts += 1
        record = {"at": now(), "attempt": restarts, "category": error.category, "error": str(error),
                  "memoryAtDetectionMB": memory}
        state.setdefault("browser_restart_events", []).append(record)
        save()
        print(json.dumps({"type": "browser_restart", **record}), flush=True)

    def prepare(remaining):
        """Fetch a bounded candidate chunk for metadata/prefetch, never as a barrier."""
        nonlocal exhausted
        page_limit = max(2, min(settings["maxPagesPerSite"], remaining)) if template["discovery"].get("enabled", True) else 1
        size = min(settings["batchSites"], max(1, remaining // page_limit))
        candidates = choose_batch(pool, state, size, page_limit)
        if not candidates:
            exhausted = True
            return None
        number = len(state["batches"]) + 1
        batch = {"id": f"{number:05d}", "status": "planned", "candidates": candidates,
                 "maxPages": page_limit, "pages": [], "sites": [], "startedAt": now(),
                 "source_jobs": {item["id"]: {"status": "pending", "attempt": 0} for item in candidates}}
        config = {**template, "concurrency": settings["concurrency"], "exportOnly": settings["exportOnly"],
                  "sources": [{"id": c["id"], "kind": "url", "baseUrl": c["url"],
                               "name": c["title"], "category": c["category"]} for c in candidates],
                  "pages": [{"id": c["id"], "source": c["id"], "path": c["url"]} for c in candidates],
                  "discovery": {**template["discovery"], "maxPages": page_limit,
                                "minPages": min(template["discovery"]["minPages"], page_limit)}}
        save_json(output / "configs" / f"{batch['id']}.json", config)
        state["batches"].append(batch)
        save()
        return batch, candidates[0]

    def finish(event):
        """Publish only the finished source, checkpoint it, then immediately refill its slot."""
        job_id = event["jobId"]
        if job_id not in active:
            raise ValueError(f"Unexpected worker result: {job_id}")
        batch, candidate, reserved = active[job_id]
        work = output / ".work" / batch["id"]
        records, outcomes = publish_batch(work, output, batch["candidates"],
                                         [v["id"] for v in template["viewports"]], reserved,
                                         source_ids={candidate["id"]})
        history.sync(work, batch)
        history.exported(batch, records)
        by_id = {item["id"]: item for item in batch["pages"]}
        by_id.update({item["id"]: item for item in records})
        batch["pages"] = list(by_id.values())
        by_id = {item["id"]: item for item in batch["sites"]}
        by_id.update({item["id"]: item for item in outcomes})
        batch["sites"] = list(by_id.values())
        batch["source_jobs"][candidate["id"]].update(status="completed", result=event["status"],
            error=event.get("error"), finishedAt=now())
        del active[job_id]
        complete = all(item["status"] == "completed" for item in batch["source_jobs"].values())
        if complete:
            batch.update(status="completed", finishedAt=now())
        save()
        if complete:
            worker.send({"type": "release_batch", "batchId": batch["id"]})
            if settings["exportOnly"]:
                shutil.rmtree(work, ignore_errors=True)
        print(json.dumps({"type": "collection_progress", "targetPages": settings["targetPages"],
                          "active": len(active), "concurrency": settings["concurrency"],
                          **state["totals"]}), flush=True)

    previous = {number: signal.signal(number, interrupt) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        state["scheduler"] = "central_source_workers_v1"
        while not stopped:
            recovered_executor = False
            if worker and worker.failure:
                recover(worker.failure)
                continue
            while len(active) < settings["concurrency"] and not stopped:
                available = settings["targetPages"] - totals(state)["pages"] - sum(item[2] for item in active.values())
                if available <= 0:
                    break
                selected = pending()
                if selected is None and not exhausted:
                    selected = prepare(available)
                if selected is None:
                    break
                batch, candidate = selected
                source_job = batch["source_jobs"][candidate["id"]]
                source_job.update(status="running", attempt=source_job["attempt"] + 1, startedAt=now())
                batch["status"] = "running"
                first_dispatch = batch["id"] not in started_batches
                if first_dispatch:
                    started_batches.add(batch["id"])
                    batch["attempt"] = batch.get("attempt", 0) + 1
                    print(json.dumps({"type": "batch_started", "batch": batch["id"], "sites": len(batch["candidates"]),
                                      "completedPages": totals(state)["pages"], "targetPages": settings["targetPages"],
                                      "role": "prefetch_chunk_no_barrier"}), flush=True)
                save()
                # The durable queued history is idempotent; append it once per
                # chunk/start instead of rebuilding its entire list per source.
                if first_dispatch:
                    history.queued(batch)
                if stopped:
                    break
                if worker is None:
                    worker = BrowserWorker(project, settings["concurrency"], settings.get("siteTimeoutSeconds", 180))
                job_id = f"{batch['id']}/{candidate['id']}"
                active[job_id] = (batch, candidate, min(batch["maxPages"], available))
                try:
                    worker.send({"type": "collect", "jobId": job_id, "batchId": batch["id"],
                                 "sourceId": candidate["id"], "configFile": str(output / "configs" / f"{batch['id']}.json"),
                                 "output": str(output / ".work" / batch["id"]),
                                 "resume": (output / ".work" / batch["id"] / "batch-index.json").exists()})
                except BrowserExecutorFailure as error:
                    recover(error)
                    recovered_executor = True
                    break
            if stopped:
                break
            if recovered_executor:
                continue
            if time.monotonic() - last_heartbeat >= 10:
                last_heartbeat = time.monotonic()
                oldest = max((datetime.now(timezone.utc) - datetime.fromisoformat(batch["source_jobs"][candidate["id"]]["startedAt"])).total_seconds()
                             for batch, candidate, _ in active.values()) if active else 0
                print(json.dumps({"type": "scheduler_status", "active": len(active),
                                  "concurrency": settings["concurrency"], "pages": totals(state)["pages"],
                                  "targetPages": settings["targetPages"], "oldestRunningSeconds": round(oldest, 1)}), flush=True)
            if not active:
                state["status"] = "completed" if totals(state)["pages"] >= settings["targetPages"] else "pool_exhausted"
                # A prefetched source may become unnecessary after other jobs fill
                # the exact page quota. It is not a failed capture or a resume job.
                if state["status"] == "completed":
                    for batch in state["batches"]:
                        if batch["status"] == "completed":
                            continue
                        for job in batch["source_jobs"].values():
                            if job["status"] == "pending":
                                job.update(status="completed", result="not_needed", finishedAt=now())
                        batch.update(status="completed", finishedAt=now())
                    save()
                    for batch in state["batches"]:
                        if batch["id"] in started_batches:
                            history.sync(output / ".work" / batch["id"], batch)
                            if settings["exportOnly"]:
                                shutil.rmtree(output / ".work" / batch["id"], ignore_errors=True)
                finished = True
                break
            try:
                kind, line = worker.events.get(timeout=1)
            except queue.Empty:
                continue
            if kind == "eof":
                recover(worker.failure or BrowserExecutorFailure("Browser executor exited unexpectedly"))
                continue
            try:
                event = json.loads(line)
            except ValueError:
                print(line, end="", flush=True)
                continue
            if event.get("type") == "stream_result":
                # The reader sets failure immediately, even when a fatal event is
                # behind many diagnostic lines in this queue. Never refill a dead executor.
                if worker.failure:
                    recover(worker.failure)
                    continue
                try:
                    finish(event)
                except BrowserExecutorFailure as error:
                    recover(error)
            elif event.get("type") == "stream_fatal":
                recover(BrowserExecutorFailure(event.get("error", "Executor failed"), event.get("category", "executor_error")))
            else:
                print(line, end="", flush=True)
    finally:
        if worker:
            worker.close(interrupt=stopped or bool(active))
        for batch, candidate, _reserved in active.values():
            batch["source_jobs"][candidate["id"]]["status"] = "pending"
            batch["status"] = "interrupted"
        for batch in state["batches"]:
            for job in batch.get("source_jobs", {}).values():
                if job["status"] == "running":
                    job["status"] = "pending"
                    batch["status"] = "interrupted"
        for batch in state["batches"]:
            if batch["status"] != "completed":
                history.sync(output / ".work" / batch["id"], batch)
        if stopped or active or not finished:
            state["status"] = "interrupted"
        save()
        for number, handler in previous.items():
            signal.signal(number, handler)
    print(json.dumps({"type": "collection_finished", "status": state["status"], **state["totals"]}), flush=True)
    return 130 if stopped else 0 if state["status"] == "completed" else 2
