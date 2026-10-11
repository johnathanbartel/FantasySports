"""Hockey adapter for the progressive game simulator (FS-026). See docs/hockey_simulator_scoring.md."""

from fantasy_sports.sports.hockey.simulator.adapter import HockeySimulatorAdapter
from fantasy_sports.sports.hockey.simulator.model import (
    CreditLine, HockeyGameInputs, InputError, ShotAttempt, SideInput, TieOutError,
)

__all__ = [
    "HockeySimulatorAdapter", "CreditLine", "HockeyGameInputs", "InputError", "ShotAttempt", "SideInput",
    "TieOutError",
]
