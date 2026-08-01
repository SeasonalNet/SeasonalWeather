from pathlib import Path

from seasonalweather.control import ControlService

candidate = Path("candidate.yaml")
candidate.write_text("mutated")
authority = ControlService
