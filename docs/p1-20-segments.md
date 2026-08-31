# P1-20 segment builders and inspection

The P1-19 registry remains the only authority for static segment identity,
ordering, enablement, cadence, maximum age, minimum air interval, failure
behavior, and builder declaration. Each static product definition now points
to one bounded `CycleBuilder.build_*_segment` method. The refresher resolves
that declaration and passes one immutable `SegmentBuildInput`; it never calls
whole-cycle generation for a single target. ID and status keep their existing
refresher-owned builders. Live time remains conductor-owned, and outro is
build-only/non-refreshable.

Builders return a typed candidate with normalized text and runtime provenance.
A configured product budget may end on a complete sentence, but the typed
candidate boundary never silently slices spoken text. The TTS admission bound
remains the final shared synthesis safety limit.
The controller-owned SQLite operational store persists the candidate metadata,
content hash, source identity/reference, synthesis and airing evidence, and
bounded failure state. Source references are stripped of credentials, query
strings, and fragments; error evidence is sanitized and bounded. A successful
candidate resets consecutive failures and replaces only its target. A failed
refresh retains an accepted last-known-good candidate; a missing candidate is
represented explicitly as a placeholder. Staleness is calculated against the
registry maximum age and is distinct from placeholder state and refresh
cadence.

The synthesis path completes a private staged WAV, then promotes the artifact
and its corresponding segment/provenance state through the store's bounded
commit boundary. Persistence or promotion failure restores the previous
last-known-good artifact and state; publication is won only after both are
accepted. Observation provenance identifies the actual RWR product or ASOS
fallback that supplied the spoken text, and marine observation refresh shares
only RWR acquisition without running land-observation or ASOS fallback work.

The conductor records `last_aired` only after its current Liquidsoap push has
been accepted. `next_eligible_airtime` is derived from that event and the
registry minimum-air-interval policy. Preview and inspection do not synthesize,
write files, mutate airing state, advance conductor position, or enqueue
Liquidsoap.

Preview recomputes current focus from the alert tracker and explicit mode using
the same pure predicate as the conductor. Its immutable deferred evidence
distinguishes not-yet-due entries from entries due now, and due deferred-focus
segments are appended in the conductor's current relative placement.

The segment refresh endpoint admits `segment.refresh` through the existing
typed command store and controller task supervisor, returns `202` while the
target work remains pending, and exposes completion through the existing
command snapshot route. Unknown, disabled, live-only, and build-only keys are
rejected deterministically.

The following read surfaces are available:

- `GET /v1/segments` — deterministic registry/runtime projection;
- `GET /v1/segments/{key}` — one bounded detail projection;
- `POST /v1/segments/{key}/refresh` — one-target asynchronous refresh;
- `GET /v1/cycle/plan` — normal/focus registry plan;
- `GET /v1/cycle/preview` — read-only current selection preview.

P1-21 owns OFFNT2 content and product behavior. P1-22 owns the broader API
correctness, configuration, and diagnostic-surface pass; P1-20 adds only the
surfaces required by this packet.
