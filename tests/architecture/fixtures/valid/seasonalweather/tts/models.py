from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    text: str
