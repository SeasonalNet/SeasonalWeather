# SWCFG0004 — Configuration reload requires operator action

## Meaning
A valid candidate was not applied because an explicit operator action is required.

## Trigger
The complete candidate requires a process restart or exact warning acknowledgment.

## Correction or recovery
Review the audit, then restart normally or resubmit the unchanged candidate with its fenced acknowledgment.

## Operational effect
The active configuration and generation remain unchanged.

## Rationale
Validation does not imply safe runtime applicability.

## Alternatives or migration
Use a dry run to review the complete redacted disposition before acting.

## Related diagnostics
See `SWCFG2005` for an unclassified change.
