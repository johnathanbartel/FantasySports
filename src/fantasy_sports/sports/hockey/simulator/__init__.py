"""Hockey adapter for the progressive game simulator (FS-026). See docs/hockey_simulator_scoring.md."""

from fantasy_sports.sports.hockey.simulator.adapter import HockeySimulatorAdapter
from fantasy_sports.sports.hockey.simulator.model import (
    CountingGame, CreditLine, HockeyGameInputs, InputError, RealGame, ShotAttempt, SideInput, TieOutError, WindowContext,
)

__all__ = [
    "HockeySimulatorAdapter", "CountingGame", "CreditLine", "HockeyGameInputs", "InputError", "RealGame", "ShotAttempt",
    "SideInput", "TieOutError", "WindowContext",
]
