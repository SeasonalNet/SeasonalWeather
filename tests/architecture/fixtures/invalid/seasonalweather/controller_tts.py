from seasonalweather.tts.tts import TTS


def render(text, output):
    engine = TTS(backend="local")
    engine.synth_to_wav(text, output)
