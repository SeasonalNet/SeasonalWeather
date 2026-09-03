# P3-03 local-only TTS Compose mode

P3-03 adds Compose service definitions for local synthesis without adding a
remote TTS service. The controller, standard routine worker, and Liquidsoap
remain the default P3-01 graph. Engine-specific workers are opt-in
profiles backed by their dedicated P2 worker images:

| Compose service | Profile | Image default | Worker profile |
| --- | --- | --- | --- |
| `routine-worker` | default | `seasonalweather-worker:standard` | `routine-worker` |
| `espeak-worker` | `espeak` | `seasonalweather-worker:espeak` | `espeak` |
| `piper-worker` | `piper` | `seasonalweather-worker:piper` | `piper` |
| `festival-worker` | `festival` | `seasonalweather-worker:festival` | `festival` |
| `dectalk-worker` | `dectalk` | `seasonalweather-worker:dectalk` | `dectalk` |
| `legacy-tts-worker` | `legacy-tts` | `seasonalweather-worker:legacy-tts` | `legacy-tts` |
| `voicetext-paul-worker` | `voicetext-paul` | `seasonalweather-worker:voicetext-paul` | `voicetext-paul` |
| `spfy-worker` | `spfy` | `seasonalweather-worker:spfy` | `spfy` |

Select the matching local engine in the mounted configuration and enable the
corresponding Compose profile when that engine requires an isolated worker
image:

```yaml
tts:
  backend: local
  fallback_backend: null
  local:
    engine: piper
```

```bash
docker compose --profile piper up -d
```

The classic local engines have independent workers for isolated trials:

```bash
docker compose --profile espeak up -d
docker compose --profile festival up -d
docker compose --profile dectalk up -d
```

VoiceText Paul and `spfy` use the same explicit worker boundary, and their
dedicated images carry the complete engine runtime. The VoiceText Paul image
installs the amd64 Wine/Wine32/Xvfb stack and embeds the checksum-pinned
WeatherRadioSuite-LIB archive. The `spfy` image installs the amd64 executable
and embeds its checksum-pinned voice manifest. Neither engine is installed in
the controller image or the routine worker image:

```bash
docker compose --profile voicetext-paul up -d
docker compose --profile spfy up -d
```

The controller image also removes the local engine implementation modules
(`seasonalweather/tts/local.py` and `seasonalweather/tts/voicetext_paul_vtml.py`)
from its installed package. The controller therefore retains only the
backend-neutral/remote client surface and cannot invoke a bundled local engine;
those implementation modules exist only in worker images.

The `spfy` engine invokes the pinned worker executable with the configured
voice and returns native WAV for the existing common finalization path. The
VoiceText Paul engine retains its existing VTML and Wine wrapper behavior; its
image entrypoint starts the bounded headless Xvfb display required by Wine.
Compose supplies a root-owned mode-1777 tmpfs at `/tmp/.X11-unix` beneath the
general temporary mount, and a private writable Wine home under `/tmp/voicetext`.
Xvfb and the worker still run as UID/GID 10001; no root startup or extra
capabilities are required. The entrypoint rejects an incorrectly owned X11
socket directory before starting the worker.
Neither profile receives controller state, job state, provider credentials, or
publication authority. The worker profiles remain fail-closed until P3-06
supplies the deployment-owned resolver that invokes these handlers.

The Piper image carries the neural runtime and common FFmpeg finalizer, while
the matching voice model is supplied through the read-only
`SEASONALWEATHER_PIPER_MODEL_DIR` mount. A Piper voice requires both
`<voice>.onnx` and `<voice>.onnx.json`; the worker reports the profile
unavailable when either file is absent. The model directory must therefore be
provisioned before enabling the profile.

The legacy profile is for deployments that deliberately provide the legacy
local runtime. Its dependencies remain worker-side and are never added to the
controller image. A missing executable or unconfigured handler keeps the
worker unqualified and unable to accept work; Compose does not silently
substitute another backend.

The Festival profile carries Debian Festival and the `kal_diphone` voice. The
DECtalk profile builds the checksum-pinned 2023-10-30 upstream source archive
inside the worker build and carries its `say` runtime under `/opt/dectalk`.
The espeak profile carries only `espeak-ng` and the common FFmpeg finalizer.

All local-TTS workers use the P3-02 volume contract: the artifact root is
read-only, only the shared worker staging directory is writable, and the
controller remains responsible for WAV validation, fencing, atomic promotion,
and final publication. Worker credentials are mounted as mode-0400 files and
are used only for the outbound SWWP connection.

Purpose priority, deadlines, cancellation, fallback eligibility, and replay
rules remain owned by the existing P1-06/P1-16 policy contracts. Alert
artifact jobs retain their strict deadline, safety-critical priority, exact
identity fences, and controller finalization. P3-03 does not add a scheduler,
remote provider integration, credential exchange, or production deployment.
