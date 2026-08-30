"""Who the thing we are about to send is addressed to.

The single place that question is answered. It used to be answered in two —
the event queue and the live score message — and they drifted apart: the queue
knew about the pause, the live message did not, so someone who pressed
`/pause` kept receiving the score as the map went on. The rule "check it in
two places" is unenforceable; the right conclusion is to have one place.

The other half of the same defect lives here too: the "this match has no team
links" branch used to hand back the chat from the config directly, bypassing
both the subscriber list and the pause.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# (chat, that chat's teams in the match). An empty team list means "show it
# from the match's own point of view": either the match is not linked to any
# team yet, or this is single-user mode.
Audience = List[Tuple[str, List[int]]]


def active_subscribers(storage) -> List[str]:
    """Subscribers we may write to right now: enabled and not paused."""
    return [chat for chat in storage.subscriber_ids()
            if not storage.subscriber_paused(chat)]


def _fallback(storage, config) -> Audience:
    """Single-user mode: there are no subscribers at all, so write to the chat
    from the config.

    If there are subscribers but all of them are quiet, return nothing: that is
    what the pause means. This used to be an unconditional `config.chat_id`,
    and the owner received notifications after putting themselves on `/pause`.
    """
    if storage.subscribers():
        return []
    return [(config.main_chat_id, [])]


def service_audience(storage, config) -> Audience:
    """Service alerts (degradation, recovery) — to everyone who is listening.

    They are not about teams but about the service having gone blind, so they
    are not mutable per type. They are subject to the pause, though: the person
    asked for quiet.
    """
    subscribers = active_subscribers(storage)
    if not subscribers:
        return _fallback(storage, config)
    return [(chat, []) for chat in subscribers]


def match_audience(storage, config, match_id: int, *,
                   teams: Optional[Sequence[int]] = None) -> Audience:
    """Who cares about this match, and from which of its teams to show it.

    `teams` overrides the participants — that is how a multikill is addressed:
    it only concerns those who follow the player's own team.
    """
    subscribers = active_subscribers(storage)
    if not subscribers:
        return _fallback(storage, config)

    if teams is None:
        teams = storage.match_team_ids(match_id)
    if not teams:
        # The match is not linked to any team yet — that is what the database
        # looks like right after an upgrade, until the team page is polled
        # again. Show it to everyone who is listening: that is more honest than
        # handing it to the config owner past both the subscriber list and the
        # pause.
        return [(chat, []) for chat in subscribers]

    allowed = set(subscribers)
    by_chat: dict = {}
    for team_id in teams:
        for chat in storage.subscribers_tracking(team_id):
            if chat in allowed:
                by_chat.setdefault(chat, []).append(team_id)
    return list(by_chat.items())
