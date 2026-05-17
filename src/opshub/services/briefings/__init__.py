"""Briefing service package (Phase 5 step B3, ADR-0015).

Splits the briefing service across two modules:

- :mod:`opshub.services.briefings.prompts` — inline Python prompt
  constants and the :func:`render_user_prompt` helper that wraps every
  external snippet in ``<source id="..." type="...">...</source>``
  delimiters (ADR-0015 §決定 (e) / (f)).
- :mod:`opshub.services.briefings.service` — :class:`BriefingService`,
  the operational orchestrator that assembles topic-relevant entities
  via :class:`~opshub.services.recall_service.RecallService`, calls the
  configured :class:`~opshub.llm.client.LLMClient`, and records the
  result via the standard event-sourced UoW pattern (event append +
  projection apply in one transaction).

The package layout mirrors :mod:`opshub.services.embedding_service`'s
"service + helpers" split kept inline; the briefing flow needs a small
prompt-template module so the injection-mitigation contract has its
own load-bearing test surface.
"""

from opshub.services.briefings.prompts import (
    SYSTEM_PROMPT,
    USER_PROMPT_PREAMBLE,
    render_user_prompt,
)
from opshub.services.briefings.service import Briefing, BriefingService

__all__ = [
    "SYSTEM_PROMPT",
    "USER_PROMPT_PREAMBLE",
    "Briefing",
    "BriefingService",
    "render_user_prompt",
]
