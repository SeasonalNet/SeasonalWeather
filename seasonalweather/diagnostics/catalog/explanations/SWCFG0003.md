# SWCFG0003 — Software identity is compatible with an advisory

## Meaning
The validator software version is inside the supported range but differs from
the preferred compatibility line.

## Trigger
Compatibility analysis accepts the semantic version while classifying its
major or minor line as advisory.

## Correction or recovery
Use the preferred supported software line when practical. The current identity
remains admissible unless local warning policy says otherwise.

## Operational effect
The candidate remains compatible. This finding is a nonblocking suggestion.

## Rationale
A compatible identity must not use the incompatible `SWCFG2003` condition.

## Alternatives or migration
Keep the accepted version until a normal upgrade window if no preferred build
is available.

## Related diagnostics
See `SWCFG2003` for software and protocol identities outside supported bounds.
