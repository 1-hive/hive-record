# SPDX-License-Identifier: GPL-3.0-or-later
"""Refusals (SPEC §12.2, §12.3)."""

from __future__ import annotations

RETRYABLE = frozenset({"PIN_UNAVAILABLE", "INTERNAL_ERROR"})


class Refusal(Exception):
    """A request that is not admitted. ``code`` is the contract; ``reason`` is for
    humans and must never be parsed."""

    def __init__(self, code: str, reason: str, **detail: object) -> None:
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE
