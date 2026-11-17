"""Adapter registry."""

from __future__ import annotations

from ..config import PrinterConfig
from ..errors import ConfigError
from .bambu import BambuAdapter
from .base import Adapter
from .creality import CrealityAdapter
from .duet import DuetAdapter
from .elegoo import ElegooAdapter
from .flashforge import FlashForgeAdapter
from .moonraker import MoonrakerAdapter
from .octoprint import OctoPrintAdapter
from .prusalink import PrusaLinkAdapter
from .repetier import RepetierAdapter
from .snapmaker import SnapmakerAdapter
from .ultimaker import UltimakerAdapter
from .usb_serial import SerialAdapter

ADAPTERS: dict[str, type[Adapter]] = {a.type_name: a for a in (
    MoonrakerAdapter, OctoPrintAdapter, PrusaLinkAdapter, BambuAdapter, CrealityAdapter, ElegooAdapter,
    DuetAdapter, FlashForgeAdapter, SnapmakerAdapter, UltimakerAdapter, RepetierAdapter, SerialAdapter)}


def adapter_class(type_name: str) -> type[Adapter]:
    if type_name not in ADAPTERS:
        raise ConfigError(f"unknown printer type {type_name!r}", f"Use one of: {', '.join(ADAPTERS)}")
    return ADAPTERS[type_name]


def build(cfg: PrinterConfig, secret: str | None = None) -> Adapter:
    return adapter_class(cfg.type)(cfg, secret)
