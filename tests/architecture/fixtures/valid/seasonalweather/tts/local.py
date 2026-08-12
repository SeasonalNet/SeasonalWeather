from seasonalweather.tts.models import Request


def accepted(request: Request) -> str:
    return request.text
