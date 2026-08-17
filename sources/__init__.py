"""sources package - registry of portal adapters.

Add a portal in three steps:
  1. Write sources/<key>.py with a `Source` subclass (see sources/gs.py as the template).
  2. Import it below and add it to _REGISTER.
  3. Fill its <KEY>_* entries in .env.

daily.py --source <key> and pipeline.py (SOURCES env) drive scraping per source.
"""
from sources.barc import Barclays
from sources.citi import Citi
from sources.db import DeutscheBank
from sources.gs import GoldmanSachs
from sources.jpm import JPMorgan
from sources.ms import MorganStanley

_REGISTER = (
    GoldmanSachs,
    JPMorgan,
    Citi,
    Barclays,
    MorganStanley,
    DeutscheBank,
)

_REGISTRY = {cls.key: cls for cls in _REGISTER}


def get(key):
    """Instantiate the adapter for `key`, or raise a helpful KeyError."""
    try:
        return _REGISTRY[key]()
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown source '{key}'. known sources: {known}")


def keys():
    return sorted(_REGISTRY)
