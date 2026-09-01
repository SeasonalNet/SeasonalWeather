// P2-01 declares the image matrix and shared provenance inputs.
// P2-02 and P2-03 provide the authoritative Dockerfiles for these targets.

variable "SW_PROJECT" { default = "seasonalweather" }
variable "SW_VERSION" { default = "0.18.0" }
variable "RUST_IMAGE" { default = "rust:1.85-bookworm" }
variable "SAMEDEC_VERSION" { default = "0.4.2" }
variable "DECTALK_SOURCE_URL" { default = "https://github.com/dectalk/dectalk/archive/refs/tags/2023-10-30.tar.gz" }
variable "DECTALK_SOURCE_SHA256" { default = "511c845e453917eea3a353cdbe8e0401360d8992dcfadfe2e9b4b83fde168f7e" }
variable "SW_BUILD_ID" { default = "unbuilt" }
variable "SW_BUILD_IDENTITY" { default = "seasonalweather-0.18.0" }
variable "SW_GIT_COMMIT" { default = "unknown" }
variable "SW_GIT_DESCRIBE" { default = "source" }
variable "SW_DIRTY_TREE" { default = "false" }
variable "SW_BUILD_SOURCE_TIMESTAMP" { default = "" }
variable "SW_SOURCE_DATE_EPOCH" { default = "" }
variable "SW_IMAGE_PROFILE" { default = "source" }
variable "SW_TARGET_PLATFORM" { default = "unknown" }
variable "SW_PYTHON_VERSION" { default = "unknown" }
variable "SW_SWWP_PROTOCOL_VERSIONS" { default = "1" }
variable "SW_JOB_PAYLOAD_SCHEMA_VERSIONS" { default = "1" }
variable "SW_JOB_RESULT_SCHEMA_VERSIONS" { default = "1" }
variable "SW_VALIDATION_PROTOCOL_VERSIONS" { default = "1" }
variable "SW_CONFIG_SCHEMA_MIN" { default = "1" }
variable "SW_CONFIG_SCHEMA_MAX" { default = "1" }
variable "SW_DIAGNOSTIC_SCHEMA_VERSION" { default = "1" }
variable "SW_DIAGNOSTIC_CATALOG_VERSION" { default = "1" }
variable "SW_CAPABILITY_MANIFEST_VERSION" { default = "1" }

group "default" {
  targets = ["controller"]
}

target "common" {
  context = "."
  dockerfile = "Dockerfile"
  args = {
    RUST_IMAGE = RUST_IMAGE
    SAMEDEC_VERSION = SAMEDEC_VERSION
    DECTALK_SOURCE_URL = DECTALK_SOURCE_URL
    DECTALK_SOURCE_SHA256 = DECTALK_SOURCE_SHA256
    SW_PROJECT = SW_PROJECT
    SW_VERSION = SW_VERSION
    SW_BUILD_ID = SW_BUILD_ID
    SW_BUILD_IDENTITY = SW_BUILD_IDENTITY
    SW_GIT_COMMIT = SW_GIT_COMMIT
    SW_GIT_DESCRIBE = SW_GIT_DESCRIBE
    SW_DIRTY_TREE = SW_DIRTY_TREE
    SW_BUILD_SOURCE_TIMESTAMP = SW_BUILD_SOURCE_TIMESTAMP
    SW_SOURCE_DATE_EPOCH = SW_SOURCE_DATE_EPOCH
    SW_IMAGE_PROFILE = SW_IMAGE_PROFILE
    SW_TARGET_PLATFORM = SW_TARGET_PLATFORM
    SW_PYTHON_VERSION = SW_PYTHON_VERSION
    SW_SWWP_PROTOCOL_VERSIONS = SW_SWWP_PROTOCOL_VERSIONS
    SW_JOB_PAYLOAD_SCHEMA_VERSIONS = SW_JOB_PAYLOAD_SCHEMA_VERSIONS
    SW_JOB_RESULT_SCHEMA_VERSIONS = SW_JOB_RESULT_SCHEMA_VERSIONS
    SW_VALIDATION_PROTOCOL_VERSIONS = SW_VALIDATION_PROTOCOL_VERSIONS
    SW_CONFIG_SCHEMA_MIN = SW_CONFIG_SCHEMA_MIN
    SW_CONFIG_SCHEMA_MAX = SW_CONFIG_SCHEMA_MAX
    SW_DIAGNOSTIC_SCHEMA_VERSION = SW_DIAGNOSTIC_SCHEMA_VERSION
    SW_DIAGNOSTIC_CATALOG_VERSION = SW_DIAGNOSTIC_CATALOG_VERSION
    SW_CAPABILITY_MANIFEST_VERSION = SW_CAPABILITY_MANIFEST_VERSION
  }
  labels = {
    "org.opencontainers.image.title" = SW_PROJECT
    "org.opencontainers.image.version" = SW_VERSION
    "org.opencontainers.image.revision" = SW_GIT_COMMIT
    "org.opencontainers.image.created" = SW_BUILD_SOURCE_TIMESTAMP
    "org.opencontainers.image.ref.name" = SW_GIT_DESCRIBE
    "io.seasonalweather.build.id" = SW_BUILD_ID
    "io.seasonalweather.build.identity" = SW_BUILD_IDENTITY
    "io.seasonalweather.build.dirty" = SW_DIRTY_TREE
    "io.seasonalweather.build.profile" = SW_IMAGE_PROFILE
    "io.seasonalweather.build.target-platform" = SW_TARGET_PLATFORM
    "io.seasonalweather.build.source-date-epoch" = SW_SOURCE_DATE_EPOCH
    "io.seasonalweather.schema.swwp" = SW_SWWP_PROTOCOL_VERSIONS
    "io.seasonalweather.schema.job-payload" = SW_JOB_PAYLOAD_SCHEMA_VERSIONS
    "io.seasonalweather.schema.job-result" = SW_JOB_RESULT_SCHEMA_VERSIONS
    "io.seasonalweather.schema.validation" = SW_VALIDATION_PROTOCOL_VERSIONS
    "io.seasonalweather.schema.configuration" = "${SW_CONFIG_SCHEMA_MIN}-${SW_CONFIG_SCHEMA_MAX}"
    "io.seasonalweather.schema.diagnostics" = SW_DIAGNOSTIC_SCHEMA_VERSION
    "io.seasonalweather.schema.catalog" = SW_DIAGNOSTIC_CATALOG_VERSION
    "io.seasonalweather.schema.capability-manifest" = SW_CAPABILITY_MANIFEST_VERSION
  }
}

