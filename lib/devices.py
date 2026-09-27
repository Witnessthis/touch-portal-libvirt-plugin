"""Generic USB device discovery.

Reads vendor/product IDs directly out of the *.xml hostdev descriptor files in
whatever directory the "xml_dir" config value points at, instead of hardcoding any
device names or assuming a particular directory layout. Dropping a new descriptor
into that directory is enough to make it show up here on the next scan -- nothing in
this repo needs to change.

This module only ever reads those files. It never writes to or executes anything in
that directory.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Device:
    # Stable identifier derived from the filename, e.g. "my-device" for
    # my-device.xml. Used as the Touch Portal state id (with a fixed prefix
    # added by the caller) and as the human-readable label.
    name: str
    vendor_id: int
    product_id: int
    xml_path: Path


def _parse_hex_attr(root: ET.Element, xpath: str) -> int | None:
    el = root.find(xpath)
    if el is None:
        return None
    raw = el.get("id")
    if not raw:
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


def discover_devices(xml_dir: Path) -> list[Device]:
    """Scan xml_dir for hostdev descriptor files and return the USB ones.

    Non-USB descriptors (no <hostdev>/vendor+product, e.g. PCI passthrough XML) are
    silently skipped, as are files that fail to parse -- a malformed or unrelated
    XML file in that directory should never take the whole plugin down.
    """
    devices: list[Device] = []
    for xml_path in sorted(xml_dir.glob("*.xml")):
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            continue

        # Standalone hostdev descriptors have <hostdev> as the document root (its
        # <source>/<vendor>/<product> are then direct descendants, not nested under
        # a further <hostdev>), so search for vendor/product anywhere in the
        # document rather than assuming a particular nesting depth.
        vendor_id = _parse_hex_attr(root, ".//vendor")
        product_id = _parse_hex_attr(root, ".//product")
        if vendor_id is None or product_id is None:
            continue

        devices.append(
            Device(
                name=xml_path.stem,
                vendor_id=vendor_id,
                product_id=product_id,
                xml_path=xml_path,
            )
        )
    return devices
