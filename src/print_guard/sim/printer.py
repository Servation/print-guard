"""A fake Bambu Lab P1S on localhost, for testing print-guard without a real printer.

It speaks the same protocols print-guard uses with the real machine:
  * an MQTT 3.1.1 broker over TLS that checks the bblp/access-code login, sends
    P1-style delta reports, answers pushall/get_version, and obeys (or, in the
    "locked" scenario, rejects like Developer Mode off) pause/resume/stop;
  * the port-6000-style camera stream: TLS, 80-byte login, 16-byte header + JPEG.

Scenarios script a failure partway through a short print. Only the subset of
MQTT needed here is implemented (QoS 0/1 publish, subscribe, ping, disconnect).
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from print_guard.sim import frames
from print_guard.sim.certs import SimCerts, make_certs

log = logging.getLogger("print_guard.sim")

SCENARIOS = {
    "clean": "a short print that finishes normally",
    "spaghetti": "the part detaches and spaghetti piles up (camera)",
    "runout": "the AMS runs out of filament; the printer pauses itself",
    "overtemp": "the nozzle overheats well above its target",
    "hms-fatal": "the printer raises a fatal heatbed error but keeps going",
    "stall": "the layer counter stops advancing while RUNNING",
    "locked": "spaghetti, but the printer rejects commands (Developer Mode off)",
    "dark": "the chamber light is off, so the camera sees nothing",
}

AUTH_ERR = 84033543
CANCELED = 0x0300400C


@dataclass
class SimSettings:
    serial: str = "01P09SIM0000042"
    access_code: str = "simcode1"
    scenario: str = "spaghetti"
    total_layers: int = 150
    seconds_per_layer: float = 1.5
    failure_layer: int = 20
    prepare_seconds: float = 4.0
    frame_interval: float = 0.5
    cert_dir: Path | None = None


@dataclass
class _Client:
    writer: asyncio.StreamWriter
    subscriptions: set[str] = field(default_factory=set)


def _encode_len(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _mqtt_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def publish_packet(topic: str, payload: bytes) -> bytes:
    body = _mqtt_str(topic) + payload
    return b"\x30" + _encode_len(len(body)) + body


class FakePrinter:
    def __init__(self, settings: SimSettings, workdir: Path) -> None:
        self.s = settings
        self.workdir = workdir
        self.certs: SimCerts = make_certs(settings.cert_dir or workdir / "certs", settings.serial)
        self.mqtt_port = 0
        self.camera_port = 0
        self.commands: list[str] = []
        self.rejected: list[str] = []
        self.logins_rejected = 0
        self._clients: list[_Client] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="fake-printer", daemon=True)
        self._ready = threading.Event()
        self._t0 = 0.0
        self._failure_started: float | None = None
        self._stalled_layer: int | None = None
        self._layer_clock = 0.0
        self._failure_at_layer = settings.failure_layer
        self.state = self._initial_state()

    # Public ---------------------------------------------------------------------------------
    @property
    def locked(self) -> bool:
        return self.s.scenario == "locked"

    def start(self) -> None:
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        if not self._ready.wait(10):
            raise RuntimeError("fake printer failed to start")

    def stop(self) -> None:
        async def close():
            for c in list(self._clients):
                c.writer.close()
            for server in self._servers:
                server.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            asyncio.run_coroutine_threadsafe(close(), self._loop).result(5)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)

    # State ----------------------------------------------------------------------------------------
    def _initial_state(self) -> dict:
        # The fun bit 29 tells clients whether commands must be signed (Developer Mode off).
        fun = "3EC1AFFF9CFF" if self.locked else "3EC18FFF9CFF"
        return {
            "command": "push_status", "msg": 0, "sequence_id": "0",
            "gcode_state": "PREPARE", "stg_cur": 2, "mc_print_stage": "1",
            "subtask_name": f"sim_{self.s.scenario}", "gcode_file": "/data/Metadata/plate_1.gcode",
            "mc_percent": 0, "mc_remaining_time": int(self.s.total_layers * self.s.seconds_per_layer / 60) + 1,
            "layer_num": 0, "total_layer_num": self.s.total_layers,
            "nozzle_temper": 30.0, "nozzle_target_temper": 220.0, "bed_temper": 28.0, "bed_target_temper": 55.0,
            "chamber_temper": 27.0, "cooling_fan_speed": "15", "heatbreak_fan_speed": "15",
            "print_error": 0, "hms": [], "fun": fun, "print_type": "local",
            "lights_report": [{"node": "chamber_light", "mode": "off" if self.s.scenario == "dark" else "on"}],
            "ams": {"ams": [{"id": "0", "humidity": "4", "temp": "26.0", "tray": [
                {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 6,
                 "tray_sub_brands": "PLA Basic"},
                {"id": "1", "tray_type": "PLA", "tray_color": "F5A3C7FF", "remain": 80,
                 "tray_sub_brands": "PLA Basic"},
                {"id": "2"}, {"id": "3"}]}], "tray_now": "0", "ams_exist_bits": "1"},
        }

    def _delta(self, **changes) -> None:
        self.state.update(changes)
        self._broadcast({"print": {"command": "push_status", "msg": 1, **changes}})

    def _tick(self) -> None:
        now = time.monotonic()
        st = self.state
        elapsed = now - self._t0
        if st["gcode_state"] == "PREPARE":
            st["nozzle_temper"] = min(220.0, 30 + elapsed * 60)
            st["bed_temper"] = min(55.0, 28 + elapsed * 10)
            self._delta(nozzle_temper=round(st["nozzle_temper"], 1), bed_temper=round(st["bed_temper"], 1))
            if elapsed >= self.s.prepare_seconds:
                self._layer_clock = now
                self._delta(gcode_state="RUNNING", stg_cur=0, mc_print_stage="2", layer_num=1,
                            nozzle_temper=220.0, bed_temper=55.0)
            return
        if st["gcode_state"] != "RUNNING":
            return
        layer = st["layer_num"]
        scenario = self.s.scenario
        hit = layer >= self._failure_at_layer
        if hit and scenario in ("spaghetti", "locked") and self._failure_started is None:
            self._failure_started = now
            log.info("sim: spaghetti starts at layer %d", layer)
        if hit and scenario == "runout" and st["stg_cur"] != 6:
            log.info("sim: AMS runout at layer %d; printer pauses itself", layer)
            self._delta(gcode_state="PAUSE", stg_cur=6, print_error=0x07008011,
                        hms=[{"attr": 0x07002000, "code": 0x00020001}])
            return
        if hit and scenario == "hms-fatal" and not st["hms"]:
            log.info("sim: fatal heatbed error at layer %d (printer keeps running)", layer)
            self._delta(hms=[{"attr": 0x03000100, "code": 0x00010001}])
        if hit and scenario == "stall" and self._stalled_layer is None:
            self._stalled_layer = layer
            log.info("sim: layer counter freezes at %d", layer)
        nozzle = 220.0 + (0.6 if int(now) % 2 else -0.4)
        if hit and scenario == "overtemp":
            nozzle = min(260.0, st["nozzle_temper"] + 3.0)
        changes = {"nozzle_temper": round(nozzle, 1), "bed_temper": 55.0 + (0.2 if int(now) % 3 else -0.1)}
        advanced = False
        # Ticks are 1 s apart; layers can be shorter, so catch up on every layer that elapsed.
        while self._stalled_layer is None and now - self._layer_clock >= self.s.seconds_per_layer:
            self._layer_clock += self.s.seconds_per_layer
            layer += 1
            advanced = True
        if advanced:
            if layer > self.s.total_layers:
                self._delta(gcode_state="FINISH", stg_cur=255, mc_percent=100, mc_remaining_time=0)
                log.info("sim: print finished")
                return
            pct = int(layer * 100 / self.s.total_layers)
            changes.update(layer_num=layer, mc_percent=pct,
                           mc_remaining_time=int((self.s.total_layers - layer) * self.s.seconds_per_layer / 60))
        self._delta(**changes)

    def user_resume(self, clear_mess: bool = False, fail_again_after_layers: int | None = None) -> None:
        """Simulate someone pressing Resume on the printer's own screen, optionally after
        removing the mess, and optionally with a new failure a few layers later."""
        def resume() -> None:
            if self.state["gcode_state"] == "PAUSE":
                if clear_mess:
                    self._failure_started = None
                if fail_again_after_layers is not None:
                    self._failure_at_layer = self.state["layer_num"] + fail_again_after_layers
                self._layer_clock = time.monotonic()
                self._delta(gcode_state="RUNNING", stg_cur=0)
        self._loop.call_soon_threadsafe(resume)

    # Commands -------------------------------------------------------------------------------------------
    def _handle_request(self, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        if "pushing" in data:
            self._broadcast({"print": {**self.state, "command": "push_status", "msg": 0}})
            return
        if "info" in data and data["info"].get("command") == "get_version":
            self._broadcast({"info": {"command": "get_version", "sequence_id": data["info"].get("sequence_id"),
                                      "module": [{"name": "ota", "sw_ver": "01.10.00.00"}]}})
            return
        req = data.get("print") or {}
        command, seq = req.get("command"), str(req.get("sequence_id", ""))
        if command not in ("pause", "resume", "stop"):
            return
        self.commands.append(command)
        if self.locked:
            self.rejected.append(command)
            log.info("sim: rejecting unsigned %s (Developer Mode off)", command)
            self._broadcast({"print": {"command": command, "sequence_id": seq, "result": "fail",
                                       "reason": "mqtt message verify failed", "err_code": AUTH_ERR}})
            self._delta(hms=[{"attr": 0x05000500, "code": 0x00010007}])
            return
        log.info("sim: %s accepted", command)
        self._broadcast({"print": {"command": command, "sequence_id": seq, "result": "success", "reason": ""}})
        if command == "pause" and self.state["gcode_state"] == "RUNNING":
            self._delta(gcode_state="PAUSE", stg_cur=16)
        elif command == "resume" and self.state["gcode_state"] == "PAUSE":
            self._failure_started = None
            self._layer_clock = time.monotonic()
            self._delta(gcode_state="RUNNING", stg_cur=0, hms=[], print_error=0)
        elif command == "stop" and self.state["gcode_state"] in ("RUNNING", "PAUSE", "PREPARE"):
            self._delta(gcode_state="FAILED", stg_cur=255, print_error=CANCELED, nozzle_target_temper=0.0,
                        bed_target_temper=0.0)

    # Networking -------------------------------------------------------------------------------------------
    def _server_ctx(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.certs.cert_file, self.certs.key_file)
        return ctx

    async def _serve(self) -> None:
        ctx = self._server_ctx()
        mqtt_server = await asyncio.start_server(self._mqtt_client, "127.0.0.1", 0, ssl=ctx)
        camera_server = await asyncio.start_server(self._camera_client, "127.0.0.1", 0, ssl=ctx)
        self._servers = [mqtt_server, camera_server]
        self.mqtt_port = mqtt_server.sockets[0].getsockname()[1]
        self.camera_port = camera_server.sockets[0].getsockname()[1]
        self._t0 = time.monotonic()
        self._loop.create_task(self._ticker())
        self._ready.set()

    async def _ticker(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("sim tick failed")
            await asyncio.sleep(1.0)

    def _broadcast(self, message: dict) -> None:
        packet = publish_packet(f"device/{self.s.serial}/report", json.dumps(message).encode())
        for c in list(self._clients):
            if f"device/{self.s.serial}/report" in c.subscriptions:
                try:
                    c.writer.write(packet)
                except Exception:
                    self._clients.remove(c)

    async def _read_packet(self, reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
        first = (await reader.readexactly(1))[0]
        multiplier, length = 1, 0
        while True:
            byte = (await reader.readexactly(1))[0]
            length += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                break
            multiplier *= 128
        body = await reader.readexactly(length) if length else b""
        return first >> 4, first & 0x0F, body

    async def _mqtt_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(writer)
        try:
            ptype, _flags, body = await self._read_packet(reader)
            if ptype != 1:
                return
            pos = 2 + struct.unpack("!H", body[:2])[0]  # protocol name
            pos += 1  # level
            flags = body[pos]
            pos += 3  # flags + keepalive
            fields = []
            while pos < len(body):
                n = struct.unpack("!H", body[pos:pos + 2])[0]
                fields.append(body[pos + 2:pos + 2 + n].decode())
                pos += 2 + n
            username = fields[1] if flags & 0x80 and len(fields) > 1 else ""
            password = fields[2] if flags & 0x40 and len(fields) > 2 else ""
            if username != "bblp" or password != self.s.access_code:
                self.logins_rejected += 1
                writer.write(b"\x20\x02\x00\x05")  # not authorized
                await writer.drain()
                return
            writer.write(b"\x20\x02\x00\x00")
            self._clients.append(client)
            while True:
                ptype, flags, body = await self._read_packet(reader)
                if ptype == 3:  # PUBLISH
                    tlen = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + tlen].decode()
                    pos = 2 + tlen
                    qos = (flags >> 1) & 3
                    if qos:
                        pid = body[pos:pos + 2]
                        pos += 2
                        writer.write(b"\x40\x02" + pid)
                    if topic == f"device/{self.s.serial}/request":
                        self._handle_request(body[pos:])
                elif ptype == 8:  # SUBSCRIBE
                    pid, pos, granted = body[:2], 2, bytearray()
                    while pos < len(body):
                        n = struct.unpack("!H", body[pos:pos + 2])[0]
                        client.subscriptions.add(body[pos + 2:pos + 2 + n].decode())
                        pos += 2 + n + 1
                        granted.append(0)
                    writer.write(b"\x90" + _encode_len(2 + len(granted)) + pid + bytes(granted))
                elif ptype == 10:  # UNSUBSCRIBE
                    writer.write(b"\xb0\x02" + body[:2])
                elif ptype == 12:  # PINGREQ
                    writer.write(b"\xd0\x00")
                elif ptype == 14:  # DISCONNECT
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            if client in self._clients:
                self._clients.remove(client)
            writer.close()

    async def _camera_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            auth = await reader.readexactly(80)
            user = auth[16:48].rstrip(b"\0").decode(errors="replace")
            code = auth[48:80].rstrip(b"\0").decode(errors="replace")
            if struct.unpack("<II", auth[:8]) != (0x40, 0x3000) or user != "bblp" or code != self.s.access_code:
                self.logins_rejected += 1
                return  # the real printer just closes the stream
            seq = 0
            while True:
                st = self.state
                progress = st["layer_num"] / max(1, st["total_layer_num"])
                age = None if self._failure_started is None else time.monotonic() - self._failure_started
                jpeg = frames.render(progress, time.monotonic(), age, light=self.s.scenario != "dark", seed=seq)
                writer.write(struct.pack("<IIII", len(jpeg), 0, 1, 0) + jpeg)
                await writer.drain()
                seq += 1
                await asyncio.sleep(self.s.frame_interval)
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()
