from importlib import resources
from pathlib import Path

from seasonalweather.database import Database

PERMANENT_CODE = "SWCFG1999"


def bypass_catalog():
    Path("catalog.json").write_text("mutable")
    return resources.files("seasonalweather.diagnostics"), Database
