# Runtime wrappers

SeasonalWeather installs a small set of helper wrappers into `/usr/local/bin` during bootstrap.
These wrappers are part of the deployed runtime contract and are version-controlled in
`scripts/wrappers/` within this repo.

## Source of truth

- Repo source: `scripts/wrappers/`
- Installed runtime path: `/usr/local/bin/`
- Installer: `scripts/00-bootstrap.sh`

Do not hand-edit the installed copies on the target host and assume the repo now matches.
If a wrapper needs to change, change the repo copy first and re-run bootstrap.

## Installed wrappers

### DECtalk

- `/usr/local/bin/dectalk-env`
- `/usr/local/bin/dectalk-text2wav`

Bootstrap installs these when `SEASONAL_DECTALK=1` is set.

The canonical DECtalk `say` path used by this repo is:

```text
/opt/dectalk/dectalk/dist/say
```

`dectalk-env` sets the DECtalk runtime library path and then execs the requested command.
`dectalk-text2wav` is the stable wrapper interface for converting text to a WAV file.

Relevant environment variables:

- `DECTALK_DIST`
- `DECTALK_SAY_BIN`
- `DECTALK_VOICE`
- `DECTALK_RATE_WPM`

### VoiceText Paul

- `/usr/local/bin/voicetext_paul_synth`
- `/usr/local/bin/voicetext_paul_wineserver_kill`

Bootstrap installs these when `SEASONAL_VOICETEXT_PAUL=1` is set.

The synth wrapper is invoked directly by the owning SeasonalWeather process and
is expected to:

- read input text from stdin
- write `output.wav` in the VoiceText Paul engine binary directory
- serialize access with a lock so concurrent synth jobs do not clobber `input1.txt` and `output.wav`
- preserve useful failure artifacts when Wine crashes or times out

The wineserver-kill wrapper takes the same lock before calling `wineserver -k` so it does not kill Wine mid-synthesis.

Repo-default VoiceText state uses the canonical SeasonalWeather state root under `/var/lib/seasonalweather`.
Deployments that want alternate storage should redirect `/var/lib/seasonalweather` with a symlink or mount, rather than hardcoding the backing path into repo code or wrapper defaults.

Fresh VoiceText installs also ship a persistent headless display service at `seasonalweather-voicetext-xvfb.service`, and the wrappers default `DISPLAY` to `:99`.

Relevant environment variables:

- `SEASONALWEATHER_DATA_BASE`
- `VOICETEXT_PAUL_ENGINE_ROOT`
- `VOICETEXT_PAUL_BIN_DIR`
- `VOICETEXT_PAUL_PREFIX_BASE`
- `VOICETEXT_PAUL_PREFIX_NAME`
- `VOICETEXT_PAUL_WINEPREFIX`
- `VOICETEXT_PAUL_TMPDIR`
- `VOICETEXT_PAUL_LOCK_PATH`
- `VOICETEXT_PAUL_WINEDEBUG`
- `VOICETEXT_PAUL_WINEESYNC`
- `VOICETEXT_PAUL_WINEFSYNC`
- `VOICETEXT_PAUL_DISABLE_WRITE_WATCH`
- `VOICETEXT_PAUL_LOCK_WAIT`
- `VOICETEXT_PAUL_CORE`
- `VOICETEXT_DEBUG`
- `VOICETEXT_VTML_PARA_PAUSE_MS`

## Deployment workflow

SeasonalWeather's bootstrapper expects to run from a clone in a staging location, then rsyncs the repo into `/opt/seasonalweather/app`.

Example:

```bash
git clone https://git.seasonalnet.org/Seasonal_Currency/SeasonalWeather /tmp/SeasonalWeather
cd /tmp/SeasonalWeather
sudo bash scripts/00-bootstrap.sh
```

Optional backend installs:

```bash
SEASONAL_DECTALK=1 sudo -E bash scripts/00-bootstrap.sh
SEASONAL_VOICETEXT_PAUL=1 sudo -E bash scripts/00-bootstrap.sh
```

The live runtime config is `/etc/seasonalweather/config.yaml`.
The repo copy at `/opt/seasonalweather/app/config/config.yaml` is only a template/example.

## VoiceText Paul display startup contract

`voicetext_paul_synth` defaults to `DISPLAY=:99` and expects the
`seasonalweather-voicetext-xvfb.service` unit to provide that local X socket.
When `SEASONAL_VOICETEXT_PAUL=1` is used during bootstrap, the installer now
adds a `seasonalweather.service.d/10-voicetext-paul.conf` drop-in so the main
service is ordered after the headless display.

