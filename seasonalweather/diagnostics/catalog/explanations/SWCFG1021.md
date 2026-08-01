# SWCFG1021 — Admission input is invalid

## Meaning
An objectively validated bounded field cannot be admitted safely.

## Trigger
A configuration, job payload, authentication request, upload, insert, or generic payload field violates its owning contract.

## Correction or recovery
Correct the identified typed path using the owning contract's limits and supported values.

## Operational effect
The input is rejected before it can affect broadcast or controller state.

## Rationale
One bounded diagnostic shape preserves precise field paths without importing future subsystem implementations.

## Alternatives or migration
Use the existing owning validator and translate only its bounded result.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
