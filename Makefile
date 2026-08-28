.DEFAULT_GOAL := check

PYTHON ?= $(if $(wildcard .venv/bin/python),./.venv/bin/python,python3)
BUILD_INFO ?= build/build-info.json
BUILD_PROFILE ?= source
TARGET_PLATFORM ?= unknown

.PHONY: format-check lint typecheck basedpyright architecture-check dependency-check suppressions-check
.PHONY: dead-code-check security-check complexity-check image-boundaries-check container-security-check
.PHONY: exceptions-check diagnostics-check diagnostics-build diagnostics-export
.PHONY: quality test compile check phase2-gate phase2-images phase3-gate build-info version image images compose-check staging-check release

DIAGNOSTICS_EXPORT_DIR ?= build/diagnostics
QUALITY_SUPPRESSIONS_BASE ?= HEAD

format-check:
	$(PYTHON) -m tools.quality.run_check format

lint:
	$(PYTHON) -m tools.quality.run_check lint

typecheck:
	$(PYTHON) -m tools.quality.run_check typecheck

basedpyright:
	$(PYTHON) -m tools.quality.run_check basedpyright

architecture-check:
	$(PYTHON) -m tools.quality.architecture_check
	$(PYTHON) -m pytest -q tests/test_architecture_check.py tests/test_quality_tooling.py tests/test_p1_23_decomposition.py

dependency-check:
	$(PYTHON) -m tools.quality.run_check dependency

suppressions-check:
	$(PYTHON) -m tools.quality.suppressions_check --base-ref "$(QUALITY_SUPPRESSIONS_BASE)"

dead-code-check:
	$(PYTHON) -m tools.quality.run_check dead-code

security-check:
	$(PYTHON) -m tools.quality.run_check security

complexity-check:
	$(PYTHON) -m tools.quality.run_check complexity

image-boundaries-check:
	$(PYTHON) -m tools.quality.image_boundaries_check

container-security-check:
	$(PYTHON) -m tools.quality.container_security_check

exceptions-check:
	$(PYTHON) -m tools.quality.validate_governance

diagnostics-check:
	$(PYTHON) -m seasonalweather.diagnostics.compiler check

diagnostics-build:
	$(PYTHON) -m seasonalweather.diagnostics.compiler build

diagnostics-export:
	$(PYTHON) -m seasonalweather diagnostics export --output $(DIAGNOSTICS_EXPORT_DIR)

quality: exceptions-check diagnostics-check format-check lint typecheck basedpyright architecture-check dependency-check suppressions-check dead-code-check security-check complexity-check image-boundaries-check container-security-check

test:
	$(PYTHON) -m pytest

compile:
	PYTHONPYCACHEPREFIX="$(CURDIR)/build/pycache" $(PYTHON) -m compileall -q seasonalweather tools tests

check: quality compile test

phase2-gate: check
	$(MAKE) phase2-images

phase2-images:
	$(MAKE) images
	$(PYTHON) -m tools.quality.phase2_exit_gate --images

phase3-gate: check compose-check

build-info:
	$(PYTHON) -m seasonalweather.build_metadata \
		--output "$(BUILD_INFO)" \
		--repo-root "$(CURDIR)" \
		--profile "$(BUILD_PROFILE)" \
		--target-platform "$(TARGET_PLATFORM)" \
		$(if $(SOURCE_DATE_EPOCH),--source-date-epoch "$(SOURCE_DATE_EPOCH)") \
		$(if $(BUILD_ID),--build-id "$(BUILD_ID)")

version:
	$(PYTHON) -m seasonalweather version --json

image:
	$(MAKE) BUILD_PROFILE=controller build-info
	$(PYTHON) -m tools.build_interface image --build-info "$(BUILD_INFO)" --target controller

images:
	@set -e; \
	for profile in controller routine-worker piper legacy-tts voicetext-paul spfy maintenance development; do \
		$(MAKE) BUILD_PROFILE="$$profile" build-info; \
		$(PYTHON) -m tools.build_interface image --build-info "$(BUILD_INFO)" --target "$$profile"; \
	done

compose-check:
	$(PYTHON) -m tools.build_interface compose-check

staging-check:
	$(PYTHON) -m tools.staging_interface config

release:
	@test -n "$(SOURCE_DATE_EPOCH)" || (echo "SOURCE_DATE_EPOCH is required for release provenance" >&2; exit 1)
	@test -z "$$(git status --porcelain)" || (echo "release requires a clean working tree" >&2; exit 1)
	$(MAKE) BUILD_PROFILE=release build-info
