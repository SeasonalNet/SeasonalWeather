# P1-16 backend-neutral TTS boundary

This document records the bounded P1-16 attempt-8 correction pass. P1-16
provides local synthesis behind a backend-neutral request/result boundary. It
does not add a remote provider adapter, an HTTP/TLS client, a scheduler, a
second audio cache, or a second publication store.

## Boundary and authorities

`seasonalweather.tts.models` contains immutable request, result, policy,
artifact-evidence, and accepted-artifact-reference models. The service owns
common preprocessing, local-engine dispatch, bounded subprocess execution,
WAV normalization, volume adjustment, hashing, staging, and atomic completion.
The local registry maps canonical engine identities to handlers and capability
identities; it does not own health or capacity.

The authority split is:

| Authority | P1-16 responsibility |
| --- | --- |
| P1-06 | job purpose, priority, deadline, attempts, replay, cancellation, and generation semantics |
| P1-09 | capability qualification, health, capacity, and the admission/result race fence |
| P1-10 | WAV/artifact evidence, staging, and promotion authority |
| P1-14 | schema-admission bridge and typed semantic validation |
| P1-15 | shared TTS activity tracking and reload coordination |
| P1-16 | local execution and backend-neutral synthesis boundary |

The transitional in-process composition uses the explicit P1-09-shaped
`LocalQualification` representation only when a test or deliberate simulation
injects it. A bare `SynthesisService` fails closed. Production composition
publishes one configuration-owned, controller-local embedded-executor snapshot
into the same `CapabilityRegistry` used by P1-09; the local engine registry
supplies implementation/resource evidence but does not own health or capacity.
`P109TtsQualificationAdapter` compares request requirements with that already
published engine/voice/profile/output snapshot; it never advertises a profile
because the request asked for it. An unbound or stale port fails closed, and a
healthy worker with the same capability cannot authorize embedded execution.
Generic `tts.synthesis.v1` evidence cannot qualify a selected local engine or
the separately owned remote adapters implemented in P1-17.

## Historical/pre-modernization caller inventory and purpose policy

The following is the historical/pre-modernization inventory used to establish
purpose coverage. It is not a current normative production-caller list;
`cli/inject_tool.py` remains a separately classified legacy privileged debug
path documented below.

| Caller | Operation | Purpose |
| --- | --- | --- |
| `broadcast/audio_origination.py` | voice-only operator audio | `administrative` |
| `broadcast/audio_origination.py` | alert audio | `alert` |
| `broadcast/segment_store.py` | forecast segment | `routine` |
| `broadcast/conductor.py` | cycle/time segment | `routine` |
| `cli/inject_tool.py` | manually injected spoken audio | `administrative` |

No production caller relies on a compatibility default to classify an alert.
The shared async-to-sync bridge preserves these explicit purposes and carries
the request's cancellation token into synchronous local synthesis.

| Purpose | P1-06 job source | Priority | Deadline | Attempts/replay | Fallback/LKG |
| --- | --- | --- | --- | --- | --- |
| `alert` | `ALERT_ARTIFACT_GENERATE` | `SAFETY_CRITICAL` | 30 seconds | one attempt; `REVALIDATE`, never blind replay | allowed |
| `routine` | `TTS_SYNTHESIZE` | `NORMAL` | 180 seconds | two attempts; idempotent fenced replay | allowed |
| `optional` | explicit optional policy | `LOW` | 120 seconds | one attempt; no replay | suppressed; no fallback |
| `administrative` | explicit administrative policy | `NORMAL` | 180 seconds | one attempt; no replay | allowed |

`policy_for(purpose)` is the source for default deadlines and the accepted
P1-06 fields. Compatibility helpers delegate into this policy and do not own
purpose policy. An alert is explicitly admitted as safety-critical before
local work, and the shared executor cannot turn it into routine synthesis by
omission. Phase-1 direct execution carries the P1-06 priority, attempt, and
replay metadata; it does not claim to implement a scheduler or priority queue
that is not present in this direct executor path.
Remote-known-primary to local fallback is selection simulation only; P1-16
does not execute a remote primary.

## Configuration and legacy normalization

Configuration is nested under `tts.local` for engine, voice, rate, and the
VoiceText options. The old top-level local engine spelling is normalized to
`backend: local` while preserving its voice/rate/VoiceText controls. The
canonical known backend identities are `local`, `seasonal_ttsd`, and
`openai_compatible`; only `local` is executable in P1-16. Semantic/admission
validation rejects unknown backend/fallback, fallback-to-self, local-to-
deferred-remote, remote-to-remote, unsupported engine/voice/rate/volume, and
unusable fallback combinations before runtime `BackendId` construction.

The P1-14 bridge reports bounded typed evidence for input size, deadline,
capability state/capacity, backend viability, engine/voice/profile support,
fallback viability, and unsupported controls where applicable. It is an
extension of the existing validation path, not a second validator.

## Local engines and subprocess policy

The registry currently supports `espeak-ng`, `piper`, `festival`, `dectalk`,
and `voicetext_paul`, with the accepted `espeak` and `espeak_ng` aliases.
Every handler builds its own argv; command construction is confined to the
local TTS owner. Every availability check validates the handler executable and
its required wrapper/resources. DECtalk checks both `dectalk-env` and the
actual executable `say`. VoiceText checks sudo, wrapper, engine executable,
and the reset utility when retry/reset behavior requires it.

