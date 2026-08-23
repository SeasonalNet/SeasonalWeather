# SWBUILD1001 — Build identity metadata is invalid

## Meaning

Build provenance or release identity metadata is malformed or incomplete.

## Trigger

The build-information boundary cannot parse a bounded identity record.

## Correction or recovery

Regenerate the build information with the governed build target and inspect the
result before using the image or release.

## Operational effect

The identity cannot establish release authority or compatibility.

## Rationale

Untrusted build identity must fail closed before startup or publication.

## Alternatives or migration

Use an older known-good image only when its complete provenance is available.

## Related diagnostics

- `SWBUILD2001` reports a well-formed but incompatible release identity.
