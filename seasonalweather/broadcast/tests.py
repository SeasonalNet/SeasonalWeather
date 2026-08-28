from __future__ import annotations

"""Shared helpers for locally-originated RWT/RMT messages and policy names."""

VALID_POSTPONE_POLICIES = frozenset(
    {
        "none",
        "fixed_delay",
        "delay_window",
        "next_day",
        "skip_day",
        "skip_week",
    }
)


def normalize_postpone_policy(value: object, default: str = "delay_window") -> str:
    """Return a supported postpone policy, falling back safely when omitted/invalid."""
    candidate = str(value or "").strip().lower().replace("-", "_")
    if candidate in VALID_POSTPONE_POLICIES:
        return candidate
    fallback = str(default or "").strip().lower().replace("-", "_")
    return fallback if fallback in VALID_POSTPONE_POLICIES else "delay_window"


def format_test_presentation_template(template: str, **ctx: str) -> str:
    tpl = str(template or "").strip()
    if not tpl:
        return ""
    try:
        return tpl.format(**ctx).strip()
    except Exception:
        return tpl


def default_test_script_lines(
    code: str,
    *,
    organization_name: str = "SeasonalNet",
    service_name: str = "IP Weather Radio station",
    station_name: str = "SeasonalWeather",
) -> list[str]:
    event_code = str(code or "").strip().upper()
    identity = f"This is the {organization_name} {service_name}, {station_name}."
    conclusion = f"This concludes the weekly test of the {organization_name} {service_name}, {station_name}."
    monthly_conclusion = (
        f"This concludes the required monthly test of the {organization_name} {service_name}, {station_name}."
    )
    if event_code == "RWT":
        return [
            identity,
            "The preceding signals were a test of this station's public warning system.",
            "During dangerous weather or other civil emergencies, specially equipped receivers and monitoring systems can be activated automatically by this signal to warn of an approaching hazard.",
            "Tests of this system are normally conducted on Wednesdays between 11 a.m. and noon.",
            "If severe weather threatens, the test may be postponed until the next available good weather day.",
            "Performance of your decoder, monitor, or alarm tone on this IP radio stream may vary depending on your monitoring setup.",
            "For hazardous watches and warnings affecting our service area, this stream uses the standard warning alarm tone of 1050 Hz.",
            "This broadcast also carries Specific Area Message Encoding, or SAME, allowing properly equipped receivers and software decoders to respond only to selected event codes and locations.",
            conclusion,
        ]
    if event_code == "RMT":
        return [
            identity,
            "The preceding signals were a test of this station's public warning system.",
            "During dangerous weather or other civil emergencies, specially equipped receivers and monitoring systems can be activated automatically by this signal to warn of an approaching hazard.",
            "This is the required monthly test of the SeasonalWeather public warning system.",
            "Monthly tests are conducted to verify the full operation of the alert origination chain under normal conditions.",
            "Performance of your decoder, monitor, or alarm tone on this IP radio stream may vary depending on your monitoring setup.",
            "For hazardous watches and warnings affecting our service area, this stream uses the standard warning alarm tone of 1050 Hz.",
            "This broadcast also carries Specific Area Message Encoding, or SAME, allowing properly equipped receivers and software decoders to respond only to selected event codes and locations.",
            monthly_conclusion,
        ]
    return []
