"""
Verification for NHL 3-Window Roster UI, Best Matchup, NHL Team wording, and Scoreboard Tab.

Checks:
1. Removal of deprecated series schedule viewer files (nhl-schedule.html, nhl-schedule-logic.js, nhl-banking-logic.js).
2. Navigation in nhl-game.html and league.html points to league.html?tab=roster.
3. Roster page in league.html has the 3 weekly game window tabs:
   - Game 1 (Sun - Tue)
   - Game 2 (Wed - Thu)
   - Game 3 (Fri - Sat)
4. Multi-matchup player resolution elements:
   - "✦ 2 Matchups:" indicator with "Better Matchup ★"
   - "★ Best Matchup (Auto)" default option
   - Dropdown options to pick specific matchups
   - Forward-only surplus game flex options (Game 2 / Game 3)
   - "⇄ Flexed from Game" badge for flexed players
   - "Idle · No game this window" for inactive players in that window
5. NHL Team terminology in place of school in Fantasy Hockey.
6. Scoreboard tab registration and live simulator scoring links.
7. JavaScript syntax check on inline script in league.html under Node.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(condition: bool, message: str) -> None:
    print(("ok  " if condition else "FAIL ") + message)
    if not condition:
        sys.exit(1)


def main() -> None:
    static_dir = ROOT / "web" / "static"

    # 1. Obsolete schedule & lineup viewer files are removed
    for obsolete_file in ("nhl-schedule.html", "nhl-schedule-logic.js", "nhl-banking-logic.js"):
        check(not (static_dir / obsolete_file).exists(), f"{obsolete_file} is deleted")

    # 2. Navigation redirects to roster tab
    nhl_game_html = (static_dir / "nhl-game.html").read_text(encoding="utf-8")
    check("league.html?league_id=${state.league_id}&tab=roster" in nhl_game_html,
          "nhl-game.html nav link points to league.html?tab=roster")
    check("Roster & lines" in nhl_game_html, "nhl-game.html nav label reads 'Roster & lines'")

    league_html = (static_dir / "league.html").read_text(encoding="utf-8")
    check('data-go="roster">Roster &amp; lines (3-game series)' in league_html,
          "league.html quick link points to roster tab for NHL")
    check('data-go="roster" class="btn btn-secondary">View 3-Game Windows &amp; Lines on Roster' in league_html,
          "league.html pro schedule directs users to roster 3-game windows")

    # 3. 3-Game Window Tabs on Roster
    check('data-window="${w.num}"' in league_html and "Game ${w.num}" in league_html and "{ num: 1, label: 'Sun – Tue'" in league_html,
          "Game 1 (Sun - Tue) tab present in league.html")
    check('data-window="${w.num}"' in league_html and "Game ${w.num}" in league_html and "{ num: 2, label: 'Wed – Thu'" in league_html,
          "Game 2 (Wed - Thu) tab present in league.html")
    check('data-window="${w.num}"' in league_html and "Game ${w.num}" in league_html and "{ num: 3, label: 'Fri – Sat'" in league_html,
          "Game 3 (Fri - Sat) tab present in league.html")

    # 4. Multi-matchup resolution & flex elements
    check("✦ 2 Matchups:" in league_html, "multi-game matchup badge present")
    check("Better Matchup ★" in league_html, "Better Matchup indicator present")
    check("win-assign-select" in league_html, "multi-game assignment dropdown present")
    check("★ Best Matchup (Auto" in league_html, "Best Matchup automated default option present")
    check("⇄ Send" in league_html, "surplus game forward flex options present")
    check("⇄ Flexed from Game" in league_html, "flex destination badge present")
    check("Idle · No game this window" in league_html, "idle indicator present for players without games")
    check("btn-reset-flex" in league_html, "reset flex control present")

    # 5. NHL Team terminology instead of school in Fantasy Hockey
    check("isHockey() ? 'Search players or NHL teams...'" in league_html,
          "free agency search placeholder uses NHL teams for hockey")
    check("isHockey() ? 'Search players, NHL teams or fantasy teams...'" in league_html,
          "stats search placeholder uses NHL teams for hockey")
    check("['real_team', isHockey() ? 'NHL Team' : 'School']" in league_html,
          "stats table header uses NHL Team for hockey")

    # 6. Scoreboard tab & live scoring
    check("['scoreboard', 'Scoreboard']" in league_html, "scoreboard tab registered in ALL_TABS")
    check("renderScoreboard" in league_html, "renderScoreboard dispatcher present")
    check("renderHockeyScoreboard" in league_html, "renderHockeyScoreboard present")
    check("data-swindow=" in league_html, "scoreboard game window selectors present")
    check("/static/nhl-game.html?daily_game_id=" in league_html, "scoreboard links to live simulator viewer")
    check("scoreboardAutoRefreshTimer" in league_html, "live scoreboard auto-refresh timer present")
    check("'1st Period'" in league_html and "'2nd Period'" in league_html,
          "scoreboard shows the current period (the period clock replaced the 'in progress' labels in 9442cc4)")
    check("Highlights" in league_html, "highlights button present on scoreboard")
    check("Box score" in league_html, "box score button present on scoreboard")
    check("#boxscore" in league_html, "box score button targets #boxscore hash")
    check("btn-skip-score" in league_html, "skip to score button present")
    check("Owner hasn't watched highlights yet" in league_html, "reminder when owner hasn't watched highlights present")

    # 7. JS Syntax check under Node
    node = shutil.which("node")
    check(node is not None, "node is available")
    inline_scripts = re.findall(r"<script>([\s\S]*?)</script>", league_html)
    check(len(inline_scripts) > 0, "league.html contains inline script")
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(inline_scripts[0])
        temp_js = f.name
    try:
        proc = subprocess.run([node, "--check", temp_js], capture_output=True, text=True)
        check(proc.returncode == 0, f"league.html script parses without syntax error: {proc.stderr.strip()}")
    finally:
        Path(temp_js).unlink(missing_ok=True)

    print("\nAll NHL 3-Window Roster UI checks passed.")


if __name__ == "__main__":
    main()
