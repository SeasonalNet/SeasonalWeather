# SWCFG7006 — Additional configuration issues were omitted

## Meaning
The compiler reached its maximum number of reported issues.

## Trigger
A candidate with more than one hundred independent schema errors triggers this summary condition.

## Correction or recovery
Fix the reported issues, then lint again to reveal any remaining findings.

## Operational effect
Compilation remains blocked and output stays bounded.

## Rationale
Bounded reports prevent malformed input from producing unmanageable diagnostics.

## Alternatives or migration
Correct candidates incrementally, starting with parse and top-level schema errors.

## Related diagnostics
The preceding source-addressed diagnostics in the same report identify the first bounded findings.
