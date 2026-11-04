"""Streaming TSV reader shared by fixed-size sampling and the unlimited candidate pool."""
import csv
import tarfile


def rows(archive, suffix):
    """Read regular tar members without extracting paths or loading a TSV into RAM."""
    with tarfile.open(archive, "r|gz") as bundle:
        for member in bundle:
            if member.isfile() and member.name.endswith(suffix):
                stream = bundle.extractfile(member)
                text = (line.decode("utf-8-sig", errors="replace") for line in stream)
                yield from csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE)
