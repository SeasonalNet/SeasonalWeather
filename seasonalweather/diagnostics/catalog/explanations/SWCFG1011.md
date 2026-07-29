# SWCFG1011 — YAML scalar cannot be represented safely

## Meaning
A scalar cannot be constructed within the bounded YAML 1.2 core rules.

## Trigger
An extremely large numeric scalar may exceed safe language conversion limits.

## Correction or recovery
Replace it with a value of the documented type and operational range.

## Operational effect
The candidate is rejected without exposing the original scalar in diagnostics.

## Rationale
Bounded construction prevents pathological input from consuming unbounded resources.

## Alternatives or migration
Quote identifier-like values that are text rather than numeric configuration.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
