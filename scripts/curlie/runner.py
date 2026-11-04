"""Page-target scheduling around the existing TypeScript collector; no browser fork."""
import collections
import json
import shutil
from datetime import datetime, timezone
from storage import read_json, save_json, identify_annotation_version
from history import UrlHistory
from prior_history import load_excluded_hosts
from recovery import restore_browser_failures


def totals(state):
    """Count unique complete responsive pages, not attempts or viewport images."""
    pages = [page for batch in state["batches"] for page in batch.get("pages", [])]
    categories = collections.Counter(page["category"] for page in pages)
    outcomes = collections.Counter(site["status"] for batch in state["batches"] for site in batch.get("sites", []))
    versions = collections.Counter(page.get("annotation_rule_version", "legacy_unmarked") for page in pages)
    return {"pages": len(pages), "images": sum(len(page["viewports"]) for page in pages),
            "categories": dict(categories), "sites": dict(outcomes), "annotation_versions": dict(versions)}


def choose_batch(pool, state, size, page_limit):
    """Prefer categories with fewer successful pages; failures trigger replacement candidates."""
    seen = {item["host"] for batch in state["batches"] for item in batch["candidates"]}
    seen.update(state.get("excluded_hosts", []))
    score = collections.Counter(totals(state)["categories"])
    exhausted = set()
    result = []
    while len(result) < size:
        categories = [category for category in state["settings"]["categories"] if category not in exhausted]
        if not categories:
            break
        category = min(categories, key=lambda item: (score[item], item))
        candidate = pool.next(category, state["offsets"], seen)
        if candidate is None:
            exhausted.add(category)
            continue
        result.append(candidate)
        seen.add(candidate["host"])
        score[category] += page_limit
    return result


def run_collection(project, pool, output, settings, template, resume):
    """Restore durable history, then hand all source admissions to the central scheduler."""
    file = output / "collection-index.json"
    state = read_json(file)
    if state and not resume:
        raise ValueError("Output exists; use --resume")
    runtime_keys = {"concurrency", "batchSites", "siteTimeoutSeconds", "browserRestarts"}
    effective = lambda value: {key: item for key, item in value.items() if key not in runtime_keys}
    if state and effective(state["settings"]) != effective(settings):
        raise ValueError("Resume requires the same collection settings; concurrency, batch-sites and site timeout may change")
    if state:
        state["settings"] = settings
    if not state:
        excluded = load_excluded_hosts(settings.get("skipHistory", []))
        state = {"schema": "curlie_page_target_v1", "settings": settings,
                      "offsets": {}, "batches": [], "status": "running",
                      "excluded_hosts": excluded,
                      "startedAt": datetime.now(timezone.utc).isoformat()}
        # Persist the import snapshot before launching any browser. Resume reuses it
        # even if prior run files are moved or changed later.
        save_json(file, state)
    if settings.get("skipHistory"):
        print(json.dumps({"type": "history_exclusions", "hosts": len(state["excluded_hosts"]),
                          "indices": settings["skipHistory"]}), flush=True)
    state["status"] = "running"
    state["publication_layout"] = "annotation_version_directories_v1"
    history = UrlHistory(output)
    recovered = restore_browser_failures(state)
    if recovered:
        print(json.dumps({"type": "infrastructure_jobs_requeued", "sites": recovered}), flush=True)
    for batch in state["batches"]:
        if batch["status"] == "completed":
            history.queued(batch)
            history.sync(output / ".work" / batch["id"], batch)
        for page in batch["pages"]:
            for observation in page["viewports"].values():
                for relative in observation.values():
                    if not (output / relative).is_file():
                        raise ValueError(f"Missing exported artifact: {relative}; cannot count it toward the target")
            identify_annotation_version(output, page)
        history.exported(batch, batch["pages"])
        if batch["status"] == "completed" and settings["exportOnly"]:
            shutil.rmtree(output / ".work" / batch["id"], ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)
    from scheduler import run_scheduler
    return run_scheduler(project, pool, output, state, template, history)
