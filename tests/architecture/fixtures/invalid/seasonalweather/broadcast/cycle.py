from seasonalweather.broadcast.segment_store import SegmentStore


class CycleBuilder:
    def publish(self, output, target):
        return SegmentStore(output, target)
