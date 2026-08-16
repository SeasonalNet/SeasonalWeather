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
)

_BINDING_BY_RULE = MappingProxyType(
    {binding.rule_id: binding for binding in (*RULE_BINDINGS, *SEGMENT_BINDINGS)}
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


def binding_for_rule(rule_id: str) -> RuleCodeBinding:
    try:
        return _BINDING_BY_RULE[rule_id]
    except KeyError as exc:
        raise ValueError(f"unmapped diagnostic rule ID: {rule_id}") from exc


def code_for_rule(rule_id: str) -> str:
    return binding_for_rule(rule_id).code