target "controller" {
  inherits = ["common"]
  args = { SW_IMAGE_PROFILE = "controller" }
  labels = { "io.seasonalweather.build.profile" = "controller" }
  tags = ["seasonalweather:standard"]
}

target "routine-worker" {
  inherits = ["common"]
  dockerfile = "Dockerfile.worker"
  args = { SW_IMAGE_PROFILE = "routine-worker" }
  labels = { "io.seasonalweather.build.profile" = "routine-worker" }
  tags = ["seasonalweather-worker:standard"]
}

target "piper" {
  inherits = ["common"]
  dockerfile = "Dockerfile.worker"
  args = { SW_IMAGE_PROFILE = "piper" }
  labels = { "io.seasonalweather.build.profile" = "piper" }
  tags = ["seasonalweather-worker:piper"]
}

target "espeak" {
  inherits = ["common"]
  dockerfile = "Dockerfile.worker"
  args = { SW_IMAGE_PROFILE = "espeak" }
  labels = { "io.seasonalweather.build.profile" = "espeak" }
  tags = ["seasonalweather-worker:espeak"]
}

target "festival" {
  inherits = ["common"]
  dockerfile = "Dockerfile.worker"
  args = { SW_IMAGE_PROFILE = "festival" }
  labels = { "io.seasonalweather.build.profile" = "festival" }
  tags = ["seasonalweather-worker:festival"]
}

target "dectalk" {
  inherits = ["common"]
  dockerfile = "Dockerfile.worker"
  args = { SW_IMAGE_PROFILE = "dectalk" }
  labels = { "io.seasonalweather.build.profile" = "dectalk" }
  tags = ["seasonalweather-worker:dectalk"]
}

target "legacy-tts" {
  inherits = ["common"]
  dockerfile = "Dockerfile.worker"
  args = { SW_IMAGE_PROFILE = "legacy-tts" }
  labels = { "io.seasonalweather.build.profile" = "legacy-tts" }
  tags = ["seasonalweather-worker:legacy-tts"]
}

target "voicetext-paul" {
  inherits = ["common"]
  dockerfile = "Dockerfile.worker"
  args = { SW_IMAGE_PROFILE = "voicetext-paul" }
  labels = { "io.seasonalweather.build.profile" = "voicetext-paul" }
  tags = ["seasonalweather-worker:voicetext-paul"]
}

target "spfy" {
  inherits = ["common"]
  dockerfile = "Dockerfile.worker"
  args = { SW_IMAGE_PROFILE = "spfy" }
  labels = { "io.seasonalweather.build.profile" = "spfy" }
  tags = ["seasonalweather-worker:spfy"]
}

target "maintenance" {
  inherits = ["common"]
  dockerfile = "Dockerfile.worker"
  args = { SW_IMAGE_PROFILE = "maintenance" }
  labels = { "io.seasonalweather.build.profile" = "maintenance" }
  tags = ["seasonalweather-worker:maintenance"]
}

target "development" {
  inherits = ["common"]
  dockerfile = "Dockerfile.worker"
  args = { SW_IMAGE_PROFILE = "development" }
  labels = { "io.seasonalweather.build.profile" = "development" }
  tags = ["seasonalweather:development"]
}
