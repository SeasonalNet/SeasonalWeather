from seasonalweather.broadcast.segment_registry import ResolvedSegmentRegistry
from seasonalweather.broadcast.segment_store import SegmentStore


class SegmentApplicationService:
    def __init__(self, registry: ResolvedSegmentRegistry, store: SegmentStore):
        self.registry = registry
        self.store = store
