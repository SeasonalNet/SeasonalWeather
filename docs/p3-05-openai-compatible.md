# P3-05 OpenAI-compatible TTS Compose integration

P3-05 connects the existing backend-neutral `openai_compatible` adapter to an
externally managed OpenAI-compatible speech API. The standard `compose.yaml`
graph does not require the provider. Operators opt into the connection overlay
only when the selected configuration backend is `openai_compatible` or uses it
as a fallback.

## Configuration

The deployment configuration must select the provider explicitly and point to
the read-only API key mounted by the overlay:

```yaml
tts:
  backend: "openai_compatible"
  fallback_backend: "local"
  openai_compatible:
    base_url: "https://api.openai.com/v1"
    api_key_file: "/run/secrets/OPENAI_COMPATIBLE_API_KEY"
    model: "configured-model"
    voice: "configured-voice"
    response_format: "wav"
    speed: 1.0
    connect_timeout_seconds: 5.0
    synthesis_timeout_seconds: 180.0
    max_input_bytes: 65536
    max_response_bytes: 67108864
    verify_tls: true
```

The provider URL must be an HTTPS origin whose configured path is `/v1`; the
adapter appends `/audio/speech`. Userinfo, query strings, fragments, alternate
route prefixes, and disabled TLS verification are not supported deployment
modes. The provider remains external to this repository and may be any service
that implements the configured compatibility contract.

## Optional Compose overlay

Create the untracked API-key file with mode `0400`, then validate the explicit
overlay before starting a deployment:

```bash
install -m 0400 /path/to/openai-compatible-api-key \
  secrets/OPENAI_COMPATIBLE_API_KEY
docker compose \
  -f compose.yaml \
  -f compose.openai-compatible.yaml \
  config --quiet
```

The overlay adds one controller-only secret mount at
`/run/secrets/OPENAI_COMPATIBLE_API_KEY`. It does not add a provider image,
provider service, worker credential, environment variable, artifact volume, or
controller-facing provider proxy. The provider's network endpoint and
credentials remain operator-managed configuration.

## Request and response authority

The adapter sends common-preprocessed backend-neutral text and the configured
model, voice, response format, and supported speed. It enforces the configured
input and response bounds before and during the request. WAV responses are
accepted directly; supported non-WAV responses are passed through the existing
common finalization path before controller-owned media validation.

The adapter only supplies a staged provider result. Common finalization and the
existing controller-owned P1-10 validation, fencing, atomic promotion, and
publication path remain authoritative. P3-05 does not add a second scheduler,
capability authority, audio cache, or publication store.

## Failure and fallback behavior

Authentication, authorization, rate-limit, request, provider, transport,
timeout, malformed-response, unsupported-format, and conversion failures
produce bounded typed failures. Configured local fallback or a
controller-resolved last-known-good artifact is used only when the existing
purpose policy permits it and the original deadline remains open. Cancellation
and global deadline expiry never trigger a new fallback synthesis.

An unsuccessful, stale, substituted, or ambiguous result cannot replace the
current active broadcast artifact. Rollback therefore means retaining the
previous controller-accepted artifact or reconciling through the existing
publication journal; it does not introduce another audio authority.

Provider keys, authorization headers, provider error bodies, raw synthesis
text, and unbounded request identifiers are excluded from logs, diagnostics,
metrics, and durable configuration-reload evidence.

## Bundled local voice parity

P3-05 also closes the local worker voice-parity gap. The existing VoiceText
Paul handler is available to the dedicated `voicetext-paul` worker profile, and
the local engine registry now includes `spfy`, matching the `spfy` invocation
contract used by the externally managed `seasonal-ttsd` worker. Both profiles
are opt-in Compose services with dedicated images that carry their own engine
runtimes: VoiceText Paul embeds a checksum-pinned archive and installs Wine,
Wine32, and Xvfb; `spfy` embeds a checksum-pinned executable and voice
manifest. The standard graph does not install either runtime.

The profiles advertise bounded TTS and alert-artifact capability metadata, but
remain unqualified while the deployment has not supplied a real handler
resolver. P3-05 does not move raw synthesis input into SWWP jobs or bypass the
P3-06 controller-owned resolver, staging, validation, and final publication
boundaries.
