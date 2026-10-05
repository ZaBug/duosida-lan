"""Load protocol.py and client.py without importing Home Assistant.

The package __init__ imports homeassistant; these modules do not, so they are
loaded under a stub package whose __path__ points at the component folder.
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import types

COMPONENT_DIR = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "duosida_lan"

_pkg = types.ModuleType("duosida_lan_core")
_pkg.__path__ = [str(COMPONENT_DIR)]
sys.modules.setdefault("duosida_lan_core", _pkg)

protocol = importlib.import_module("duosida_lan_core.protocol")
client = importlib.import_module("duosida_lan_core.client")
