"""Slack Socket-Mode intake (SCRUM-60/61/62). See ``worker.py`` for the
transport + event-routing implementation, and ``intake.py`` for the
``app_mention`` handler (SCRUM-61); disabled by default via
``integrations.slack.intake`` (config.py DEFAULT_CONFIG)."""

from __future__ import annotations

from .intake import AppMentionHandler, extract_repo_token, parse_message
from .worker import SlackWorker

__all__ = ["SlackWorker", "AppMentionHandler", "extract_repo_token", "parse_message"]
