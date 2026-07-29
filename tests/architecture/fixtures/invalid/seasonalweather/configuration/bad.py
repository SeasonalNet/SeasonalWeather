import yaml

from seasonalweather.tts import tts


def duplicate_parser(value: str):
    return yaml.safe_load(value), tts
