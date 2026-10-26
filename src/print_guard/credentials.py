"""Secret lookup and log redaction.

The printer's LAN access code is a password: anyone on the network who has it can
control the printer. It is resolved from, in order: the PRINT_GUARD_ACCESS_CODE
environment variable, Windows Credential Manager (written by `print-guard setup`),
or the config file as a last resort. Every secret that gets loaded is registered
with the redaction filter so it can never reach a log line, a notification or
the dashboard, even through an exception message.
"""

from __future__ import annotations

import logging
import os
import re
import threading

KEYRING_SERVICE = "print-guard"
ENV_ACCESS_CODE = "PRINT_GUARD_ACCESS_CODE"

log = logging.getLogger(__name__)

_secrets: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    """Make `value` redacted everywhere from now on. Short strings are ignored."""
    if value and len(value) >= 4:
        with _lock:
            _secrets.add(value)


def redact(text: str) -> str:
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, "***")
    return text


def mask_serial(serial: str) -> str:
    """Show enough of the serial to tell printers apart without printing it whole."""
    if len(serial) <= 8:
        return "***"
    return f"{serial[:5]}...{serial[-3:]}"


class RedactingFilter(logging.Filter):
    """Rewrites log records so registered secrets never reach any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad format string should still log something
            message = str(record.msg)
        clean = redact(message)
        if record.exc_info and record.exc_info[1] is not None:
            # Exception text is rendered later by the formatter; pre-render it redacted.
            formatter = logging.Formatter()
            record.exc_text = redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        record.msg, record.args = clean, None
        return True


def _keyring_get(name: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception as e:  # no backend, locked vault, etc.
        log.debug("keyring lookup for %s failed: %s", name, e)
        return None


def keyring_set(name: str, value: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, name, value)
    register_secret(value)


def access_code_key(serial: str) -> str:
    return f"access-code:{serial}"


def resolve_access_code(serial: str, config_value: str = "") -> tuple[str, str]:
    """Return (access_code, source). Raises LookupError when none is configured."""
    env = os.environ.get(ENV_ACCESS_CODE, "").strip()
    if env:
        register_secret(env)
        return env, "environment"
    stored = _keyring_get(access_code_key(serial))
    if stored:
        register_secret(stored)
        return stored, "Windows Credential Manager"
    if config_value.strip():
        register_secret(config_value.strip())
        return config_value.strip(), "config file"
    raise LookupError(
        "no access code found. Run `print-guard setup` (stores it in Windows Credential Manager) "
        f"or set {ENV_ACCESS_CODE}."
    )


def resolve_anthropic_key() -> str | None:
    """API key for the optional Claude judge, if one is stored for print-guard.

    Returns None when nothing is stored here; the Anthropic SDK then falls back to
    its own resolution (ANTHROPIC_API_KEY, `ant auth login` profiles).
    """
    stored = _keyring_get("anthropic-api-key")
    if stored:
        register_secret(stored)
    env = os.environ.get("ANTHROPIC_API_KEY")
    register_secret(env)
    return stored


_ACCESS_CODE_RE = re.compile(r"^[A-Za-z0-9]{8}$")


def looks_like_access_code(value: str) -> bool:
    """P1-series LAN access codes are 8 characters (not the 6-digit bind PIN)."""
    return bool(_ACCESS_CODE_RE.match(value))
