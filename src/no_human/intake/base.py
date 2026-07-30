"""Task intake: detect a source from a URL/id, fetch the record, normalize → Task.

Each adapter has a pure ``normalize(raw) -> Task`` (tested against real fixtures)
and a ``fetch_raw(ref) -> dict`` that hits the live source. Keeping normalize
pure means we verify field mapping against captured real data without network.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Protocol

from ..core.task import Task

# ----------------------------- source detection --------------------------- #

@dataclass
class SourceRef:
    kind: str           # github | gitlab | freeform
    ref: str            # "owner/repo#n" or issue url
    raw_input: str


def parse_source(text: str) -> SourceRef:
    """Classify a URL or bare id into a source + a normalized reference."""
    s = text.strip()

    if "/issues/" in s or "/-/issues/" in s:
        if "gitlab" in s:
            return SourceRef("gitlab", s, text)
        # github.com or GitHub Enterprise (e.g. code.example.com)
        return SourceRef("github", s, text)

    return SourceRef("freeform", s, text)


# ------------------------------- HTML helpers ------------------------------ #


def clean_html(value: str | None) -> str:
    """Strip tags + unescape entities + collapse whitespace."""
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def html_list_items(value: str | None) -> list[str]:
    """Extract `<li>` items from an HTML ordered/unordered list as clean strings.

    Falls back to newline-splitting when the field isn't HTML (some sources
    store acceptance criteria as plain numbered text).
    """
    if not value:
        return []
    items = re.findall(r"<li[^>]*>(.*?)</li>", value, re.S | re.I)
    if items:
        return [clean_html(i) for i in items if clean_html(i)]
    # plain text: split on newlines / numbered bullets
    lines = [clean_html(ln) for ln in re.split(r"[\r\n]+", value)]
    out = []
    for ln in lines:
        ln = re.sub(r"^\s*(\d+[.)]|[-*])\s*", "", ln).strip()
        if ln:
            out.append(ln)
    return out


def dv(field: Any) -> str:
    """Read a tracker field that may be a {value, display_value} dict or scalar."""
    if isinstance(field, dict):
        return str(field.get("display_value") or field.get("value") or "")
    return "" if field is None else str(field)


# ------------------------------- adapter API ------------------------------- #


class IntakeAdapter(Protocol):
    kind: str

    def fetch_raw(self, ref: str) -> dict[str, Any]:
        """Fetch the source record. May hit the network / a CLI."""
        ...

    def normalize(self, raw: dict[str, Any]) -> Task:
        """Pure: map a fetched record onto an internal Task."""
        ...


def ingest(ref: SourceRef, adapter: IntakeAdapter) -> Task:
    raw = adapter.fetch_raw(ref.ref)
    return adapter.normalize(raw)
