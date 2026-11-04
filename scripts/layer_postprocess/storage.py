"""Atomic derived JSON output and canonical screenshot validation; source remains read-only."""
import json
import struct

def write(file, value):
    """Atomically publish UTF-8 JSON within the derived output only."""
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_suffix(file.suffix+".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n")
    temp.replace(file)


def image_path(file, tree):
    """Resolve a local canonical screenshot and verify its declared PNG dimensions."""
    name = tree["capture"]["image"]
    if name not in ("full.png", "initial.png"):
        raise ValueError("capture.image must be full.png or initial.png")
    image = (file.parent/name).resolve()
    if image.parent != file.parent.resolve():
        raise ValueError("Screenshot must remain in its observation directory")
    with image.open("rb") as handle:
        header = handle.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or len(header) != 24:
        raise ValueError("Invalid PNG header")
    if struct.unpack(">II", header[16:24]) != (tree["capture"]["width"], tree["capture"]["height"]):
        raise ValueError("Screenshot dimensions disagree with hierarchy")
    return image


