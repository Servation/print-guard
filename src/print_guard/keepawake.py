"""Keeps Windows from sleeping while the guard watches a print.

A sleeping PC can't see the printer or stop it, so `watch` asks Windows to stay
awake (the display may still turn off) until the guard exits. Uses the
documented SetThreadExecutionState API; no settings are changed.
"""

from __future__ import annotations

import contextlib
import logging
import sys

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def keep_awake():
    if sys.platform != "win32":
        yield False
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    ok = bool(kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    if ok:
        log.info("keeping Windows awake while watching (the display can still sleep)")
    else:
        log.warning("could not stop Windows from sleeping; check your power settings for long prints")
    try:
        yield ok
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
