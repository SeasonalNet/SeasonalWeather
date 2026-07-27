from seasonalweather.worker.handlers import synthesize
from seasonalweather.artifacts.promotion import PromotionService


def dispatch() -> None:
    synthesize()
