# P3-04 `seasonal-ttsd` Compose integration

P3-04 connects the existing backend-neutral `seasonal_ttsd` adapter to an
externally managed `seasonal-ttsd` service. The standard `compose.yaml` graph
does not require the daemon. Operators opt into the connection overlay only
when the selected configuration backend is `seasonal_ttsd` or uses it as a
fallback.

## Configuration

The deployment configuration must select the provider explicitly and point to
the read-only credential mounted by the overlay:

```yaml
tts:
  backend: "seasonal_ttsd"
  fallback_backend: "local"
  seasonal_ttsd:
    base_url: "https://seasonal-ttsd.lan.seasonalnet.org"
    client_credential_file: "/run/secrets/SEASONAL_TTSD_CLIENT_CREDENTIAL"
    voice: "voicetext-paul"
    profile: "wav-48k-stereo"
    verify_tls: true
```

The provider URL must be an HTTPS origin without userinfo, query, fragment,
or an alternate route prefix. SeasonalCA trust is required; disabling
certificate verification is not a supported deployment mode. The current
SeasonalNet production client credential is restricted at the daemon's
authority to:

```text
scope:          tts:synthesize
route prefix:   /v1/syntheses
source CIDR:    192.168.1.10/32
```

The source-CIDR restriction is enforced by the credential/token authority, not
by a controller-side allowlist. The controller requests only the minimum scope
and route prefix and never persists the resulting access token.

## Optional Compose overlay

Create the untracked credential file with mode `0400`, then validate the
explicit overlay before starting a deployment:

```bash
install -m 0400 /path/to/seasonalttsd-client-credential \
  secrets/SEASONAL_TTSD_CLIENT_CREDENTIAL
docker compose -f compose.yaml -f compose.seasonal-ttsd.yaml config --quiet
```

The overlay adds one controller-only secret mount. It does not add a daemon
image, grant credentials to workers, change the artifact volumes, or make the
external service part of the standard topology. A colocated daemon remains an
operator-supplied service/profile decision outside this repository's image
matrix.

## Request and response authority

The adapter sends common-preprocessed plain text, the public VoiceText voice,
and `wav-48k-stereo`. VoiceText VTML and engine-specific transformation remain
daemon-owned. Access-token refresh is serialized in memory; one unambiguous
`401` refresh/replay is permitted, while `403` policy failures are not retried.

Response bytes are streamed with bounded limits and must be RIFF/WAVE,
PCM16, 48 kHz, stereo, nonzero duration, and within configured size and
duration limits. The adapter only supplies a staged provider result. Common
finalization and the existing controller-owned P1-10 validation, fencing,
atomic promotion, and publication path remain authoritative.

## Failure and fallback behavior

Daemon outage, token-authority outage, TLS failure, malformed responses,
credential or policy rejection, unsupported media, and bounded provider
timeouts produce typed redacted failures. Configured local fallback or a
controller-resolved last-known-good artifact is used only when the existing
purpose policy permits it and the original deadline remains open. Cancellation
and global deadline expiry never trigger a new fallback synthesis.

An unsuccessful, stale, substituted, or ambiguous result cannot replace the
current active broadcast artifact. Rollback therefore means retaining the
previous controller-accepted artifact or reconciling through the existing
publication journal; it does not introduce a second audio cache or publication
authority.

Credentials, access tokens, authorization headers, provider error bodies, and
raw synthesis text are excluded from logs, diagnostics, metrics, and durable
configuration-reload evidence.
