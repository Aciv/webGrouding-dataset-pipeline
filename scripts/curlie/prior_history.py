"""Import prior Curlie host exclusions without copying outputs or completion counts."""
import json
from pathlib import Path
from urllib.parse import urlsplit


def normalize_host(value):
    """Use the candidate pool's lower-case, www-insensitive host identity."""
    host = urlsplit(value if "://" in value else "https://" + value).hostname
    if not host:
        raise ValueError(f"Invalid history host: {value}")
    return host.lower().removeprefix("www.")


def load_excluded_hosts(paths):
    """Read all claimed hosts (including failures) and inherited exclusions once.

    Only collection indices are required; training files may have been moved.
    Missing or malformed history fails explicitly rather than silently recollecting.
    """
    hosts = set()
    for supplied in paths:
        file = Path(supplied)
        if file.is_dir():
            file /= "collection-index.json"
        index = json.loads(file.read_text())
        if index.get("schema") != "curlie_page_target_v1" or not isinstance(index.get("batches"), list):
            raise ValueError(f"Not a Curlie collection index: {file}")
        hosts.update(normalize_host(host) for host in index.get("excluded_hosts", []))
        for batch in index["batches"]:
            for candidate in batch["candidates"]:
                value = candidate.get("host") or candidate.get("url")
                if not value:
                    raise ValueError(f"History candidate has no host/URL: {file}")
                hosts.add(normalize_host(value))
    return sorted(hosts)
