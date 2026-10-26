"""Logging: a full log file per run, plus either plain console output or an
in-memory tail that the live dashboard shows. Every handler redacts secrets."""

from __future__ import annotations

import collections
import contextlib
import logging
import sys
import threading
from pathlib import Path

from print_guard.credentials import RedactingFilter

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
SHORT_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


class TailHandler(logging.Handler):
    """Keeps the most recent records for the dashboard."""

    def __init__(self, size: int = 300) -> None:
        super().__init__()
        self.lines: collections.deque[tuple[int, str]] = collections.deque(maxlen=size)
        self._lock2 = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            text = str(record.msg)
        with self._lock2:
            self.lines.append((record.levelno, text))

    def tail(self, n: int) -> list[tuple[int, str]]:
        with self._lock2:
            return list(self.lines)[-n:]


def setup(log_file: Path | None = None, console: bool = True, tail: TailHandler | None = None,
          verbose: bool = False) -> None:
    # Windows consoles default to a legacy code page; status text includes °C and the like.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    redactor = RedactingFilter()
    handlers: list[logging.Handler] = []
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(FORMAT))
        handlers.append(fh)
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter(SHORT_FORMAT, datefmt="%H:%M:%S"))
        handlers.append(ch)
    if tail:
        tail.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        handlers.append(tail)
    for h in handlers:
        h.addFilter(redactor)
        root.addHandler(h)
    for noisy in ("paho", "anthropic", "httpx", "httpx2", "httpcore", "urllib3", "windows_toasts"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
