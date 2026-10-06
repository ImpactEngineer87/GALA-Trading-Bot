"""Application release identifier, read once when the server process starts."""
import re
from pathlib import Path


def read_app_version(path):
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "unknown"
    return value if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", value) else "unknown"


APP_VERSION = read_app_version(Path(__file__).resolve().parents[1] / "VERSION")
