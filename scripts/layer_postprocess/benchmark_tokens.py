#!/usr/bin/env python3
"""Compare sparse verification with recorded full requests on IDENTICAL saved final trees."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
from pathlib import Path
import time
from storage import image_path, write
from delta_context import review_patch, reconstruct_review_boxes, row
from frontier import large_region_requirements, validate_large_region_review
from model_edits import apply_model_edits
from normalize import normalize
from rules import apply_operations, propose
from vlm import Client, ResponseError


def benchmark(file, config):
    """Reuse paid edit decisions; spend only one new verification call per fixed observation."""
    audit = json.loads(file.read_text())
    source = Path(audit["source"])
    original = json.loads(source.read_text())
    final = json.loads(file.with_name("hierarchy.json").read_text())
    prepare_ops = audit.get("edit_base_operations", audit.get("rule_operations", []))
    before, _ = apply_operations(original, prepare_ops)
    response = audit["edit_response"]
    _, applied, rejected = apply_model_edits(before, response["operations"], config.get("rules", {}))
    edits = applied+audit.get("followup_rule_operations", [])
    patch = review_patch(before, final, edits)
    if reconstruct_review_boxes(before, patch) != {n["node_id"]: row(n) for n in final["nodes"]}:
        raise ValueError("Verification patch does not reconstruct the fixed target")
    client = Client({**config["api"], "context_mode": "delta"}, original_tree=original)
    image = image_path(source, original)
    url = "data:image/png;base64,"+base64.b64encode(image.read_bytes()).decode()
    first, _ = client.messages(before, url, {"applied_rule_edits": prepare_ops,
        "rule_summary": {"large_region_checks": large_region_requirements(before, config.get("rules", {}))}}, client.task())
    client.history = first+[{"role": "assistant", "content": json.dumps(response, ensure_ascii=False, separators=(",", ":"))}]
    client.previous_tree = deepcopy(before)
    old = next(c for c in reversed(audit["calls"]) if c["phase"] == "verify" and "usage" in c)
    record = {"source": str(source), "method": "fixed_target_reuse_recorded_edit", "baseline": old,
        "current_nodes": len(final["nodes"]), "patch_reconstruction": "passed"}
    try:
        verdict, timing = client.call(final, image, {"review_operations": edits,
            "large_region_checks": large_region_requirements(final, config.get("rules", {})),
            "rejected_model_instructions": rejected}, verify=True)
        valid, _, errors = validate_large_region_review(final, config.get("rules", {}), verdict)
        record.update(delta=timing, verdict=verdict, status="accepted" if verdict["accepted"] and valid else "needs_review", validation_errors=errors)
    except ResponseError as error:
        record.update(delta=error.timing, status="invalid_response", error=str(error), model_content=error.content)
    except Exception as error:
        record.update(status="failed", error=str(error) if isinstance(error, ValueError) else type(error).__name__)
    return record


def main():
    """Run bounded, explicitly billable verification comparisons; never rewrite annotations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 32:
        parser.error("concurrency must be 1..32")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output must be empty")
    config = json.loads(args.config.read_text())
    files = sorted(p for p in args.baseline.rglob("layer-review.json") if p.with_name("hierarchy.json").exists())
    if not files:
        parser.error("No saved final trees")
    for file in files:
        audit = json.loads(file.read_text())
        old = next(c for c in reversed(audit["calls"]) if c["phase"] == "verify" and "usage" in c)
        if old.get("model") != config["api"]["model"]:
            parser.error("Configured model differs from recorded baseline")
    started = time.monotonic()
    records = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(benchmark, file, config) for file in files]
        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            write(args.output/(Path(record["source"]).parts[-3]+"-"+Path(record["source"]).parts[-2]+".json"), record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
    totals = {}
    for mode in ("baseline", "delta"):
        totals[mode] = {key: sum((r.get(mode, {}).get("usage") or {}).get(key, 0) for r in records) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    report = {"seconds": round(time.monotonic()-started, 3), "method": "fixed final trees; reuse recorded edits; one new verify call per sample",
        "totals": totals, "records": records}
    write(args.output/"token-comparison.json", report)


if __name__ == "__main__":
    main()