If a deployment intentionally uses another display, set `VOICETEXT_PAUL_DISPLAY`
in `/etc/seasonalweather/seasonalweather.env`.  When the default local display
socket is missing, the wrapper fails fast with an actionable message instead of
letting Wine abort without context.

## VoiceText Paul Wine/runtime notes

Fresh VoiceText Paul installs provision i386 architecture and install the 32-bit Wine stack because VoiceText Paul is a 32-bit Windows runtime. The wrapper defaults fresh prefixes to `VOICETEXT_PAUL_WINEARCH=win32`; operators may explicitly set `VOICETEXT_PAUL_WINEARCH=auto` only as a known-good fallback for a specific Wine build.

Bootstrap also enables systemd lingering for the dedicated `voicetext` account with `loginctl enable-linger voicetext`. This keeps the user runtime stable for the Wine subprocesses instead of letting logind repeatedly tear down the account's runtime state between short-lived invocations.

The Xvfb unit is started with access control disabled for this private local display, and the wrapper probes the display with `xdpyinfo` when available. If Wine fails after those preflight checks, the wrapper prints the tail of the saved Wine log into journald and preserves the full log under `/var/lib/seasonalweather/tmp/`.

SeasonalWeather also serializes the Python-side VoiceText call path before invoking the wrapper. This avoids concurrent segment refresh jobs deleting or copying the shared `output.wav` while another synthesis is still returning from Wine.

## VoiceText Paul runtime source and smoke test

The VoiceText Paul Windows runtime is treated as a pinned deployment artifact,
not just a convenience download. Fresh installs should use a runtime source that
has been smoke-tested on a known-good SeasonalWeather host.

Bootstrap accepts these optional variables when `SEASONAL_VOICETEXT_PAUL=1`:

- `SEASONAL_VOICETEXT_PAUL_SOURCE`: URL, local archive, or local directory to install.
- `SEASONAL_VOICETEXT_PAUL_ZIP_URL`: legacy alias for a URL source.
- `SEASONAL_VOICETEXT_PAUL_SHA256`: optional SHA-256 checksum for archive sources.
- `SEASONAL_VOICETEXT_PAUL_REFRESH=1`: replace an existing runtime from the configured source.
- `SEASONAL_VOICETEXT_PAUL_SMOKE=0`: skip the post-install smoke test; not recommended.

When enabled, bootstrap starts `seasonalweather-voicetext-xvfb.service` and runs
a small VoiceText Paul synthesis before declaring the backend installed. If the
smoke test fails, bootstrap exits non-zero rather than leaving a service that
will immediately spam segment refresh failures. The smoke path uses multiple
wrapper attempts because VoiceText Paul/Wine failures have historically been
stateful: crash exits such as rc=134 or rc=139 are retried after `wineserver -k`
before bootstrap declares the backend unsafe.

The wrapper defaults fresh prefixes to `VOICETEXT_PAUL_WINEARCH=win32`, matching
the 32-bit VoiceText Paul executable and avoiding a 64-bit/WOW64 Wine prefix as
the primary runtime. Operators may still force `VOICETEXT_PAUL_WINEARCH=auto`
for a known-good fallback host, but that is no longer the default supported
fresh-install path.

The runtime directory is shared between two local users:

- `voicetext` runs the Wine wrapper and creates `input1.txt`, `output.wav`, and
  `.voicetext_paul.flock`.
- `seasonalweather` runs the Python service and must be able to remove or copy
  `output.wav` after synthesis.

Bootstrap therefore makes the runtime tree group-owned by `seasonalweather`,
adds `voicetext` to that group, sets setgid on runtime directories, and verifies
during the smoke test that `seasonalweather` can clean up the WAV produced by
`voicetext`.


## VoiceText Paul fresh-deployment behavior

Fresh VoiceText Paul deployments use a dedicated Wine prefix at
`/var/lib/seasonalweather/wineprefixes/voicetext_paul_voicetext`.
The wrapper defaults to a pure 32-bit Wine prefix. The VoiceText Paul executable
is 32-bit, so bootstrap installs the 32-bit Wine stack and does not install
`wine64` as part of the primary path.

Bootstrap initializes the prefix with `wineboot --init`, and then runs a small
synthesis smoke test before the backend is considered safe to enable. Operators
may set `VOICETEXT_PAUL_WINEARCH=auto` only for a known-good fallback host. When
the default `win32` mode finds an old non-win32 prefix, bootstrap recreates it by
default; set `SEASONAL_VOICETEXT_PAUL_RECREATE_NON_WIN32_PREFIX=0` to preserve it
for forensics.
