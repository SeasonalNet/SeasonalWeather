# SeasonalWeather radio automation (Liquidsoap)
# - cycle plane: request.queue(id="cycle")
# - voice interrupt plane: request.queue(id="voice_alert")
# - full interrupt plane: request.queue(id="full_alert") (highest priority)
# - alert bus: FULL > VOICE, then routine cycle, with non-track-sensitive fallback
# - output mounts: Ogg/Vorbis and MP3 Icecast streams

container_mode = getenv("SEASONALWEATHER_CONTAINER") == "true"
if container_mode then
  set("log.stdout", true)
  set("log.file", false)
else
  set("log.stdout", false)
  set("log.file", true)
  set("log.file.path", "/var/log/seasonalweather/liquidsoap.log")
end

def env_or(name, fallback) =
  value = getenv(name)
  if value == "" then fallback else value end
end

telnet_bind_addr = env_or("SEASONALWEATHER_LIQUIDSOAP_TELNET_BIND_ADDR", "127.0.0.1")
icecast_host = env_or("SEASONALWEATHER_ICECAST_HOST", "127.0.0.1")
icecast_port = int_of_string(env_or("SEASONALWEATHER_ICECAST_PORT", "8000"))
icecast_password_file = getenv("SEASONALWEATHER_ICECAST_PASSWORD_FILE")
icecast_password =
  if icecast_password_file == "" then
    getenv("ICECAST_SOURCE_PASSWORD")
  else
    secret_reader = file.read(icecast_password_file)
    secret = string.trim(secret_reader())
    secret_reader.close()
    secret
  end

set("server.telnet", true)
set("server.telnet.bind_addr", telnet_bind_addr)
set("server.telnet.port", 1234)

# Internal request queues.  Liquidsoap 2.3.x may expose these built-in
# request.queue controls as anonymous request_queue* telnet commands instead of
# the requested ids below.  Do not depend on those generated names for runtime
# queue routing; explicit SeasonalWeather aliases are registered below.
cycle = request.queue(id="cycle")
voice_alert = request.queue(id="voice_alert")
full_alert = request.queue(id="full_alert")

# Explicit, stable command aliases for the Python orchestrator.  These avoid the
# request_queue/request_queue.N ambiguity seen on Liquidsoap 2.3.x builds.
def sw_cycle_push(uri) =
  cycle.push(request.create(uri))
  "OK"
end

def sw_voice_alert_push(uri) =
  voice_alert.push(request.create(uri))
  "OK"
end

def sw_full_alert_push(uri) =
  full_alert.push(request.create(uri))
  "OK"
end

# Metadata-bearing requests are transported as base64-encoded complete URIs.
# This keeps annotate: values, quotes, and spaces out of Liquidsoap's
# line-oriented server command parser while preserving per-request metadata.
def sw_cycle_push_b64(payload) =
  cycle.push(request.create(string.base64.decode(payload)))
  "OK"
end

def sw_voice_alert_push_b64(payload) =
  voice_alert.push(request.create(string.base64.decode(payload)))
  "OK"
end

def sw_full_alert_push_b64(payload) =
  full_alert.push(request.create(string.base64.decode(payload)))
  "OK"
end

def sw_cycle_skip(_) =
  source.skip(cycle)
  "OK"
end

# Reset only the routine cycle plane.  Emptying the queued requests is not
# enough because fallback(track_sensitive=false) leaves the interrupted current
# request paused.  Conversely, an unconditional skip on an empty request source
# can remain pending and kill the next request.  Only skip when Liquidsoap says
# a current cycle request actually exists, and explicitly destroy the detached
# queued requests after replacing the queue.
def sw_cycle_reset(_) =
  queued = cycle.queue()
  cycle.set_queue([])
  list.iter(request.destroy, queued)
  if cycle.current() != null() then
    cycle.skip()
  end
  "OK"
end

def sw_interrupt_status(_) =
  full_active = full_alert.current() != null() or full_alert.length() > 0
  voice_active = voice_alert.current() != null() or voice_alert.length() > 0
  if full_active or voice_active then
    "ACTIVE"
  else
    "IDLE"
  end
end

def sw_voice_alert_skip(_) =
  source.skip(voice_alert)
  "OK"
end

def sw_full_alert_skip(_) =
  source.skip(full_alert)
  "OK"
end

server.register(namespace="sw.cycle", usage="push <uri>", description="Push routine cycle audio.", "push", sw_cycle_push)
server.register(namespace="sw.voice_alert", usage="push <uri>", description="Push voice alert audio.", "push", sw_voice_alert_push)
server.register(namespace="sw.full_alert", usage="push <uri>", description="Push full alert audio.", "push", sw_full_alert_push)
server.register(namespace="sw.cycle", usage="push_b64 <base64-uri>", description="Push routine cycle audio with safely encoded request metadata.", "push_b64", sw_cycle_push_b64)
server.register(namespace="sw.voice_alert", usage="push_b64 <base64-uri>", description="Push voice alert audio with safely encoded request metadata.", "push_b64", sw_voice_alert_push_b64)
server.register(namespace="sw.full_alert", usage="push_b64 <base64-uri>", description="Push full alert audio with safely encoded request metadata.", "push_b64", sw_full_alert_push_b64)
server.register(namespace="sw.cycle", usage="skip", description="Skip current cycle audio.", "skip", sw_cycle_skip)
server.register(namespace="sw.cycle", usage="reset", description="Safely clear queued and current routine cycle audio.", "reset", sw_cycle_reset)
server.register(namespace="sw.voice_alert", usage="skip", description="Skip current voice alert audio.", "skip", sw_voice_alert_skip)
server.register(namespace="sw.full_alert", usage="skip", description="Skip current full alert audio.", "skip", sw_full_alert_skip)
server.register(namespace="sw.interrupt", usage="status", description="Report whether interrupt audio is current or queued.", "status", sw_interrupt_status)

# Always have something to output (silence) so the mount stays up.
silence = blank()

# Keep the runtime graph deliberately flat for Liquidsoap 2.3.x compatibility.
# Priority remains explicit: FULL beats VOICE, VOICE beats cycle, and silence
# keeps Icecast mounted if every request queue is momentarily empty.
radio = fallback(track_sensitive=false, [full_alert, voice_alert, cycle, silence])

# Primary low-bandwidth web stream.
output.icecast(
  %vorbis(samplerate=48000, channels=2, quality=0.5),
  host=icecast_host,
  port=icecast_port,
  password=icecast_password,
  mount="seasonalweather.ogg",
  name="SeasonalWeather Ogg",
  description="Unofficial automated IP weather broadcast (not NOAA Weather Radio)",
  genre="Weather",
  radio
)

# Compatibility stream for clients that do not reliably support Ogg/Vorbis.
output.icecast(
  %mp3(bitrate=128, samplerate=48000),
  host=icecast_host,
  port=icecast_port,
  password=icecast_password,
  mount="seasonalweather.mp3",
  name="SeasonalWeather MP3",
  description="Unofficial automated IP weather broadcast (not NOAA Weather Radio)",
  genre="Weather",
  radio
)
