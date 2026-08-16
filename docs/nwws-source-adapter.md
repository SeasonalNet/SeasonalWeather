# NWWS-OI source adapter

NWWS-OI is a controller-owned, long-running in-process source adapter. The
controller starts, drains, reconnects, and stops it through the existing task
supervisor and lifecycle authority. It is not an SWWP job, bounded worker,
standalone daemon, container, or new source-worker protocol.

The stable boundary is `seasonalweather.nwws.source.NwwsSource`. Consumers
receive only `NwwsProductEnvelope` values through `ProductSink`; they do not
import slixmpp or use XMPP-native stanzas, presence, MUC, SASL, or transport
objects. The initial `SlixmppNwwsSource` implementation is confined to
`seasonalweather.nwws.slixmpp_adapter`. A future Rust implementation can
replace that adapter behind the same source and replay contract.

The adapter owns bounded connection startup, MUC confirmation, connected-but-
silent detection, reconnect backoff, post-materialization stanza normalization,
cancellation, drain, and transport cleanup. The installed/declared slixmpp
1.17.0 XML stream parser has no supported pre-materialization stanza-size
control; therefore complete raw-ingress bounding remains a dependency blocker
and is not claimed by this packet. Post-parse body, metadata, node, depth, and
field limits remain enforced. It preserves stable product identity but does not
suppress duplicates. The controller remains authoritative for product parsing,
allowlisting, targeting, duplicate policy, alert lifecycle, TTS, and
publication.

For `nwws-oi.weather.gov:5222`, the slixmpp adapter explicitly requires the
XMPP STARTTLS feature, disables direct-TLS and plaintext fallback, and uses a
hostname-verifying `CERT_REQUIRED` context with TLS 1.2 or newer. SASL feature
processing is rejected until the adapter observes a verified TLS session, so
authentication cannot be attempted when STARTTLS is unavailable or its
handshake/certificate verification fails.

The controller applies a final source-instance admission fence synchronously at
the queue boundary. Drain closes new admission before waiting for accepted
products, and source replacement retires the old instance before stopping it.
Credentials and raw transport state are excluded from health and diagnostic
messages; runtime diagnostic occurrences use the existing controller-owned
redaction and occurrence service.

The external dependency finding is recorded separately from the implementation:
stock slixmpp 1.17.0 has no supported pre-materialization/raw-ingress bound.
The P1-18 feasibility report and prototype evidence are retained separately and
are not incorporated into SeasonalWeather. This adapter therefore retains its
post-materialization bounds, while the stronger Revision-10 raw-ingress
requirement remains dependency-blocked.

Configured SeasonalWeather log outputs apply a final handler-level containment
filter to the complete `slixmpp` logger namespace. This remains effective when
operators lower `slixmpp` or `slixmpp.xmlstream` to DEBUG, preventing raw RECV,
SEND, malformed-stanza, parser-exception, product, and authentication payloads
from reaching those outputs. Normalized NWWS runtime diagnostics remain the
useful, redacted failure channel; unrelated SeasonalWeather loggers are not
suppressed.
