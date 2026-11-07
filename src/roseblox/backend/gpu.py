"""The host's graphics cards, so one can be pinned for Rosemary's Vulkan renderer."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

NVIDIA = 0x10DE
_VENDOR_NAMES = {NVIDIA: "NVIDIA", 0x1002: "AMD", 0x8086: "Intel"}
_SOFTWARE_MARKERS = ("lavapipe", "llvmpipe", "swrast", "softpipe")
PCI_IDS_PATHS = (Path("/usr/share/hwdata/pci.ids"), Path("/usr/share/misc/pci.ids"))


@dataclass(frozen=True)
class Gpu:
    slot: str
    vendor: int
    device: int
    driver: str
    name: str
    boot: bool = False

    @property
    def ids(self) -> str:
        return f"{self.vendor:04x}:{self.device:04x}"

    def vulkan_environment(self) -> dict[str, str]:
        """Variables that make the Vulkan loader offer this card alone."""
        return {
            "MESA_VK_DEVICE_SELECT": self.ids,
            "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE": "1",
            # NVIDIA's Optimus layer otherwise reorders the cards itself; its driver ignores Mesa.
            "__VK_LAYER_NV_optimus": "NVIDIA_only" if self.driver == "nvidia" else "non_NVIDIA_only",
        }


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _hex(text: str | None) -> int | None:
    try:
        return int(text, 16) if text else None
    except ValueError:
        return None


@lru_cache(maxsize=4)
def _pci_names(path: Path) -> dict[int, tuple[str, dict[int, str]]]:
    names: dict[int, tuple[str, dict[int, str]]] = {}
    current: dict[int, str] | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if line.startswith("C "):
                    break
                if not line.strip() or line.startswith("#"):
                    continue
                if not line.startswith("\t"):
                    vendor = _hex(line[:4])
                    current = {} if vendor is not None else None
                    if vendor is not None:
                        names[vendor] = (line[4:].strip(), current)
                elif current is not None and not line.startswith("\t\t"):
                    device = _hex(line[1:5])
                    if device is not None:
                        current[device] = line[5:].strip()
    except OSError:
        return {}
    return names


def gpu_name(vendor: int, device: int, pci_ids: Path | None = None) -> str:
    for path in (pci_ids,) if pci_ids is not None else PCI_IDS_PATHS:
        vendor_name, devices = _pci_names(path).get(vendor, (None, {}))
        if vendor_name:
            device_name = devices.get(device)
            short_vendor = _VENDOR_NAMES.get(vendor, vendor_name)
            return f"{short_vendor} {device_name}" if device_name else f"{short_vendor} device {device:04x}"
    return f"{_VENDOR_NAMES.get(vendor, f'Vendor {vendor:04x}')} device {device:04x}"


def list_gpus(drm: Path = Path("/sys/class/drm"), pci_ids: Path | None = None) -> list[Gpu]:
    """Return the real graphics cards, the boot card first."""
    try:
        entries = sorted(os.listdir(drm))
    except OSError:
        return []
    cards: list[Gpu] = []
    for entry in entries:
        if not entry.startswith("card") or not entry[4:].isdigit():
            continue
        device = drm / entry / "device"
        vendor = _hex(_read(device / "vendor"))
        product = _hex(_read(device / "device"))
        if vendor is None or product is None:
            continue
        try:
            slot = os.path.basename(os.path.realpath(device))
            driver = os.path.basename(os.readlink(device / "driver"))
        except OSError:
            driver = ""
        name = gpu_name(vendor, product, pci_ids)
        if any(marker in f"{driver} {name}".lower() for marker in _SOFTWARE_MARKERS):
            continue
        if any(card.slot == slot for card in cards):
            continue
        cards.append(Gpu(slot, vendor, product, driver, name, _read(device / "boot_vga") == "1"))
    return sorted(cards, key=lambda card: (not card.boot, card.slot))


def find_gpu(slot: str | None, gpus: list[Gpu]) -> Gpu | None:
    return next((gpu for gpu in gpus if gpu.slot == slot), None) if slot else None
