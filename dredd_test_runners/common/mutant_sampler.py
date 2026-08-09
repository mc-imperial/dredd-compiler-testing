from abc import ABC, abstractmethod
import random


class MutantSampler(ABC):
    """Decides whether the current test should be run against a given mutant."""

    @abstractmethod
    def select(self, mutant: int) -> bool:
        """Return True if the pairing should be executed.

        Not a pure predicate: implementations may record the decision,
        so repeated calls for the same mutant can differ.
        """


class RunAllMutants(MutantSampler):
    def select(self, mutant: int) -> bool:
        return True


class HarmonicBackoffSampler(MutantSampler):
    """Runs a mutant with probability 1/(1 + times already run)."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._runs: dict[int, int] = {}
        self._rng = rng or random.Random()

    def select(self, mutant: int) -> bool:
        runs = self._runs.get(mutant, 0)
        if self._rng.random() * (1 + runs) >= 1.0:
            return False
        self._runs[mutant] = runs + 1
        return True
