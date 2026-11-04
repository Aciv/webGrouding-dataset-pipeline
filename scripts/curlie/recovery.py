"""Repair only known executor failures, leaving website failures and published pages untouched."""
import re


def restore_browser_failures(state):
    """Requeue old zero-output context-creation failures; never retry successful source captures."""
    if sum(len(batch.get("pages", [])) for batch in state["batches"]) >= state["settings"]["targetPages"]:
        return 0
    count = 0
    pattern = re.compile(r"browser\.(?:newContext|newPage):[^\n]*(?:closed|disconnected)", re.I)
    for batch in state["batches"]:
        published = {page["source"] for page in batch.get("pages", [])}
        retry = {site["id"] for site in batch.get("sites", []) if site["id"] not in published and
                 pattern.search(site.get("error") or "")}
        if not retry:
            continue
        outcomes = {item["id"]: item for item in batch.get("sites", [])}
        jobs = batch.setdefault("source_jobs", {})
        for candidate in batch["candidates"]:
            source_id = candidate["id"]
            job = jobs.setdefault(source_id, {"status": "completed" if source_id in outcomes else "pending", "attempt": 0})
            if source_id in retry:
                job.update(status="pending", lastInfrastructureError=outcomes[source_id]["error"])
                job.pop("finishedAt", None)
                job.pop("result", None)
                job.pop("error", None)
                count += 1
        batch["sites"] = [site for site in batch.get("sites", []) if site["id"] not in retry]
        batch["status"] = "interrupted"
    return count
