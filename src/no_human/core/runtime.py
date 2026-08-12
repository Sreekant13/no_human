"""The ONE construction site for the coder orchestrator.

Both `nh` (CLI/TUI) and the FastAPI server build the coder `Orchestrator`
through `build_orchestrator` so `worker.backend` cannot diverge between the
two entry points (audit A8/X2, 2026-08-11): the server used to hardcode
`ClaudeBackend` in its own closure, so a task run through the GUI ignored
`worker.backend` while the same task via `nh` honoured it.
"""

from __future__ import annotations

from typing import Any

from ..agent.backend import make_backend
from ..context import ContextGatherer, build_default_sources
from ..notify import build_notifier
from .db import Store
from .orchestrator import Orchestrator


def build_orchestrator(config, store: Store, *, event_sink: Any = None,
                        task: Any = None) -> Orchestrator:
    # THE ONE SWITCH. `make_backend` returns exactly the ClaudeBackend this
    # line used to construct — same class, same arguments — unless
    # `worker.backend` says otherwise. The orchestrator below is handed a
    # `CodingBackend` and cannot tell which it got.
    backend = make_backend(
        model=config.primary_model,
        config=config.data,
        role="coder",
        forbidden_paths=config["safety"]["forbidden_paths"],
        never_push_to=config["git"]["never_push_to"],
    )
    review_backend = None  # reviewer defaults to ClaudeBackend(readonly=True)
    # Fan-out over every configured notify-OUT channel (Slack + Teams). One
    # source of truth for which channels are live: notify.build_notifier.
    notifier = build_notifier(config.data)
    gatherer = ContextGatherer(build_default_sources(store, config.data))
    from ..learning import LearningQueue
    from ..review.reviewer import AdversarialReviewer
    reviewer = AdversarialReviewer.from_config(config.data, backend=review_backend)
    return Orchestrator(store, config.data, backend, notifier,
                        event_sink=event_sink, context_gatherer=gatherer,
                        learning_queue=LearningQueue(store),
                        reviewer=reviewer)
