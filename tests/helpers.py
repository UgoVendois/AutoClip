from __future__ import annotations


class FakeClock:
    """Horloge contrôlée par le test."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def set(self, value: float) -> None:
        self.now = value
