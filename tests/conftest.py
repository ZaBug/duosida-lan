"""Load protocol.py and client.py without importing Home Assistant.

The package __init__ imports homeassistant; these modules do not, so they are
loaded under a stub package whose __path__ points at the component folder.
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import types

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components" / "duosida_lan"

# Import the repo's custom_components package before the HA test harness puts
# its own testing_config/custom_components on sys.path; HA's loader then finds
# duosida_lan.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
importlib.import_module("custom_components")

_pkg = types.ModuleType("duosida_lan_core")
_pkg.__path__ = [str(COMPONENT_DIR)]
sys.modules.setdefault("duosida_lan_core", _pkg)

protocol = importlib.import_module("duosida_lan_core.protocol")
client = importlib.import_module("duosida_lan_core.client")
discovery = importlib.import_module("duosida_lan_core.discovery")
