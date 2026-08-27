# Installer and configuration assistant

SeasonalWeather is intended to run on a modern Debian server VM. The bootstrapper
is interactive by default when attached to a terminal, while still supporting
non-interactive automation through flags and environment variables.

## Bootstrap profiles

Run from a staging clone, not directly from `/opt/seasonalweather/app`:

```bash
sudo bash scripts/00-bootstrap.sh
```

Profiles:

- `minimal` — base daemon only; no optional TTS packages and no `samedec`.
- `standard` — base daemon, `espeak-ng` fallback TTS, and `samedec`.
- `voicetext-paul` — base daemon, VoiceText Paul/Wine runtime, and `samedec`.
- `dectalk` — base daemon, DECtalk build/install path, and `samedec`.
- `custom` — prompt for each feature.

The bootstrapper tries to use `dialog` or `whiptail` when available. If neither
is present, it prints a fallback notice and uses numbered stdout/stdin prompts:

```text
[/]: dialog/whiptail not found, falling back to stdout/stdin and disabling Terminal User Interface
```

Automation should use `--non-interactive` and explicit feature flags:

```bash
SEASONAL_INSTALL_PROFILE=voicetext-paul sudo -E bash scripts/00-bootstrap.sh --non-interactive
SEASONAL_PIPER=1 sudo -E bash scripts/00-bootstrap.sh --non-interactive
SEASONAL_SAMEDEC=0 sudo -E bash scripts/00-bootstrap.sh --non-interactive
```

## Dependency groups

Base Python dependencies intentionally exclude the Piper stack. The base install
no longer pulls `piper-tts`, `onnxruntime`, or `numpy` unless Piper is selected.

Install-time feature flags map to dependency groups:

- `SEASONAL_ESPEAK=1` installs `espeak-ng`.
- `SEASONAL_FESTIVAL=1` installs `festival` and `festvox-kallpc16k`.
- `SEASONAL_PIPER=1` enables the `piper` Python dependency group in the venv.
- `SEASONAL_DECTALK=1` installs DECtalk build dependencies and builds DECtalk.
- `SEASONAL_VOICETEXT_PAUL=1` installs the VoiceText Paul/Wine runtime path.
- `SEASONAL_SAMEDEC=1` installs the pinned Rust `samedec` decoder.

## Configuration assistant

After bootstrap, use:

```bash
sudo seasonalweather-configure
```

The assistant reads `/etc/seasonalweather/config.yaml` when it exists, otherwise
it uses the repo template. Existing live configs default to the `advanced`
profile, which preserves the current profile/backend values and only prompts for
common fields. Fresh template-driven configs default to the `standard` profile.
It writes a candidate config to `/etc/seasonalweather/config.yaml.new`, validates
that SeasonalWeather can load it, and offers to back up and apply it.

To force a fresh profile over an existing config, pass `--profile`, for example:

```bash
sudo seasonalweather-configure --profile voicetext-paul
```

This is a profile-driven assistant, not a full editor for every YAML key. Manual
editing remains supported and expected for advanced deployments.

Generated candidate YAML does not preserve comments from the source file. Keep
that in mind before replacing a hand-commented live config.
