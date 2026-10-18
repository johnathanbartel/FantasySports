"""
Operator tool: records a team's platform fee (deposit) payment for one season in an AD_FREE league, in whatever database
DATABASE_URL points at. There is no admin role and no payment processor, so this is how a confirmed payment gets in. It only
writes the record (platform/platform_fees.py); it moves no money. Run it once the payment is really received.

    $env:DATABASE_URL = "sqlite:///./user_run.db"
    .venv-api/Scripts/python.exe scripts/record_platform_fee.py --league 1 --team 4 --season 2 `
        --amount-cents 1200 --reference "receipt-0042" --status paid --by "John"

Repeating a reference is safe (returns the existing record). A 'paid' record for a season that is already secured is refused,
and a record that backed an executed pick trade can't be moved to 'refunded'. Back up a real *.db file first.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_sports.platform import platform_fees
from fantasy_sports.platform.db.models.core import League, Team
from fantasy_sports.platform.db.session import DATABASE_URL, SessionLocal


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--league", type=int, required=True)
    ap.add_argument("--team", type=int, required=True)
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--amount-cents", type=int, required=True)
    ap.add_argument("--reference", required=True, help="the processor's or the receipt's reference; unique per league")
    ap.add_argument("--status", default="paid", choices=platform_fees.STATUSES)
    ap.add_argument("--by", required=True, help="the operator recording it")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        league = db.get(League, args.league)
        team = db.get(Team, args.team)
        if league is None or team is None:
            print("League or team not found.")
            return 1
        row = platform_fees.record_payment(db, league, team, args.season, args.amount_cents, args.status,
                                           args.reference, args.by)
        db.commit()
        print(f"Recorded on {DATABASE_URL}: record {row.id}, team {team.id} ({team.name}), season {row.season_number}, "
              f"{row.amount_cents} cents, {row.status}, secured={platform_fees.is_secured(db, league, team.id, row.season_number)}.")
        return 0
    except ValueError as e:
        db.rollback()
        print(f"Not recorded: {e}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
