"""A shore power cable and the temperature it is rated for."""

from __future__ import annotations

from collections.abc import Callable


class Cable:
    def __init__(self, identifier: str, rated_temperature: float) -> None:
        if rated_temperature <= 0:
            raise ValueError("rated temperature must be positive")
        self.identifier = identifier
        self._rated_temperature = rated_temperature

    @property
    def rated_temperature(self) -> float:
        return self._rated_temperature

    def describe(self) -> dict[str, object]:
        return {"id": self.identifier, "rated_temperature": self._rated_temperature}


class ActiveCable(Cable):
    """Cable identity that follows the latest asset installed in a lifecycle."""

    def __init__(self, provider: Callable[[], Cable]) -> None:
        self._provider = provider

    @property
    def identifier(self) -> str:  # type: ignore[override]
        return self._provider().identifier

    @identifier.setter
    def identifier(self, value: str) -> None:
        raise AttributeError("active cable identifier is dynamic")

    @property
    def rated_temperature(self) -> float:
        return self._provider().rated_temperature

    def describe(self) -> dict[str, object]:
        return self._provider().describe()
