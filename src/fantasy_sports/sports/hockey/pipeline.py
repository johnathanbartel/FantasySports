"""NHL xG credit attribution (hockey README, "Credit attribution").

Pure functions over shot records - no database access - so the same code
scores a game for validation and for persistence.

v1 rules (MoneyPuck data carries no assist columns, so the assist share of a
goal's xG is folded into the shooter, i.e. every goal is "unassisted"):

- Goal: the shooter gets exactly 1.0 (xG chance share + 1 - xG finishing
  share). A non-goal shot earns the shooter nothing, so a team's model total
  equals the goals it scored in regulation and overtime by construction -
  never a flat bonus stacked on top.
- Goalie: Goals Saved Above Expected for every shot faced, xG - goal.
  Empty-net shots are excluded (the team's own decision, not a goalie
  failure). Summed over a game this tie-outs to
  xG faced - goals allowed (empty-net excluded).
- Shootout goals are not attributed (the shot data has none) - same kind of
  expected exclusion as the football pipeline's kickoffs and punts.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ShotRecord:
    shot_id: int
    shooting_team_id: int
    goalie_team_id: int
    shooter_asset_id: int | None
    goalie_asset_id: int | None
    pre_shot_xg: float
    is_goal: bool
    is_empty_net: bool


@dataclass(frozen=True)
class ShotCreditRow:
    shot_id: int
    asset_id: int
    role: str  # "shooter" / "goalie"
    points: float


@dataclass
class GameResult:
    goals_by_team: dict = field(default_factory=lambda: defaultdict(float))  # model total (shooter credit)
    xg_faced_by_team: dict = field(default_factory=lambda: defaultdict(float))  # non-empty-net xG against
    gsax_by_team: dict = field(default_factory=lambda: defaultdict(float))  # team goalie credit incl. unnamed goalies
    empty_net_goals_by_team: dict = field(default_factory=lambda: defaultdict(int))  # scored BY this team's opponent into its empty net
    credits: list = field(default_factory=list)  # list[ShotCreditRow] (named players only)
    shooter_credit_by_asset: dict = field(default_factory=lambda: defaultdict(float))
    gsax_by_asset: dict = field(default_factory=lambda: defaultdict(float))
    unnamed_shooter_points: float = 0.0
    unnamed_goalie_points: float = 0.0

    def model_total(self, team_id: int) -> float:
        return self.goals_by_team.get(team_id, 0.0)

    def model_goals_allowed(self, team_id: int) -> float:
        """Goals the team's goalies allowed, rebuilt from the credits alone:
        xG faced minus goalie GSAx (empty net excluded)."""
        return self.xg_faced_by_team.get(team_id, 0.0) - self.gsax_by_team.get(team_id, 0.0)


def process_shots(shots) -> GameResult:
    result = GameResult()
    for shot in shots:
        goal = 1.0 if shot.is_goal else 0.0
        if goal:
            result.goals_by_team[shot.shooting_team_id] += 1.0
            if shot.shooter_asset_id is None:
                result.unnamed_shooter_points += 1.0
            else:
                result.credits.append(ShotCreditRow(shot.shot_id, shot.shooter_asset_id, "shooter", 1.0))
                result.shooter_credit_by_asset[shot.shooter_asset_id] += 1.0
        else:
            result.goals_by_team.setdefault(shot.shooting_team_id, 0.0)

        if shot.is_empty_net:
            if goal:
                result.empty_net_goals_by_team[shot.goalie_team_id] += 1
            continue
        gsax = shot.pre_shot_xg - goal
        result.xg_faced_by_team[shot.goalie_team_id] += shot.pre_shot_xg
        result.gsax_by_team[shot.goalie_team_id] += gsax
        if shot.goalie_asset_id is None:
            result.unnamed_goalie_points += gsax
        else:
            result.credits.append(ShotCreditRow(shot.shot_id, shot.goalie_asset_id, "goalie", gsax))
            result.gsax_by_asset[shot.goalie_asset_id] += gsax
    return result