VoiceText Paul retains its process lock, `kill_before`, `reset_every`,
wineserver reset after failed synthesis, bounded configured retry, retry sleep
under the same absolute deadline, wrapper/sudo/resource checks, and VTML
preparation. A primary `ProcessFailure` is re-raised when cleanup/reset also
fails; bounded secondary reset evidence is attached to the primary error.
Focused tests use fake executables/resources only and never invoke Wine.
Piper preserves the accepted `-r <sample_rate_hz>` argv meaning. DECtalk keeps
numeric speaker normalization, the 75..600 rate clamp, and existing volume
semantics. `TTS.synth_to_wav()` remains the compatibility "WAV or fail"
operation: typed non-output dispositions raise a bounded exception that does
not render source text, while `synthesize()` remains the typed result boundary.

`run_bounded` launches a new process group, bounds stdout/stderr, watches the
absolute deadline and cancellation event, terminates the group, and reaps the
child before returning a failure. It is the common subprocess owner rather
than an engine-specific process implementation.

## Preprocessing and identity

`seasonalweather.tts.preprocess` is the pure common owner of cleaning, NWS
spoken-time normalization, URL verbalization, and ordered generic overrides.
Legacy exports from `tts.py` delegate to it for compatibility. VoiceText VTML
is local-handler-only. The preprocessing version is
`tts-preprocess-v1`.

Text overrides preserve order and share one pure safe-regex authority with
VoiceText alias and x-cmu phoneme overrides. The accepted grammar covers
literals, anchors, escaped characters/categories, character classes,
noncapturing groups, safe alternation, and bounded or structurally
non-overlapping repetition. Lookaround, backreferences, nested ambiguous
repetition, competing sequential variable repeats, lazy/possessive quantifiers,
and overlarge bounds are rejected. Pattern/replacement size, rule count, and
8,192 replacement work are bounded. Python `IGNORECASE` special equivalence
families (`I/i/İ/ı`, `S/s/ſ`, and `K/k/K`) are included in overlap analysis,
and character-class range expansion is width-bounded before materialization.
Configuration validation rejects unsafe rules with typed source paths; runtime
checking remains a defensive fence.

The controller normalizes the exact text and overrides, then derives the
content identity from the normalized bytes and preprocessing version. A
caller-supplied identity is accepted only as a compatibility assertion and is
recomputed and mismatch-checked. It cannot assert an arbitrary authoritative
identity.

## Deadline, cancellation, WAV, and finalization

One absolute monotonic deadline is created before executor admission and
crosses queue delay, preprocessing, engine execution, normalization,
WAV inspection, volume adjustment, hashing, bounded staging copy, and atomic
completion. Volume adjustment streams PCM16 frames in bounded chunks and
rechecks fences during the loop. Hashing and copies use bounded blocks.

Explicit cancellation and deadline expiry use distinct causes while retaining
native `asyncio.CancelledError` for an explicitly cancelled owner. Queued and
running work consume the same monotonic budget. The controller-composed P1-06
embedded execution port is shared across requests; P1-16 does not create a
per-call executor, scheduler, or priority queue. P1-09 owns the controller-
local capacity reservation and release, including pending embedded work.

The worker writes only to a private staging workspace. Common subprocess
termination/reaping remains authoritative, and a bounded non-cooperative
shutdown cannot recreate caller-owned paths after the bridge returns. A
custom finalizer must return one typed `FinalizationEvidence` record naming a
private completed artifact; it may consume the raw intermediate. The bridge
then performs the accepted generation/capability/deadline fences immediately
before exactly one stable-output `os.replace`. No long finalization work or
caller-visible write follows the authoritative final fence. Typed async
results are translated to `TTSCompatibilityError` only by the narrow
completed-WAV operation, which requires the raw output only when no custom
finalizer was supplied. LKG uses the same final fence. A failed or cancelled
operation cannot publish a completed output. P1-10-compatible WAV inspection
and artifact evidence are used for result validation; P1-16 stages its output
and does not claim publication/promotion ownership.

## Fallback and last-known-good

Only remote-known-primary to local fallback is represented before P1-17. A
fallback must pass its own capability admission, credit the exact already-owned
P1-09 reservation, and still fit the original deadline; cancellation and
timeout never trigger fallback synthesis. The outer execution owner releases
that reservation exactly once across every terminal path.

LKG reuse no longer trusts a filesystem path plus caller metadata and
`validated=True`. A controller-owned P1-10 resolver must return an
`AcceptedArtifactReference`. The trusted output profile binds backend, normalized
local engine, voice, rate, VoiceText pronunciation/markup options, common
preprocessing version, and generic output policy. The service independently
checks content, purpose, source/event/segment identity, configuration
generation, freshness deadline, path safety, authoritative digest, WAV media
evidence, and all cancellation/deadline fences before bounded copy and atomic
completion. Thus forged matching metadata cannot substitute unrelated alert
audio.

## Activity, reload, redaction, and deferrals

The service enters the existing P1-15 shared TTS activity context when one is
provided. One `SynthesisService` and VoiceText invocation counter belong to
each TTS facade/configuration generation. A TTS-changing reload installs the
new controller-local profile and qualification source at the target-generation
resource boundary; source replacement and publication share one controller-
local generation fence, so an overtaken source cannot publish after the new
generation becomes authoritative. Non-TTS reloads retain the profile.
Reload/generation checks are preserved at admission and result acceptance, and
activity release remains exactly once.
Subprocess diagnostics retain bounded, printable evidence and do not include
credentials or raw configuration secrets; paths and identities remain bounded
opaque values.

The historical `cli/inject_tool.py` is outside this modern control path and
retains its documented privileged debug bypass status. HTTP/TLS/provider clients, remote synthesis adapters, remote credential
handling and deferred remote execution were implemented separately in P1-17;
Phase-2 scheduling/queue design remains deferred to a later phase. P1-16 does not restart
services, mutate live configuration, perform production synthesis, or create
another audio cache/publication store.
