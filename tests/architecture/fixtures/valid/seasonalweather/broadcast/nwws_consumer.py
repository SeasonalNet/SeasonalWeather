from seasonalweather.nwws.source import NwwsProductEnvelope


def consume(envelope: NwwsProductEnvelope) -> str:
    return envelope.source
