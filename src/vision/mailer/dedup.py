"""Send-dedup: suppress a duplicate approval email within a window (BRD §14.5).

WHY this module exists: the daily job (or a manual re-run, or a cron overlap)
must not email the owner the *same* approval twice. finalert hit exactly this —
identical alerts fired minutes apart during a restart storm — and solved it with
a persisted, atomically-written state file keyed by subject. We adapt that here:
one approval email per key per window (default: a day), so re-running today's job
is idempotent from the owner's inbox perspective.

Design carried over from finalert (and hardened):
  * **check / mark split** — ``is_suppressed`` never records; the caller calls
    ``mark_sent`` ONLY after the provider accepts the send. This closes the
    silent-drop bug (mark-before-send would suppress the retry of a send that
    actually failed).
  * **atomic write** — write a temp file then ``os.replace`` (atomic rename) so a
    crash mid-write can never corrupt the state or half-persist a mark.
  * **self-pruning load** — entries older than the window are dropped on load so
    the file cannot grow without bound.

State is a flat JSON object ``{key: last_sent_epoch}`` — trivial to inspect and
portable across dev (JSON file) without needing a DB row.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# One approval email per key per day: a same-day re-run is suppressed, but the
# NEXT day's genuinely-new draft (whose key includes the date) sends normally.
DEFAULT_WINDOW_SECS = 86_400  # 24h


class SendDeduper:
    """Persisted, atomic, windowed dedup of email sends keyed by an arbitrary string.

    Instances are cheap and thread-safe (a lock guards the in-memory cache and the
    file). The ``key`` is chosen by the caller — for the daily approval email the
    natural key is the subject (which embeds focus + date), so an identical
    subject on the same day is treated as the same send.
    """

    def __init__(self, state_path: Path, window_secs: int = DEFAULT_WINDOW_SECS) -> None:
        self._path = state_path
        self._window = window_secs
        self._lock = threading.Lock()
        # Cache is loaded lazily on first use so constructing a deduper never
        # touches disk (keeps tests and imports side-effect free).
        self._cache: dict[str, float] | None = None

    def _load(self) -> dict[str, float]:
        """Load state from disk once, pruning entries older than the window.

        WHY prune on load: it bounds the file size and means a stale key can
        never suppress a fresh send after its window has elapsed. A missing or
        unreadable file yields an empty cache (fail-open for *reads* is safe here
        — the worst case is sending one extra email, never dropping one).
        """
        if self._cache is not None:
            return self._cache

        cache: dict[str, float] = {}
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # First run: no state yet — an empty cache is correct, not an error.
            self._cache = cache
            return cache
        except OSError as exc:
            log.debug("dedup state unreadable (%s); starting empty.", exc.__class__.__name__)
            self._cache = cache
            return cache

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            # A corrupt file must not crash the mailer; treat as empty and let the
            # next successful mark_sent rewrite it cleanly.
            log.warning("dedup state file was corrupt; ignoring and rebuilding.")
            self._cache = cache
            return cache

        if isinstance(parsed, dict):
            cutoff = time.time() - self._window
            for key, value in parsed.items():
                # Keep only well-typed, still-in-window entries.
                if isinstance(value, (int, float)) and float(value) >= cutoff:
                    cache[str(key)] = float(value)
        self._cache = cache
        return cache

    def is_suppressed(self, key: str, *, now: float | None = None) -> bool:
        """Return ``True`` if ``key`` was sent within the window (does NOT record).

        Purely a read: the caller must call :meth:`mark_sent` after a successful
        send. Splitting check from mark is what prevents a failed send from
        suppressing its own retry.
        """
        current = time.time() if now is None else now
        with self._lock:
            cache = self._load()
            last = cache.get(key)
            return last is not None and (current - last) < self._window

    def mark_sent(self, key: str, *, now: float | None = None) -> None:
        """Record that ``key`` was just sent, persisting atomically.

        Call ONLY after the provider accepted the send, so a failed delivery
        never leaves a mark that would suppress the retry.
        """
        current = time.time() if now is None else now
        with self._lock:
            cache = self._load()
            # Immutable-style update: build the next state, then persist it.
            next_state = {**cache, key: current}
            self._persist(next_state)
            self._cache = next_state

    def _persist(self, state: dict[str, float]) -> None:
        """Atomically write ``state`` to disk (temp file + rename).

        WHY temp + ``os.replace``: ``os.replace`` is atomic on both POSIX and
        Windows, so a reader never observes a partially-written file and a crash
        mid-write leaves the previous good state intact.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            # Persistence failure must not crash the send path; the email already
            # went out. Log so a permanently-unwritable state dir is visible.
            log.warning("could not persist dedup state (%s); dedup may not survive restart.", exc.__class__.__name__)
