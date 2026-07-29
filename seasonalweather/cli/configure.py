from __future__ import annotations

import argparse
import copy
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path("/etc/seasonalweather/config.yaml")
DEFAULT_OUTPUT = Path("/etc/seasonalweather/config.yaml.new")
REPO_TEMPLATE = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


class Ui:
    def __init__(self, *, tui: str = "auto") -> None:
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()
        self.tool = "plain"
        if not self.interactive or tui == "0" or tui == "never":
            return
        dialog = shutil.which("dialog")
        whiptail = shutil.which("whiptail")
        if dialog:
            self.tool = "dialog"
            print("[/]: using dialog Terminal User Interface", file=sys.stderr)
        elif whiptail:
            self.tool = "whiptail"
            print("[/]: using whiptail Terminal User Interface", file=sys.stderr)
        else:
            print("[/]: dialog/whiptail not found, falling back to stdout/stdin and disabling Terminal User Interface", file=sys.stderr)
            if tui in {"1", "always", "required"}:
                raise SystemExit("--tui was requested, but neither dialog nor whiptail is installed")

    def menu(self, title: str, items: list[tuple[str, str]], *, default: str) -> str:
        if self.tool in {"dialog", "whiptail"}:
            args: list[str] = []
            for value, label in items:
                args.extend([value, label])
            if self.tool == "dialog":
                cmd = ["dialog", "--stdout", "--title", "SeasonalWeather configure", "--default-item", default, "--menu", title, "18", "78", "9", *args]
                proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE)
            else:
                cmd = ["whiptail", "--title", "SeasonalWeather configure", "--default-item", default, "--menu", title, "18", "78", "9", *args]
                proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                # whiptail writes the selection to stderr.
                proc.stdout = proc.stderr
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        return self._plain_menu(title, items, default=default)

    def _plain_menu(self, title: str, items: list[tuple[str, str]], *, default: str) -> str:
        print(file=sys.stderr)
        print(title, file=sys.stderr)
        for idx, (_value, label) in enumerate(items, start=1):
            print(f"  {idx}) {label}", file=sys.stderr)
        value_by_num = {str(idx): value for idx, (value, _label) in enumerate(items, start=1)}
        values = {value for value, _label in items}
        while True:
            print(f"Select [{default}]: ", end="", file=sys.stderr, flush=True)
            answer = sys.stdin.readline().strip() or default
            if answer in value_by_num:
                return value_by_num[answer]
            if answer in values:
                return answer
            print(f"[!] Invalid selection: {answer}", file=sys.stderr)

    def input(self, prompt: str, *, default: str = "") -> str:
        if self.tool in {"dialog", "whiptail"}:
            if self.tool == "dialog":
                cmd = ["dialog", "--stdout", "--title", "SeasonalWeather configure", "--inputbox", prompt, "9", "78", default]
                proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE)
            else:
                cmd = ["whiptail", "--title", "SeasonalWeather configure", "--inputbox", prompt, "9", "78", default]
                proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                proc.stdout = proc.stderr
            if proc.returncode == 0:
                return proc.stdout.rstrip("\n")
        print(f"{prompt} [{default}]: ", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline().strip()
        return answer if answer else default

    def yesno(self, prompt: str, *, default: bool = False) -> bool:
        default_label = "Y" if default else "N"
        if self.tool in {"dialog", "whiptail"}:
            extra = [] if default else ["--defaultno"]
            if self.tool == "dialog":
                cmd = ["dialog", "--title", "SeasonalWeather configure", *extra, "--yesno", prompt, "8", "78"]
            else:
                cmd = ["whiptail", "--title", "SeasonalWeather configure", *extra, "--yesno", prompt, "8", "78"]
            proc = subprocess.run(cmd)
            if proc.returncode in {0, 1}:
                return proc.returncode == 0
        while True:
            print(f"{prompt} [{default_label}]: ", end="", file=sys.stderr, flush=True)
            answer = sys.stdin.readline().strip().lower() or default_label.lower()
            if answer in {"y", "yes", "1", "true"}:
                return True
            if answer in {"n", "no", "0", "false"}:
                return False
            print("[!] Enter yes or no.", file=sys.stderr)


def _get(data: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def _set(data: dict[str, Any], path: str, value: Any) -> None:
    cur = data
    parts = path.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _csv(value: str) -> list[str]:
    return [item.strip().upper() for item in value.replace("\n", ",").split(",") if item.strip()]


def _safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def load_yaml(path: Path) -> dict[str, Any]:
    from seasonalweather.configuration.source import SourceDocument
    from seasonalweather.configuration.yaml_parser import parse_document

    source = SourceDocument.read(path)
    parsed = parse_document(source)
    if parsed.parsed is None:
        rule_ids = ", ".join(issue.rule_id for issue in parsed.issues)
        raise RuntimeError(f"configuration YAML could not be parsed: {rule_ids}")
    return dict(parsed.parsed.value)


def validate_candidate(path: Path) -> None:
    # load_config() requires secrets from the environment. Provide safe validation
    # placeholders when the operator has not filled seasonalweather.env yet.
    environment = dict(os.environ)
    environment.setdefault("ICECAST_SOURCE_PASSWORD", "validation-source")
    if not environment.get("SEASONAL_API_TOKEN") and not environment.get(
        "SEASONAL_API_TOKENS_JSON"
    ):
        environment["SEASONAL_API_TOKEN"] = os.urandom(24).hex()
    try:
        from seasonalweather.configuration.loader import load_runtime_config

        load_runtime_config(str(path), environ=environment)
    except Exception as exc:
        raise RuntimeError(f"generated config did not load cleanly: {exc}") from exc


def apply_profile(data: dict[str, Any], profile: str) -> None:
    if profile == "minimal-lab":
        _set(data, "nwws.enabled", False)
        _set(data, "cap.enabled", False)
        _set(data, "ipaws.enabled", False)
        _set(data, "ern.enabled", False)
        _set(data, "tts.backend", "espeak-ng")
    elif profile == "standard":
        _set(data, "tts.backend", "espeak-ng")
        _set(data, "same.enabled", True)
    elif profile == "voicetext-paul":
        _set(data, "tts.backend", "voicetext_paul")
        _set(data, "tts.voice", "9")
        _set(data, "same.enabled", True)
    elif profile == "dectalk":
        _set(data, "tts.backend", "dectalk")
        _set(data, "tts.voice", "0")
        _set(data, "same.enabled", True)


def configure(
    data: dict[str, Any],
    ui: Ui,
    *,
    profile: str | None = None,
    default_profile: str = "advanced",
) -> dict[str, Any]:
    out = copy.deepcopy(data)

    if profile is None:
        profile = ui.menu(
            "Configuration profile",
            [
                ("advanced", "Keep current profile and only prompt common fields"),
                ("minimal-lab", "Minimal lab/test station"),
                ("standard", "Standard weather-radio station"),
                ("voicetext-paul", "VoiceText Paul production station"),
                ("dectalk", "DECtalk station"),
            ],
            default=default_profile,
        )
    apply_profile(out, profile)

    _set(out, "station.name", ui.input("Station name", default=str(_get(out, "station.name", "SeasonalWeather"))))
    _set(out, "station.service_area_name", ui.input("Spoken service area name", default=str(_get(out, "station.service_area_name", "your service area"))))
    _set(out, "station.timezone", ui.input("IANA timezone", default=str(_get(out, "station.timezone", "America/New_York"))))

    deployment = ui.menu(
        "Deployment type",
        [
            ("land", "Land station"),
            ("coastal", "Coastal/marine-only station"),
            ("land_coastal", "Land + coastal waters"),
            ("land_marine", "Land + inland/bay waters"),
            ("marine", "Marine-only station"),
        ],
        default=str(_get(out, "station.deployment_type", "land")),
    )
    _set(out, "station.deployment_type", deployment)

    tts_backend = ui.menu(
        "TTS backend",
        [
            ("espeak-ng", "espeak-ng fallback"),
            ("voicetext_paul", "VoiceText Paul"),
            ("dectalk", "DECtalk"),
            ("festival", "Festival"),
            ("piper", "Piper"),
        ],
        default=str(_get(out, "tts.backend", "espeak-ng")),
    )
    _set(out, "tts.backend", tts_backend)
    _set(out, "tts.voice", ui.input("TTS voice", default=str(_get(out, "tts.voice", "9"))))
    _set(out, "tts.rate_wpm", _safe_int(ui.input("TTS rate words per minute", default=str(_get(out, "tts.rate_wpm", 165))), 165))

    color = ui.menu(
        "Runtime log color mode",
        [
            ("never", "Never emit ANSI escape codes"),
            ("auto", "Only color when stdout is a TTY"),
            ("always", "Always color, including journalctl streams"),
        ],
        default=str(_get(out, "logs.runtime.color", "never")),
    )
    _set(out, "logs.runtime.color", color)

    _set(out, "nwws.enabled", ui.yesno("Enable NWWS-OI ingest?", default=bool(_get(out, "nwws.enabled", True))))
    wfos = ui.input("Allowed WFOs, comma-separated", default=",".join(_get(out, "nwws.allowed_wfos", []) or []))
    _set(out, "nwws.allowed_wfos", _csv(wfos))

    obs = ui.input("Observation stations, comma-separated", default=",".join(_get(out, "observations.stations", []) or []))
    _set(out, "observations.stations", _csv(obs))

    _set(out, "stream.icecast_mount", ui.input("Icecast mount", default=str(_get(out, "stream.icecast_mount", "/seasonalweather.ogg"))))
    _set(out, "api.allow_remote", ui.yesno("Allow API listener on remote interfaces?", default=bool(_get(out, "api.allow_remote", False))))

    return out


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or apply a SeasonalWeather config.yaml candidate.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="existing config to use as input")
    parser.add_argument("--template", type=Path, default=REPO_TEMPLATE, help="template used when --config does not exist")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="candidate config output path")
    parser.add_argument("--apply", action="store_true", help="backup --config and replace it with the candidate")
    parser.add_argument("--print", dest="print_config", action="store_true", help="print generated YAML to stdout instead of writing")
    parser.add_argument(
        "--profile",
        choices=["advanced", "minimal-lab", "standard", "voicetext-paul", "dectalk"],
        default=os.environ.get("SEASONAL_CONFIGURE_PROFILE"),
        help="configuration profile to apply without prompting",
    )
    parser.add_argument("--tui", choices=["auto", "always", "never", "0", "1"], default=os.environ.get("SEASONAL_CONFIGURE_TUI", "auto"))
    args = parser.parse_args(argv)

    config_exists = args.config.exists()
    source = args.config if config_exists else args.template
    data = load_yaml(source)
    ui = Ui(tui=args.tui)
    default_profile = "advanced" if config_exists else "standard"
    candidate = configure(data, ui, profile=args.profile, default_profile=default_profile)

    if args.print_config:
        sys.stdout.write(yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True))
        return 0

    write_yaml(args.output, candidate)
    validate_candidate(args.output)
    print(f"[+] Wrote candidate config: {args.output}")
    print("[!] Candidate YAML is generated output; comments from the source config are not preserved.")

    if args.apply or ui.yesno(f"Apply candidate to {args.config}?", default=False):
        if args.config.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = args.config.with_name(f"{args.config.name}.bak-{stamp}")
            shutil.copy2(args.config, backup)
            print(f"[+] Backed up existing config: {backup}")
        args.config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.output, args.config)
        print(f"[+] Applied config: {args.config}")
    else:
        print(f"Review with: diff -u {args.config} {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
