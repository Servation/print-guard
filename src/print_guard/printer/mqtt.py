"""Live connection to the printer's local MQTT broker (TLS on port 8883).

The printer itself is the broker. Username is always "bblp" and the password is
the LAN access code. Status arrives on device/<serial>/report; commands go to
device/<serial>/request. This module keeps the merged status, reconnects on its
own, records command replies and lets callers wait for a state change, which is
how pause/stop are verified (a reply alone doesn't prove the printer paused).
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
import ssl
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from print_guard.printer import tls
from print_guard.printer.state import PrinterState, deep_merge
from print_guard.printer.tls import PrinterIdentityError, verify_identity

log = logging.getLogger(__name__)

USERNAME = "bblp"
# P1-series firmware slows down if asked for a full report too often.
MIN_PUSHALL_INTERVAL = 300.0
SILENCE_BEFORE_PUSHALL = 60.0
# While disconnected, probe the printer (no credentials) this often to explain why on the dashboard.
DIAGNOSE_EVERY = 60.0


@dataclass
class CommandReply:
    sequence_id: str
    command: str
    result: str
    reason: str
    err_code: int
    raw: dict

    @property
    def ok(self) -> bool:
        if self.err_code:
            return False
        return self.result.lower() in ("", "success", "ok")


class PrinterLink:
    def __init__(
        self,
        host: str,
        serial: str,
        access_code: str,
        tls_context: ssl.SSLContext,
        port: int = 8883,
        model: str = "P1S",
        verify_serial_after_connect: bool = False,
        probe_ca: str | None = None,
    ) -> None:
        self.host, self.port, self.serial, self.model = host, port, serial, model
        self._verify_after = verify_serial_after_connect
        self._probe_ca = probe_ca
        self._disconnected_since = time.time()
        self._raw: dict = {}
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._replies: dict[str, CommandReply] = {}
        self._seq = itertools.count(int(time.time()) % 100000 + 20000)
        self._last_message = 0.0
        self._last_pushall = 0.0
        self._connected = False
        self._ever_connected = False
        self._stopping = threading.Event()
        self.last_error = ""
        self.identity_error = False
        # "lan" or "cloud" from the printer's SSDP announcement, when heard (see runtime).
        self.announced_mode: str | None = None
        self.messages = 0
        self.listeners: list[Callable[[], None]] = []

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"print-guard-{uuid.uuid4().hex[:8]}",
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        self._client.username_pw_set(USERNAME, access_code)
        self._client.tls_set_context(tls_context)
        self._client.reconnect_delay_set(min_delay=2, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._watchdog = threading.Thread(target=self._watch, name="mqtt-watchdog", daemon=True)

    # Lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        log.info("connecting to printer MQTT at %s:%d", self.host, self.port)
        self._client.connect_async(self.host, self.port, keepalive=30)
        self._client.loop_start()
        self._watchdog.start()

    def stop(self) -> None:
        self._stopping.set()
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def ever_connected(self) -> bool:
        return self._ever_connected

    @property
    def last_message_at(self) -> float:
        return self._last_message

    # Topics --------------------------------------------------------------------
    @property
    def _report_topic(self) -> str:
        return f"device/{self.serial}/report"

    @property
    def _request_topic(self) -> str:
        return f"device/{self.serial}/request"

    # paho callbacks (run on paho's network thread) ------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            name = str(reason_code)
            if name != self.last_error:
                hint = ""
                if "authorized" in name.lower() or "password" in name.lower():
                    hint = " (wrong access code? It changes when LAN mode is toggled or after some firmware updates)"
                log.error("printer refused the MQTT connection: %s%s", name, hint)
            self.last_error = name
            return
        if self._verify_after:
            try:
                sock = client.socket()
                if isinstance(sock, ssl.SSLSocket):
                    verify_identity(sock, self.serial)
            except PrinterIdentityError as e:
                self.identity_error = True
                self.last_error = str(e)
                log.error("%s; disconnecting", e)
                client.disconnect()
                return
        self._connected = True
        self._ever_connected = True
        self.last_error = ""
        log.info("MQTT connected; subscribing to printer reports")
        client.subscribe(self._report_topic, qos=0)
        self._publish({"info": {"sequence_id": str(next(self._seq)), "command": "get_version"}})
        self.request_pushall(force=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        was = self._connected
        self._connected = False
        if was:
            self._disconnected_since = time.time()
        if was and not self._stopping.is_set():
            log.warning("MQTT disconnected (%s); reconnecting", reason_code)
        self._notify()

    def _on_message(self, client, userdata, message) -> None:
        try:
            data = json.loads(message.payload.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.debug("ignoring non-JSON message on %s", message.topic)
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        with self._lock:
            self._last_message = now
            self.messages += 1
            report = data.get("print")
            if isinstance(report, dict):
                command = report.get("command", "push_status")
                if command == "push_status":
                    deep_merge(self._raw, report)
                else:
                    self._record_reply(report)
            info = data.get("info")
            if isinstance(info, dict) and info.get("command") == "get_version":
                for module in info.get("module") or []:
                    if module.get("name") == "ota" and module.get("sw_ver"):
                        self._raw["_firmware"] = module["sw_ver"]
            self._changed.notify_all()
        self._notify()

    def _record_reply(self, report: dict) -> None:
        seq = str(report.get("sequence_id", ""))
        try:
            err = int(report.get("err_code") or 0)
        except (TypeError, ValueError):
            err = 0
        reply = CommandReply(
            sequence_id=seq,
            command=str(report.get("command", "")),
            result=str(report.get("result", "")),
            reason=str(report.get("reason", "")),
            err_code=err,
            raw=report,
        )
        self._replies[seq] = reply
        log.debug("command reply: %s result=%s err=%s", reply.command, reply.result, reply.err_code)

    def _notify(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                log.exception("status listener failed")

    # State -------------------------------------------------------------------------
    def snapshot(self) -> PrinterState:
        with self._lock:
            return PrinterState(raw=copy.deepcopy(self._raw), received_at=self._last_message, model=self.model)

    def wait_for(self, predicate: Callable[[PrinterState], bool], timeout: float) -> PrinterState | None:
        """Block until predicate(state) holds or the timeout passes. Returns the matching state."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                state = PrinterState(raw=copy.deepcopy(self._raw), received_at=self._last_message,
                                     model=self.model)
                if predicate(state):
                    return state
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._changed.wait(min(remaining, 1.0))

    def pop_reply(self, sequence_id: str) -> CommandReply | None:
        """The printer's reply to a command, if it has arrived (non-blocking)."""
        with self._lock:
            return self._replies.pop(sequence_id, None)

    # Requests ------------------------------------------------------------------------
    def _publish(self, payload: dict) -> bool:
        info = self._client.publish(self._request_topic, json.dumps(payload), qos=0)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.error("publishing to the printer failed: %s", mqtt.error_string(info.rc))
            return False
        return True

    def request_pushall(self, force: bool = False) -> None:
        """Ask for a full status report (rate limited; P1 firmware dislikes frequent pushall)."""
        now = time.time()
        if not force and now - self._last_pushall < MIN_PUSHALL_INTERVAL:
            return
        self._last_pushall = now
        self._publish({"pushing": {"sequence_id": str(next(self._seq)), "command": "pushall"}})

    def send_print_command(self, command: str, **fields) -> str | None:
        """Publish {"print": {"command": ...}}. Returns the sequence id, or None if not sent."""
        if not self._connected:
            log.error("cannot send %s: not connected to the printer", command)
            return None
        seq = str(next(self._seq))
        payload = {"print": {"sequence_id": seq, "command": command, "param": "", **fields}}
        return seq if self._publish(payload) else None

    # Watchdog ------------------------------------------------------------------------------
    def _watch(self) -> None:
        last_probe = 0.0
        while not self._stopping.wait(5.0):
            now = time.time()
            if self._connected and now - self._last_message > SILENCE_BEFORE_PUSHALL:
                self.request_pushall()
            if (not self._connected and now - self._disconnected_since > 30
                    and now - last_probe > DIAGNOSE_EVERY):
                last_probe = now
                self._diagnose()

    def _diagnose(self) -> None:
        """Explain a connection that isn't coming up, without sending the access code."""
        result = tls.probe(self.host, self.port, self._probe_ca)
        if not result.reachable:
            why = f"printer unreachable at {self.host}:{self.port} (off, asleep, or wrong IP?)"
        elif not result.verified:
            why = result.error
        elif result.common_name != self.serial:
            why = f"the device at {self.host} is a different printer (check printer.host)"
        else:
            return  # TLS is fine; the MQTT login error (if any) is already in last_error
        if why != self.last_error:
            log.warning("printer connection: %s", why)
        self.last_error = why
