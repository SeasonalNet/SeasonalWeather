# SWBUILD2001 — Build or release compatibility was rejected

## Meaning

The selected image or release contract is incompatible with the deployment.

## Trigger

Version, image profile, protocol, catalog, or provenance identity fails the
current compatibility policy.

## Correction or recovery

Select compatible controller and worker artifacts from one release family.

## Operational effect

Startup or deployment admission is blocked until the release contract matches.

## Rationale

Mixed release authorities can invalidate worker, catalog, and artifact fences.

## Alternatives or migration

Complete a controlled rolling upgrade or roll back all coupled images together.

## Related diagnostics

- `SWBUILD1001` reports malformed build identity metadata.
