"""Windows toast notifications. Critical alerts break through Focus Assist and play
the looping alarm sound, so a failed print wakes you up instead of waiting silently."""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


class DesktopChannel:
    name = "desktop"
    min_level = "info"

    def __init__(self, app_name: str, alarm_sound: bool = True) -> None:
        self._app_name = app_name
        self._alarm = alarm_sound
        self._toaster = None
        self._broken = sys.platform != "win32"
        if self._broken:
            log.info("desktop notifications need Windows; disabled")

    def send(self, notice) -> None:
        if self._broken:
            return
        from windows_toasts import (
            AudioSource,
            Toast,
            ToastAudio,
            ToastDisplayImage,
            ToastDuration,
            ToastScenario,
            WindowsToaster,
        )

        if self._toaster is None:
            self._toaster = WindowsToaster(self._app_name)
        toast = Toast([notice.title, notice.body])
        if notice.level == "critical":
            toast.scenario = ToastScenario.Important
            toast.duration = ToastDuration.Long
            if self._alarm:
                toast.audio = ToastAudio(AudioSource.Alarm, looping=True)
        elif notice.level == "warning":
            toast.duration = ToastDuration.Long
            toast.audio = ToastAudio(AudioSource.Reminder)
        if notice.image and notice.image.exists():
            toast.AddImage(ToastDisplayImage.fromPath(notice.image))
        try:
            self._toaster.show_toast(toast)
        except Exception:
            self._toaster = None  # recreate next time; the notification platform can restart
            raise
