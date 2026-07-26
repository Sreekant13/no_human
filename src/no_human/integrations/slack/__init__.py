"""Slack Socket-Mode intake (SCRUM-60/61/62). See ``worker.py`` for the
transport + event-routing implementation; disabled by default via
``integrations.slack.intake`` (config.py DEFAULT_CONFIG)."""

from __future__ import annotations

from .worker import SlackWorker

__all__ = ["SlackWorker"]
