"""Central synthesis-purpose policy mapped to existing job semantics."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..jobs.policies import JobPriority, ReplayPolicy
from ..jobs.registry import policy_for as job_policy_for
from ..jobs.policies import JobType
from .models import SynthesisPurpose


@dataclass(frozen=True)
class SynthesisPurposePolicy:
    purpose: SynthesisPurpose
    priority: JobPriority
    default_deadline_seconds: int
    max_attempts: int
    replay_policy: ReplayPolicy
    fallback_allowed: bool
    last_known_good_allowed: bool
    suppress_on_failure: bool
    job_type: JobType | None = None

    @property
    def strict_deadline(self) -> bool:
        return self.purpose is SynthesisPurpose.ALERT

    @property
    def retry_or_replay_safe(self) -> bool:
        return self.replay_policy is not ReplayPolicy.NEVER


_ROUTINE_JOB = job_policy_for(JobType.TTS_SYNTHESIZE)
_ALERT_JOB = job_policy_for(JobType.ALERT_ARTIFACT_GENERATE)

_POLICIES = {
    SynthesisPurpose.ALERT: SynthesisPurposePolicy(
        SynthesisPurpose.ALERT,
        _ALERT_JOB.default_priority,
        30,
        _ALERT_JOB.retry.max_attempts,
        _ALERT_JOB.replay,
        True,
        True,
        False,
        JobType.ALERT_ARTIFACT_GENERATE,
    ),
    SynthesisPurpose.ROUTINE: SynthesisPurposePolicy(
        SynthesisPurpose.ROUTINE,
        _ROUTINE_JOB.default_priority,
        _ROUTINE_JOB.deadline.default_seconds or 180,
        _ROUTINE_JOB.retry.max_attempts,
        _ROUTINE_JOB.replay,
        True,
        True,
        False,
        JobType.TTS_SYNTHESIZE,
    ),
    SynthesisPurpose.OPTIONAL: SynthesisPurposePolicy(
        SynthesisPurpose.OPTIONAL, JobPriority.LOW, 120, 1, ReplayPolicy.REVALIDATE, False, False, True
    ),
    SynthesisPurpose.ADMINISTRATIVE: SynthesisPurposePolicy(
        SynthesisPurpose.ADMINISTRATIVE, JobPriority.NORMAL, 180, 1, ReplayPolicy.REVALIDATE, False, False, False
    ),
}


def policy_for(purpose: SynthesisPurpose) -> SynthesisPurposePolicy:
    try:
        return _POLICIES[purpose]
    except KeyError as exc:  # defensive if a new enum is added without policy
        raise ValueError(f"no synthesis policy for purpose {purpose!r}") from exc


def purpose_policies() -> tuple[SynthesisPurposePolicy, ...]:
    return tuple(_POLICIES[purpose] for purpose in SynthesisPurpose)


def deadline_for(purpose: SynthesisPurpose, now: dt.datetime | None = None) -> dt.datetime:
    """Return the purpose-owned default deadline for compatibility callers."""

    current = now or dt.datetime.now(dt.UTC)
    return current + dt.timedelta(seconds=policy_for(purpose).default_deadline_seconds)
