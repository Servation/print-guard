"""Push notifications to your phone through ntfy (https://ntfy.sh or a self-hosted server).

In LAN Only mode the Bambu Handy app can't reach the printer, so this is the way
to hear about a failure when you're away from the PC. Install the ntfy app,
subscribe to a long random topic name, and put the same name in the config.
"""

from __future__ import annotations

import json
import unicodedata
import urllib.request

PRIORITY = {"info": 3, "warning": 4, "critical": 5}
TAGS = {"info": "white_check_mark", "warning": "warning", "critical": "rotating_light"}


def _ascii(text: str) -> str:
    """HTTP headers must be Latin-1; strip what can't be represented."""
    text = text.replace("°", " deg")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


class NtfyChannel:
    name = "ntfy"

    def __init__(self, server: str, topic: str, min_level: str = "warning") -> None:
        self._server = server.rstrip("/")
        self._topic = topic
        self.min_level = min_level

    def send(self, notice) -> None:
        if notice.image and notice.image.exists():
            # Attachment upload: the body is the image, text goes in headers.
            request = urllib.request.Request(
                f"{self._server}/{self._topic}",
                data=notice.image.read_bytes(),
                method="PUT",
                headers={
                    "Title": _ascii(notice.title),
                    "Message": _ascii(notice.body)[:3000],
                    "Priority": str(PRIORITY[notice.level]),
                    "Tags": TAGS[notice.level],
                    "Filename": notice.image.name,
                },
            )
        else:
            payload = {
                "topic": self._topic,
                "title": notice.title,
                "message": notice.body[:4000],
                "priority": PRIORITY[notice.level],
                "tags": [TAGS[notice.level]],
            }
            request = urllib.request.Request(
                self._server,
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status >= 300:
                raise RuntimeError(f"ntfy answered HTTP {response.status}")
