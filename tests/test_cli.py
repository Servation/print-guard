"""The read-only commands against the fake printer, and setup with stubbed input.

Setup is tested with a stubbed credential store so tests never write to the real
Windows Credential Manager, and discovery is tested with a packet sent to
localhost so no real printer is involved.
"""

from __future__ import annotations

import socket
import threading
import time
import tomllib
from pathlib import Path

import pytest

from print_guard import cli, credentials
from print_guard.config import load
from print_guard.printer import discovery
from print_guard.sim.printer import FakePrinter, SimSettings

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models" / "obico-failure-detector.onnx"


@pytest.fixture
def fake(tmp_path):
    printer = FakePrinter(SimSettings(scenario="clean", access_code="chk12345", prepare_seconds=1.0),
                          tmp_path / "sim")
    printer.start()
    yield printer
    printer.stop()


def write_config(tmp_path: Path, fake: FakePrinter) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[printer]\nhost = "127.0.0.1"\nserial = "{fake.s.serial}"\nmqtt_port = {fake.mqtt_port}\n'
        f'camera_port = {fake.camera_port}\nca_file = "{fake.certs.ca_file.as_posix()}"\n'
        f'[vision]\nmodel_path = "{MODEL.as_posix()}"\n[guard]\nruns_dir = "{(tmp_path / "runs").as_posix()}"\n',
        encoding="utf-8")
    return path


def test_check_against_fake_printer(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(credentials.ENV_ACCESS_CODE, "chk12345")
    code = cli.main(["check", "--config", str(write_config(tmp_path, fake))])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "MQTT status" in out and "01.10.00.00" in out
    assert "Developer Mode on" in out
    assert "No commands were sent" in out
    assert "chk12345" not in out
    assert fake.commands == []
    assert list((tmp_path / "runs").glob("check-*.jpg"))


def test_check_reports_wrong_access_code(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(credentials.ENV_ACCESS_CODE, "wrong999")
    code = cli.main(["check", "--config", str(write_config(tmp_path, fake))])
    out = capsys.readouterr().out
    assert code == 1
    assert "access code" in out
    assert fake.logins_rejected >= 1


def test_check_refuses_a_different_printer(fake, tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path, fake)
    path.write_text(path.read_text(encoding="utf-8").replace(fake.s.serial, "01P09OTHER00001"), encoding="utf-8")
    monkeypatch.setenv(credentials.ENV_ACCESS_CODE, "chk12345")
    cli.main(["check", "--config", str(path)])
    out = capsys.readouterr().out
    assert "different from config" in out
    assert fake.logins_rejected == 0  # never sent the code to the wrong device's login


@pytest.mark.skipif(not MODEL.exists(), reason="needs the detector model")
def test_snapshot_against_fake_printer(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(credentials.ENV_ACCESS_CODE, "chk12345")
    out_file = tmp_path / "snap.jpg"
    code = cli.main(["snapshot", "--config", str(write_config(tmp_path, fake)), "--out", str(out_file)])
    assert code == 0
    assert out_file.exists() and out_file.stat().st_size > 10_000
    assert "failure score 0.00" in capsys.readouterr().out


def test_setup_writes_valid_config_and_stores_code_securely(tmp_path, monkeypatch):
    stored = {}
    monkeypatch.setattr(credentials, "keyring_set", lambda name, value: stored.__setitem__(name, value))
    monkeypatch.setattr(discovery, "discover", lambda seconds: [discovery.Announcement(
        "192.168.1.77", "01P09A0000000001", "C12", "Workshop", "lan", "01.10.00.00")])
    answers = iter(["1", "", "", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    secrets_in = iter(["abcd1234", ""])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(secrets_in))
    target = tmp_path / "config.toml"
    assert cli.main(["setup", "--config", str(target)]) == 0
    text = target.read_text(encoding="utf-8")
    assert "abcd1234" not in text
    cfg = load(target)
    assert (cfg.printer.host, cfg.printer.serial) == ("192.168.1.77", "01P09A0000000001")
    assert cfg.notify.ntfy_topic.startswith("print-guard-") and len(cfg.notify.ntfy_topic) > 20
    assert stored == {"access-code:01P09A0000000001": "abcd1234"}
    tomllib.loads(text)  # comments from the example survive and still parse


def test_discover_hears_an_announcement():
    # A private port: Bambu Studio may own 2021 on localhost, and a broadcast on 2021 would show
    # it a phantom printer. Real printers broadcast, which every listener on 2021 receives.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result: list = []
    listener = threading.Thread(target=lambda: result.extend(discovery.discover(3.0, port=port)))
    listener.start()
    time.sleep(0.5)
    packet = (b"NOTIFY * HTTP/1.1\r\nLocation: 127.0.0.1\r\nNT: urn:bambulab-com:device:3dprinter:1\r\n"
              b"USN: 01P09LOCALTEST1\r\nDevModel.bambu.com: C12\r\nDevConnect.bambu.com: lan\r\n\r\n")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _ in range(3):
            s.sendto(packet, ("127.0.0.1", port))
            time.sleep(0.2)
    listener.join()
    mine = [a for a in result if a.serial == "01P09LOCALTEST1"]
    assert mine and mine[0].model == "P1S" and mine[0].connect_mode == "lan"


def test_connection_diagnosis_explains_failures(fake):
    from print_guard.printer import tls
    from print_guard.printer.mqtt import PrinterLink

    ctx = tls.make_context("01P09OTHER00001", True, fake.certs.ca_file)
    wrong = PrinterLink("127.0.0.1", "01P09OTHER00001", "x", ctx, port=fake.mqtt_port,
                        probe_ca=str(fake.certs.ca_file))
    wrong._diagnose()
    assert "different printer" in wrong.last_error
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    gone = PrinterLink("127.0.0.1", fake.s.serial, "x", ctx, port=closed_port)
    gone._diagnose()
    assert "unreachable" in gone.last_error
