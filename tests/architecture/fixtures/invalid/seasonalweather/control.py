from seasonalweather.broadcast.segment_service import SegmentApplicationService
from seasonalweather.configuration_reload.service import ConfigurationReloadService


def compose():
    return SegmentApplicationService()
