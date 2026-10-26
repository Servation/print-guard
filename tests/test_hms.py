"""Decoding and classifying the printer's health codes and print errors."""

from __future__ import annotations

from print_guard.printer.hms import (
    Category,
    decode_hms,
    decode_print_error,
    hms_code,
    print_error_code,
)


def test_code_formatting():
    assert hms_code(0x07002000, 0x00020001) == "0700_2000_0002_0001"
    assert print_error_code(117473297) == "0700_8011"


def test_runout_is_classified():
    (p,) = decode_hms([{"attr": 0x07002000, "code": 0x00020001}])
    assert p.category == Category.RUNOUT
    assert p.severity == "serious"
    assert "run out" in p.text
    pe = decode_print_error(0x07008011)
    assert pe is not None and pe.category == Category.RUNOUT


def test_auto_switch_to_backup_spool_is_informational():
    (p,) = decode_hms([{"attr": 0x07002000, "code": 0x00030002}])
    assert p.auto_recovered
    assert p.severity == "common"


def test_thermal_fatal():
    (p,) = decode_hms([{"attr": 0x03000100, "code": 0x00010001}])
    assert p.severity == "fatal"
    assert p.category == Category.THERMAL


def test_auth_failure_code():
    (p,) = decode_hms([{"attr": 0x05000500, "code": 0x00010007}])
    assert p.category == Category.AUTH


def test_clog_is_extrusion():
    (p,) = decode_hms([{"attr": 0x03000900, "code": 0x00020001}])
    assert p.category == Category.EXTRUSION


def test_cancel_and_empty_and_garbage():
    assert decode_print_error(0) is None
    pe = decode_print_error(0x0300400C)
    assert pe is not None and pe.category == Category.CANCELED
    assert decode_hms([{"attr": 0, "code": 0}, {"bad": 1}, {"attr": "x", "code": 1}]) == []


def test_unknown_code_still_reported():
    (p,) = decode_hms([{"attr": 0x0F0F0F0F, "code": 0x00010001}])
    assert p.severity == "fatal"
    assert "Unknown" in p.text
    assert p.wiki_url.endswith(p.code)
