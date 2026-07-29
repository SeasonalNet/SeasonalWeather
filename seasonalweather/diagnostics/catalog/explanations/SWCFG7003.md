# SWCFG7003 — Configuration contains too many YAML nodes

## Meaning
The document exceeds the total parsed-node limit.

## Trigger
A generated file containing tens of thousands of scalar entries can trigger this condition.

## Correction or recovery
Remove duplicated or unsupported generated content.

## Operational effect
The candidate is rejected before full construction.

## Rationale
Node bounds prevent disproportionate CPU and memory use from configuration input.

## Alternatives or migration
Place dynamic operational records in their authoritative store, not in YAML configuration.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
