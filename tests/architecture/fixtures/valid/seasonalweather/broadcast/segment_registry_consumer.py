from seasonalweather.broadcast.segment_registry import DEFAULT_SEGMENT_REGISTRY


def title_for_status() -> str:
    return DEFAULT_SEGMENT_REGISTRY.resolve().title_for("status")
