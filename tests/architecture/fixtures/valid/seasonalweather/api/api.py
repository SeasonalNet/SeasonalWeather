from seasonalweather.control import OrchestratorControl


async def route(control: OrchestratorControl) -> dict[str, str]:
    return {"status": "ok"}
