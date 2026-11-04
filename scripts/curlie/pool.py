"""Immutable SQLite candidate pool built from Curlie's original categorized TSVs."""
import random
import sqlite3
from pathlib import Path
from archive import rows

DEFAULT_CATEGORIES = ["Arts", "Business", "Computers", "Games", "Health", "Home", "News",
                      "Recreation", "Reference", "Science", "Shopping", "Society", "Sports"]


def build_pool(archive, destination):
    """Index all eligible English topic entries; do not cap the pool at 2,000 sites."""
    from urllib.parse import urlsplit
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".building.sqlite")
    temporary.unlink(missing_ok=True)
    categories = {}
    for row in rows(archive, "-s.tsv"):
        if len(row) >= 2:
            label = row[1].removeprefix("Top/")
            if label.split("/")[0] in DEFAULT_CATEGORIES:
                categories[row[0]] = label
    rng = random.Random(42)
    with sqlite3.connect(temporary) as db:
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, host TEXT, url TEXT, category TEXT, category_path TEXT, title TEXT, description TEXT, priority REAL, UNIQUE(host,category))")
        batch = []
        for row in rows(archive, "-c.tsv"):
            if len(row) < 4 or row[3] not in categories:
                continue
            try:
                url = urlsplit(row[0])
                if url.scheme not in ("http", "https") or not url.hostname or url.username:
                    continue
                host = url.hostname.lower().removeprefix("www.")
            except ValueError:
                continue
            label = categories[row[3]]
            batch.append((host, row[0], label.split("/")[0], label, row[1], row[2], rng.random()))
            if len(batch) >= 2000:
                db.executemany("INSERT OR IGNORE INTO candidates(host,url,category,category_path,title,description,priority) VALUES(?,?,?,?,?,?,?)", batch)
                batch.clear()
        db.executemany("INSERT OR IGNORE INTO candidates(host,url,category,category_path,title,description,priority) VALUES(?,?,?,?,?,?,?)", batch)
        db.execute("CREATE INDEX category_queue ON candidates(category,priority,id)")
        db.execute("INSERT INTO metadata VALUES('schema','curlie_candidate_pool_v1')")
        db.execute("INSERT INTO metadata VALUES('archive',?)", (str(Path(archive).resolve()),))
        db.commit()
    temporary.replace(destination)


class CandidatePool:
    """Read-only per-category queues; consumed offsets live in the run checkpoint."""
    def __init__(self, file):
        self.file = Path(file).resolve()
        self.db = sqlite3.connect(self.file.as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        schema = self.db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if not schema or schema[0] != "curlie_candidate_pool_v1":
            raise ValueError("Unsupported Curlie pool")
        self.counts = dict(self.db.execute("SELECT category,count(*) FROM candidates GROUP BY category"))

    def next(self, category, offsets, seen):
        """Consume unseen normalized hosts, preserving exact Curlie URLs and labels."""
        offset = offsets.get(category, 0)
        cursor = self.db.execute("SELECT * FROM candidates WHERE category=? ORDER BY priority,id LIMIT -1 OFFSET ?", (category, offset))
        for row in cursor:
            offsets[category] = offsets.get(category, 0) + 1
            if row["host"] not in seen:
                item = dict(row)
                item["id"] = f"curlie-{item['id']:08d}"
                return item
        return None

    def close(self):
        """Release the local candidate database; no per-run status is stored here."""
        self.db.close()
