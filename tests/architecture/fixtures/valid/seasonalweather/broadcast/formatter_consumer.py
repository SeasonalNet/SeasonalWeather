from seasonalweather.broadcast.formatters import FormatterSubsystem


def render(formatters: FormatterSubsystem, event) -> str:
    return formatters.ipaws_script(event)
