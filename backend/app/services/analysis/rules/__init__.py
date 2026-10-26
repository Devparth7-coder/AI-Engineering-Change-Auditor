"""Rule registry.

Add a rule by importing its module and appending to ``ALL_RULES``.  Each entry
is ``(rule_id, description, evaluate_fn)``.
"""

from __future__ import annotations

from collections.abc import Callable

from . import arithmetic, assumptions, boundary, coverage, error_handling, validation
from .base import (
    Evidence,
    FileContext,
    FindingDraft,
    RepairHint,
    RuleContext,
    confidence,
)

ALL_RULES: list[tuple[str, str, Callable[[RuleContext], list[FindingDraft]]]] = [
    (arithmetic.RULE_ID, arithmetic.DESCRIPTION, arithmetic.evaluate),
    (validation.RULE_ID, validation.DESCRIPTION, validation.evaluate),
    (boundary.RULE_ID, boundary.DESCRIPTION, boundary.evaluate),
    (error_handling.RULE_ID, error_handling.DESCRIPTION, error_handling.evaluate),
    (assumptions.ASSUME_RULE_ID, assumptions.DESCRIPTION, assumptions.evaluate),
    (assumptions.LEAK_RULE_ID, assumptions.DESCRIPTION, assumptions.evaluate),
    (coverage.RULE_ID, coverage.DESCRIPTION, coverage.evaluate),
]

RULE_IDS = [r[0] for r in ALL_RULES]

__all__ = [
    "ALL_RULES",
    "Evidence",
    "FileContext",
    "FindingDraft",
    "RULE_IDS",
    "RepairHint",
    "RuleContext",
    "confidence",
]
