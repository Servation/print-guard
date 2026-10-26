"""Telling you something happened: Windows desktop notifications and optional ntfy phone push.

Sending never blocks the guard: each notice is delivered on a background thread,
and a failing channel is logged instead of interrupting monitoring.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from print_guard.config import NotifyConfig
from print_guard.credentials import redact, register_secret

log = logging.getLogger(__name__)

LEVEL_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass(frozen=True)
class Notice:
    level: str  # info | warning | critical
    title: str
    body: str
    image: Path | None = None


class Notifier:
    def __init__(self, cfg: NotifyConfig, app_name: str = "Print Guard") -> None:
        self.cfg = cfg
        self._channels = []
        if cfg.desktop:
            from print_guard.notify.desktop import DesktopChannel

            self._channels.append(DesktopChannel(app_name, alarm_sound=cfg.alarm_sound))
        if cfg.ntfy_topic.strip():
            from print_guard.notify.ntfy import NtfyChannel

            # The topic name is the only thing protecting an ntfy.sh topic; treat it as a secret.
            register_secret(cfg.ntfy_topic.strip())
            self._channels.append(NtfyChannel(cfg.ntfy_server, cfg.ntfy_topic.strip(), cfg.ntfy_min_level))
        self.sent = 0

    @property
    def channel_names(self) -> list[str]:
        return [c.name for c in self._channels]

    def send(self, notice: Notice, wait: bool = False) -> None:
        notice = Notice(notice.level, redact(notice.title), redact(notice.body),
                        notice.image if self.cfg.attach_snapshots else None)
        threads = []
        for channel in self._channels:
            if LEVEL_ORDER[notice.level] < LEVEL_ORDER.get(getattr(channel, "min_level", "info"), 0):
                continue
            t = threading.Thread(target=self._deliver, args=(channel, notice), daemon=True,
                                 name=f"notify-{channel.name}")
            t.start()
            threads.append(t)
        self.sent += 1
        if wait:
            for t in threads:
                t.join(timeout=30)

    @staticmethod
    def _deliver(channel, notice: Notice) -> None:
        try:
            channel.send(notice)
        except Exception as e:
            log.warning("%s notification failed: %s", channel.name, redact(str(e)))
