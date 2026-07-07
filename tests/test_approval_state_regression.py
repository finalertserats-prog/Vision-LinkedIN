"""Regression guard for the CLI↔service awaiting-state mismatch.

Root cause (caught in the first LIVE approval test, 2026-07-08): the vision-daily
and vision-council CLIs create drafts in state ``pending_approval`` (BRD §10.4),
but the approval service's allowed-from guards only admitted ``new`` — so EVERY
real approval click failed with a StateConflict. The unit tests seeded ``new``,
so it hid until a genuine CLI-created draft was approved end-to-end.

This test ties the two together: the state the state-machine calls "awaiting the
owner's decision" MUST be actionable by every approval action.
"""

from __future__ import annotations

from vision.approval import service
from vision.approval.state_machine import DraftState


def test_pending_approval_is_actionable_by_every_approval_action() -> None:
    # The canonical awaiting-decision state per BRD §10.4 (what the CLIs create).
    awaiting = DraftState.PENDING_APPROVAL.value

    # Every action a fresh, awaiting draft offers must fire from that state.
    assert awaiting in service._APPROVE_FROM
    assert awaiting in service._POST_NOW_FROM
    assert awaiting in service._REJECT_FROM
    assert awaiting in service._EDIT_FROM
