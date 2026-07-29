from seasonalweather.diagnostics.registry import DiagnosticCatalogService


class WeatherService:
    async def status(self) -> dict[str, str]:
        return {"status": "ok"}


def diagnostics_service():
    return DiagnosticCatalogService
