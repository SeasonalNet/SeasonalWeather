# SWCFG0005 — Generated-audio duration was raised to the supported minimum

## Meaning
The configured generated-audio duration was below the supported fifteen-minute minimum.

## Trigger
`audio.generated_max_duration_seconds` is a finite positive value below 900 seconds.

## Correction or recovery
Set the value to 900 seconds or a larger operator-selected duration.

## Operational effect
Configuration loading continues and SeasonalWeather applies 900 seconds. The byte allowance is derived from that duration and the normalized PCM WAV format.

## Rationale
One duration policy must remain achievable at every generated-media boundary.

## Alternatives or migration
Omit the setting to use the 900-second default.

## Related diagnostics
None.
