from seasonalweather.nwws.source import NwwsProductEnvelope, ProductSink


def consume(sink: ProductSink, envelope: NwwsProductEnvelope) -> None:
    del sink, envelope
