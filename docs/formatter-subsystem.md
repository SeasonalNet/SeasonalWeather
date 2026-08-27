# Prose formatter subsystem

SeasonalWeather has one physical prose-formatting implementation:
`seasonalweather.broadcast.formatters`. `FormatterSubsystem` is the controller
composition API in that same module; it is not a façade over source-specific
formatter implementations.

The subsystem owns the final spoken/prose contracts for ERN/GWES SAME,
NWWS-OI and NWS product text (including NWS JSON-LD/CAP), IPAWS CAP, NOW, PNS,
and the CAP/NWS, observation, and OFFNT2 render hooks. Source adapters may
parse and normalize their own wire formats, but they do not choose an
independent spoken vocabulary or narration policy.

The older source-named modules (`builder`, `product_text`, `cap_text`, `ern_script`,
`ipaws_text`, `now`, `pns`, `offnt2`, and `rwr`) are re-export-only compatibility
shims. Their implementation must not be restored. Production controller/runtime
code imports the single subsystem, and `tools.quality.architecture_check` enforces
both sides of that contract: SWARCH055 rejects legacy formatter imports and
SWARCH056 rejects formatter implementation in a compatibility shim.

Shared prose policy is physically defined once. For example, CAP and NWWS-OI
watch notifications both consume the same `build_watch_reminder` implementation,
so a WCN reminder wording change cannot silently diverge between source paths.

IPAWS polling likewise performs transport and structural validation only. It
does not discard product types such as DMO, RWT, or RMT. The controller's
configured `ipaws.full_events` and `ipaws.voice_events` sets decide whether an
accepted event is aired or ignored.
