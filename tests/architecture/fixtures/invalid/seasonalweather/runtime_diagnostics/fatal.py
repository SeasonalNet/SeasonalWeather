from seasonalweather.database import SeasonalDatabase


def unsafe_renderer():
    return SeasonalDatabase(path="runtime.sqlite3")
