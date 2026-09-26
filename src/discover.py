"""
Extracts the list of optimizer serials (and metadata) from the site's
equipment tree, so the poller doesn't need a hand-maintained list.

Confirmed shape (2026-09-23, live account): the layout endpoint returns a
tree of nested "children", where an optimizer node looks like:
    {"type": "OPTIMIZER", "serial": "1A2B3C4D-01", "name": "Optimizer 1.0.1",
     "displayOrder": "1.0.1", "properties": {"panelModelName": "ACME 400"}}
"""
from __future__ import annotations

import logging

from .client import SolarEdgeClient

logger = logging.getLogger(__name__)


class Optimizer:
    __slots__ = ("serial", "name", "display_order", "panel_model")

    def __init__(self, serial: str, name: str, display_order: str, panel_model: str | None):
        self.serial = serial
        self.name = name
        self.display_order = display_order
        self.panel_model = panel_model


def extract_optimizers(layout_json: dict) -> list[Optimizer]:
    optimizers: list[Optimizer] = []
    _walk(layout_json, optimizers)
    return optimizers


def _walk(node: object, optimizers: list[Optimizer]) -> None:
    if isinstance(node, dict):
        if node.get("type") == "OPTIMIZER" and node.get("serial"):
            optimizers.append(
                Optimizer(
                    serial=node["serial"],
                    name=node.get("name", ""),
                    display_order=node.get("displayOrder", ""),
                    panel_model=node.get("properties", {}).get("panelModelName"),
                )
            )
        for value in node.values():
            _walk(value, optimizers)
    elif isinstance(node, list):
        for item in node:
            _walk(item, optimizers)


def discover_optimizers(client: SolarEdgeClient) -> list[Optimizer]:
    layout = client.fetch_site_layout()
    optimizers = extract_optimizers(layout)
    logger.info("Discovered %d optimizers in site layout", len(optimizers))
    return optimizers
