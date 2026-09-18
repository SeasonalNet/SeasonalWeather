# Generated-audio policy

Generated PCM WAVs use one configuration-derived policy across synthesis,
worker claims, controller staging, promotion, and cleanup accounting.

`audio.generated_max_duration_seconds` defaults to 900 seconds and has a
900-second minimum. A smaller finite positive value is normalized to 900 and
emits `SWCFG0005`; malformed and non-finite values remain configuration
errors. Larger values are supported without a second duration ceiling.

The byte allowance is derived from the selected sample rate, stereo PCM16
output, configured duration, and a bounded WAV-container allowance. It is
independent of `jobs.result_max_bytes`, which continues to bound serialized
job-result JSON rather than media artifacts.

Generated-file cleanup is controller-owned and reference-first. Active
segments, alerts, inserts, station-feed entries, and live audio assets remain
protected regardless of age. The retention interval is only a crash/race
grace after no durable reference remains; the legacy soft-cap setting does not
override a reference or that grace.

The `spfy` worker is pinned to upstream release `2026.09.06`, commit
`698c6e137cde17815272e7090be05442bed995e9`, with the Linux x86_64 release
archive SHA-256
`b932f76874be06e9545fbd677bb050d0d603d93519a4710a4d474db16784a6c5`.
The upstream project remains under its shipped GPL-3.0 license and resource
layout. SeasonalWeather passes the configured local rate through `SPFY_RATE`;
SSML and inline `\\!` controls remain interpreted by the pinned engine.
