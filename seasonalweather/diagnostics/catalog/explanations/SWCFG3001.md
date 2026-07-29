# SWCFG3001 — Configuration source cannot be read

## Meaning
The selected configuration path cannot be opened for bounded reading.

## Trigger
A missing file, denied permission, or unreadable filesystem path can trigger this condition.

## Correction or recovery
Verify the exact path and grant the service account read access without exposing secrets.

## Operational effect
Compilation cannot begin and no candidate is activated.

## Rationale
The compiler reports a value-free dependency failure rather than leaking host exception details.

## Alternatives or migration
Lint a repository example with an explicit path to distinguish path access from document content.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
