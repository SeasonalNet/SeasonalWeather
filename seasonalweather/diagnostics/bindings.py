"""Typed P1-11 configuration rule-to-catalog-code bindings."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, order=True)
class RuleCodeBinding:
    rule_id: str
    code: str
    phase: str


RULE_BINDINGS = (
    RuleCodeBinding("advisory.configuration", "SWCFG0001", "advisory"),
    RuleCodeBinding("admission.invalid", "SWCFG1021", "semantic"),
    RuleCodeBinding("compiler.issue_limit", "SWCFG7006", "schema"),
    RuleCodeBinding("compatibility.advisory", "SWCFG0003", "compatibility"),
    RuleCodeBinding("compatibility.unsupported", "SWCFG2003", "compatibility"),
    RuleCodeBinding("compatibility.degraded", "SWCFG4002", "compatibility"),
    RuleCodeBinding("preflight.degraded", "SWCFG4001", "preflight"),
    RuleCodeBinding("preflight.dependency_unavailable", "SWCFG3002", "preflight"),
    RuleCodeBinding("preflight.timeout", "SWCFG7007", "preflight"),
    RuleCodeBinding("schema.config_schema_type", "SWCFG1013", "schema"),
    RuleCodeBinding("schema.config_schema_unsupported", "SWCFG2001", "schema"),
    RuleCodeBinding("schema.enum", "SWCFG1016", "schema"),
    RuleCodeBinding("schema.max_length", "SWCFG1018", "schema"),
    RuleCodeBinding("schema.min_length", "SWCFG1017", "schema"),
    RuleCodeBinding("schema.required", "SWCFG1014", "schema"),
    RuleCodeBinding("schema.tuple_length", "SWCFG1019", "schema"),
    RuleCodeBinding("schema.type", "SWCFG1015", "schema"),
    RuleCodeBinding("schema.unknown_field", "SWCFG1020", "schema"),
    RuleCodeBinding("source.encoding", "SWCFG1001", "parse"),
    RuleCodeBinding("source.limit.bytes", "SWCFG7001", "parse"),
    RuleCodeBinding("source.limit.collection", "SWCFG7004", "parse"),
    RuleCodeBinding("source.limit.depth", "SWCFG7002", "parse"),
    RuleCodeBinding("source.limit.nodes", "SWCFG7003", "parse"),
    RuleCodeBinding("source.limit.scalar", "SWCFG7005", "parse"),
    RuleCodeBinding("source.read", "SWCFG3001", "parse"),
    RuleCodeBinding("semantic.invariant", "SWCFG2002", "semantic"),
    RuleCodeBinding("validation.report_rejected", "SWCFG2004", "compatibility"),
    RuleCodeBinding("yaml.alias", "SWCFG1008", "parse"),
    RuleCodeBinding("yaml.anchor", "SWCFG1007", "parse"),
    RuleCodeBinding("yaml.duplicate_key", "SWCFG1012", "parse"),
    RuleCodeBinding("yaml.empty", "SWCFG1002", "parse"),
    RuleCodeBinding("yaml.merge_key", "SWCFG1009", "parse"),
    RuleCodeBinding("yaml.multiple_documents", "SWCFG1004", "parse"),
    RuleCodeBinding("yaml.non_string_key", "SWCFG1006", "parse"),
    RuleCodeBinding("yaml.root_mapping", "SWCFG1010", "parse"),
    RuleCodeBinding("yaml.scalar_construction", "SWCFG1011", "parse"),
    RuleCodeBinding("yaml.syntax", "SWCFG1003", "parse"),
    RuleCodeBinding("yaml.tag", "SWCFG1005", "parse"),
    RuleCodeBinding("advisory.deprecated", "SWCFG0002", "deprecation"),
)

SEGMENT_BINDINGS = (
    RuleCodeBinding("segment.registry.invalid_definition", "SWSEG1001", "semantic"),
    RuleCodeBinding("segment.registry.policy_invariant", "SWSEG2001", "semantic"),
    RuleCodeBinding("segment.refresh_failed", "SWSEG3001", "runtime"),
    RuleCodeBinding("segment.fallback_used", "SWSEG4001", "recovery"),
    RuleCodeBinding("segment.publication_reconciliation", "SWSEG8001", "reconciliation"),
)

RUNTIME_CODES = MappingProxyType(
    {
        "optional_task_degraded": "SWRUN4001",
        "fatal_controller": "SWRUN5001",
        "prior_incomplete_shutdown": "SWRUN8001",
        "worker_diagnostic_rejected": "SWWP1001",
        "worker_diagnostic_incompatible": "SWWP2001",
    }
)

NWWS_CODES = MappingProxyType(
    {
        "malformed_message": "SWNWWS1001",
        "protocol_failure": "SWNWWS2001",
        "transport_failure": "SWNWWS3001",
        "tls_failure": "SWNWWS3002",
        "reconnect_degraded": "SWNWWS4001",
        "source_silent": "SWNWWS4002",
        "auth_failure": "SWNWWS6001",
        "lifecycle_deadline": "SWNWWS7001",
        "stale_generation": "SWNWWS8001",
        "lifecycle_failure": "SWNWWS8002",
    }
)

RELOAD_CODES = MappingProxyType(
    {
        "operator_action_required": "SWCFG0004",
        "unclassified_change": "SWCFG2005",
        "candidate_or_preparation_failed": "SWCFG3003",
        "retirement_pending": "SWCFG4003",
        "safe_point_timeout": "SWCFG7008",
        "reconciliation_required": "SWCFG8001",
    }
)

OBS_CODES = MappingProxyType(
    {
        "configuration_rejected": "SWOBS2001",
        "transport_failed": "SWOBS3001",
        "sink_degraded": "SWOBS4001",
        "destination_unauthorized": "SWOBS6001",
        "queue_dropped": "SWOBS7001",
    }
)

SEGMENT_CODES = MappingProxyType(
    {
        "refresh_failed": "SWSEG3001",
        "fallback_used": "SWSEG4001",
        "publication_reconciliation": "SWSEG8001",
    }
)

FOUNDATION_BINDINGS = (
    RuleCodeBinding("build.identity_invalid", "SWBUILD1001", "build"),
    RuleCodeBinding("build.compatibility_rejected", "SWBUILD2001", "startup"),
    RuleCodeBinding("cap.product_invalid", "SWCAP1001", "runtime"),
    RuleCodeBinding("cap.source_failed", "SWCAP3001", "runtime"),
    RuleCodeBinding("database.operation_failed", "SWDB3001", "runtime"),
    RuleCodeBinding("database.reconciliation_required", "SWDB8001", "recovery"),
    RuleCodeBinding("ern.transport_failed", "SWERN3001", "runtime"),
    RuleCodeBinding("ern.stream_degraded", "SWERN4001", "runtime"),
    RuleCodeBinding("job.contract_incompatible", "SWJOB2001", "admission"),
    RuleCodeBinding("job.reconciliation_required", "SWJOB8001", "recovery"),
    RuleCodeBinding("liquidsoap.control_failed", "SWLQS3001", "runtime"),
    RuleCodeBinding("liquidsoap.publication_reconciliation", "SWLQS8001", "recovery"),
    RuleCodeBinding("tts.response_invalid", "SWTTS1001", "runtime"),
    RuleCodeBinding("tts.provider_failed", "SWTTS3001", "runtime"),
    RuleCodeBinding("tts.fallback_used", "SWTTS4001", "recovery"),
    RuleCodeBinding("tts.trust_failed", "SWTTS6001", "runtime"),
    RuleCodeBinding("tts.deadline_exceeded", "SWTTS7001", "runtime"),
)

FOUNDATION_CODES = MappingProxyType({binding.rule_id: binding.code for binding in FOUNDATION_BINDINGS})

_BINDING_BY_RULE = MappingProxyType(
    {binding.rule_id: binding for binding in (*RULE_BINDINGS, *SEGMENT_BINDINGS, *FOUNDATION_BINDINGS)}
)


def binding_for_rule(rule_id: str) -> RuleCodeBinding:
    try:
        return _BINDING_BY_RULE[rule_id]
    except KeyError as exc:
        raise ValueError(f"unmapped diagnostic rule ID: {rule_id}") from exc


def code_for_rule(rule_id: str) -> str:
    return binding_for_rule(rule_id).code
