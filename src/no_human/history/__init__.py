"""Extract and analyze Cascade/Windsurf IDE conversation history.  # term-ok: real IDE name

Used during onboarding to bootstrap the learning queue from a user's real
interaction patterns — failure modes, anti-patterns, and successful approaches
that the user has already encountered and corrected.
"""

from .ingester import IngestResult, TranscriptIngester

__all__ = ["TranscriptIngester", "IngestResult"]
