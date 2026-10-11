"""
Offline verification for the draft broadcast room and deterministic commentary (FS-031).

Checks the commentary provider (determinism, value/reach/fair/unknown, missing inputs named, no invented numbers,
swappable seam), the versioned announcement API (stable event ids, pagination/backfill, idempotent reads, absent
points), the page's pure logic under Node (duplicate events, muted default, backfill never spoken, reconnect backoff),
and the served page's structure. Throwaway SQLite; no network, no external model, no audio assets.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_draft_broadcast.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

NODE_CHECK = r"""
const fs = require('fs');
const B = require(process.argv[2]);
const d = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const ok = (c, m) => { if (!c) { console.error('FAIL ' + m); process.exit(1); } console.log('ok  ' + m); };
const A = d.announcements;
ok(A.every(a => B.versionSupported(a.version)) && !B.versionSupported('2.0') && !B.versionSupported(undefined), 'only contract major version 1 is accepted');
let m = B.merge([], A.slice(0, 4));
ok(m.list.length === 4 && m.fresh.length === 4 && m.lastPick === 4, 'first page merges in order');
m = B.merge(m.list, A.slice(2, 7));
ok(m.list.length === 7 && m.fresh.length === 3 && m.lastPick === 7, 'an overlapping reconnect page adds only the new announcements');
const again = B.merge(m.list, A.slice(2, 7));
ok(again.fresh.length === 0 && again.list.length === 7, 'the same page twice announces nothing new (duplicate events)');
ok(B.merge([], A.slice().reverse()).list.every((a, i) => a.pick_number === i + 1), 'out-of-order input is sorted by pick');
ok(B.merge([], [Object.assign({}, A[0], {version: '2.0'})]).list.length === 0, 'an unsupported version is ignored');
ok(B.merge([A[0]], [Object.assign({}, A[0], {commentary: {text: 'changed'}})]).list[0] === A[0], 'a known event id is never replaced');
const fresh = A.slice(0, 3);
ok(B.toSpeak(fresh, {soundOn: false, supported: true, backfill: false}).length === 0, 'muted (the default) speaks nothing');
ok(B.toSpeak(fresh, {soundOn: true, supported: false, backfill: false}).length === 0, 'no speech support speaks nothing');
ok(B.toSpeak(fresh, {soundOn: true, supported: true, backfill: true}).length === 0, 'a first load or reconnect backfill is shown but never spoken');
ok(B.toSpeak(fresh, {soundOn: true, supported: true, backfill: false}).length === 1 && B.toSpeak(fresh, {soundOn: true, supported: true, backfill: false})[0] === fresh[2], 'with sound on only the newest of several picks is spoken');
ok(B.toSpeak([], {soundOn: true, supported: true, backfill: false}).length === 0 && B.toSpeak(null, null).length === 0, 'no announcements, nothing to speak');
const a0 = A[0];
ok(B.headline(a0).startsWith('Pick 1 · ') && B.headline(a0).includes(a0.player.name), 'headline names the pick, team and player');
ok(B.announcementText(a0).includes(a0.commentary.text), 'screen-reader text carries the commentary');
const labels = A.map(a => B.toneLabel(a));
ok(labels.includes('No value call') && labels.includes('Value') && labels.includes('Reach'), 'unknown, value and reach picks are labelled: ' + [...new Set(labels)].join(', '));
const unk = A.find(a => a.commentary.value_label === 'unknown');
ok(/Not available for this pick: last-season points rank/.test(B.unavailableNote(unk)) && B.unavailableNote(A.find(a => a.commentary.value_label === 'value')) === '', 'missing inputs are shown, present inputs add no note');
ok(B.soundLabel(false, true).text === 'Sound off' && B.soundLabel(true, true).pressed === true && B.soundLabel(true, false).disabled === true, 'sound control states');
ok([0, 1, 2, 5, 9].map(B.pollDelayMs).join() === '5000,10000,20000,60000,60000', 'poll backoff: 5s, doubling, capped at 60s');
"""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def main() -> None:
    from fantasy_sports.platform.ai import commentary as cm

    # ---- commentary provider -------------------------------------------------------------------------------------
    p = cm.DeterministicCommentary()
    base = dict(pick_number=5, round=2, teams=4, team_name="Harbor", player_name="Pat Player", position="C",
                team_position_count=0, league_position_count=0, pool_rank=5, ranked_pool_size=40)
    ctx = cm.PickContext(**base)
    a, b = p.describe(ctx), p.describe(cm.PickContext(**base))
    check(a == b, "the same pick always reads the same (deterministic)")
    check(cm.value_label(20, 5) == "value" and cm.value_label(12, 2) == "value" and cm.value_label(11, 2) == "fair"
          and cm.value_label(2, 12) == "reach" and cm.value_label(2, 11) == "fair" and cm.value_label(7, None) == "unknown",
          "value / reach need a 10-slot gap; no rank gives unknown")
    check(a.value_label == "fair" and "First C off the board." in a.text and "fills a need" in a.text and not a.unavailable,
          "a fair first-at-position pick for a team with no C reads accordingly")
    check("rank" not in cm.DeterministicCommentary().describe(cm.PickContext(**{**base, "pool_rank": None})).text.lower().replace("no stored points to rank", ""),
          "without a rank the text makes no ranking claim")
    nr = p.describe(cm.PickContext(**{**base, "pool_rank": None}))
    check(nr.value_label == "unknown" and nr.unavailable == ("last-season points rank",) and "no stored points" in nr.text.lower()
          or "without stored points" in nr.text.lower(), "a missing rank is named as unavailable and the text says so")
    np_ = p.describe(cm.PickContext(**{**base, "position": None}))
    check("position" in np_.unavailable and "First" not in np_.text, "a missing position is named and no position claim is made")
    deep = p.describe(cm.PickContext(**{**base, "team_position_count": 2, "league_position_count": 4}))
    check("5th C taken so far" in deep.text and "already hold 2 at C" in deep.text and "depth" in deep.tags, "depth and league counts come from the context")
    check("automatic pick" in p.describe(cm.PickContext(**{**base, "is_autopick": True})).text, "an autopick says the clock ran out")
    # No invented numbers: every digit string in the text is one of the context numbers.
    for pr in (None, 1, 5, 40):
        for pos in (None, "G"):
            c = cm.PickContext(**{**base, "pool_rank": pr, "position": pos, "team_position_count": 2, "league_position_count": 3})
            allowed = {str(base["pick_number"]), str(pr), "2", "3", "4", "1"}
            nums = set(re.findall(r"\d+", p.describe(c).text))
            check(nums <= allowed, f"commentary only uses numbers from its inputs (rank={pr}, pos={pos}): {sorted(nums)}")

    class Fake:
        provider_id = "fake-test"
        def describe(self, ctx):
            return cm.Commentary(text="FAKE", tags=(), value_label="unknown")
    default = cm.get_provider()
    cm.set_provider(Fake())
    check(cm.get_provider().provider_id == "fake-test", "the provider seam can be swapped")
    cm.set_provider(default)
    src = (ROOT / "src" / "fantasy_sports" / "platform" / "ai" / "commentary.py").read_text(encoding="utf-8")
    check(not re.search(r"^\s*(import|from)\s+(requests|httpx|urllib|socket|openai|anthropic|aiohttp)", src, re.M), "the commentary module imports no network or model library")

    with tempfile.TemporaryDirectory(prefix="fs031-bc-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'bc').as_posix()}"

        from fastapi.testclient import TestClient

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            DraftableAsset, League, LeagueUniverse, Player, Team, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.sports.football.college.season_scoring import SCORING_SYSTEM
        from fantasy_sports.sports.football.models import PlayerSeasonPoints

        Base.metadata.create_all(bind=engine)
        client = TestClient(app)
        with SessionLocal() as db:
            users = [User(email=f"u{i}@example.com", display_name=f"U{i}") for i in range(4)]
            commissioner = User(email="comm@example.com", display_name="Comm")
            db.add_all(users + [commissioner])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            db.add(nhl)
            db.flush()
            assets = []
            for i in range(40):
                pl = Player(universe_id=nhl.id, full_name=f"Player {i:02d}", position=["C", "W", "D", "G"][i % 4])
                db.add(pl)
                db.flush()
                a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=pl.id)
                db.add(a)
                db.flush()
                assets.append(a.id)
                if i < 20:   # stored points for players 00-19: Player 00 ranks 1st
                    db.add(PlayerSeasonPoints(player_id=pl.id, season=2025, scoring_system=SCORING_SYSTEM, points=500.0 - 10 * i, breakdown={}))
            noproj = League(name="NoPoints", commissioner_user_id=commissioner.id, size=4, draft_rounds=1)
            lg = League(name="Broadcast", commissioner_user_id=commissioner.id, size=4, draft_rounds=3)
            db.add_all([noproj, lg])
            db.flush()
            for league in (noproj, lg):
                db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
                for i, u in enumerate(users):
                    db.add(Team(league_id=league.id, name=f"{league.name}-{i}", owner_user_id=u.id, is_ai_owned=False))
            db.flush()
            league_id = lg.id
            tok = [create_session(db, u.id).token for u in users]
            team_ids = [t.id for t in db.query(Team).filter_by(league_id=league_id).order_by(Team.id)]
            db.commit()
        hdr = [{"Authorization": f"Bearer {t}"} for t in tok]
        draft = client.post("/drafts/start", json={"league_id": league_id}).json()["id"]
        check(client.get(f"/drafts/{draft}/announcements").json()["announcements"] == [], "no picks yet, no announcements")
        check(client.get("/drafts/99999/announcements").status_code == 404 and client.get(f"/drafts/{draft}/announcements?after_pick=-1").status_code == 400,
              "unknown draft is 404; a negative cursor is 400")

        # Snake order over 4 teams: 0 1 2 3 | 3 2 1 0 | 0 1 2 3. Players 30-39 have no stored points; 00-19 do.
        order = [0, 1, 2, 3, 3, 2, 1, 0, 0, 1, 2, 3]
        plan = [30, 19, 31, 32, 33, 34, 35, 36, 37, 38, 0, None]   # last pick is an autopick
        for n, (t, who) in enumerate(zip(order, plan), start=1):
            if who is None:
                r = client.post(f"/drafts/{draft}/autopick")
            else:
                r = client.post(f"/drafts/{draft}/pick", json={"draftable_asset_id": assets[who]}, headers=hdr[t])
            assert r.status_code == 200, r.text
        full = client.get(f"/drafts/{draft}/announcements").json()
        an = full["announcements"]
        check(full["version"] == "1.0" and full["provider"] == "deterministic-v1" and full["picks_made"] == 12 and len(an) == 12
              and [x["pick_number"] for x in an] == list(range(1, 13)), "12 picks give 12 announcements in order, versioned, with the provider named")
        check(all(x["event_id"] == f"draft:{draft}:pick:{x['pick_number']}" and x["version"] == "1.0" for x in an), "event ids are stable per pick")
        check(client.get(f"/drafts/{draft}/announcements").json() == full, "reading twice returns the identical result (idempotent, no duplicates)")
        labels = [x["commentary"]["value_label"] for x in an]
        check(labels[0] == "unknown" and an[0]["commentary"]["unavailable"] == ["last-season points rank"],
              "a player with no stored points is 'unknown' and says which input was missing")
        check(labels[1] == "reach" and an[1]["commentary"]["inputs"]["pool_rank"] == 20, "Player 19 (rank 20) at pick 2 is a reach")
        check(labels[10] == "value" and an[10]["commentary"]["inputs"]["pool_rank"] == 1 and "bargain" in an[10]["commentary"]["text"].lower() or "value" in an[10]["commentary"]["text"].lower(),
              "Player 00 (rank 1) at pick 11 is a value")
        check(an[11]["is_autopick"] and "autopick" in an[11]["commentary"]["tags"] and an[11]["player"]["name"] == "Player 01",
              "the autopick is announced as such")
        check("first-at-position" in an[0]["commentary"]["tags"] and "fills-need" in an[0]["commentary"]["tags"], "the first pick is the first at its position and fills a need")
        check(an[0]["player"] == {"asset_id": assets[30], "name": "Player 30", "position": "D"} and an[0]["team"]["id"] == team_ids[0],
              "the announcement names the real player, position and team")
        check(an[0]["spoken_text"].startswith("Pick 1, round 1:") and an[0]["commentary"]["text"] in an[0]["spoken_text"], "spoken text is the headline plus the commentary")
        # Backfill / reconnect.
        tail = client.get(f"/drafts/{draft}/announcements?after_pick=9").json()
        check([x["pick_number"] for x in tail["announcements"]] == [10, 11, 12] and tail["next_pick"] == 12 and tail["after_pick"] == 9,
              "a reconnecting client asks after its last pick and gets exactly the rest")
        check(tail["announcements"] == an[9:], "the backfilled announcements equal the originals")
        empty = client.get(f"/drafts/{draft}/announcements?after_pick=12").json()
        check(empty["announcements"] == [] and empty["next_pick"] == 12, "when caught up there is nothing new and the cursor stays put")
        page = client.get(f"/drafts/{draft}/announcements?limit=5").json()
        check(len(page["announcements"]) == 5 and page["next_pick"] == 5, "limit pages the feed")

        # ---- page logic under Node ------------------------------------------------------------------------------------
        node = shutil.which("node")
        check(node is not None, "node is available")
        fx = Path(temp_dir) / "p.json"
        sc = Path(temp_dir) / "c.js"
        fx.write_text(json.dumps({"announcements": an}), encoding="utf-8")
        sc.write_text(NODE_CHECK, encoding="utf-8")
        proc = subprocess.run([node, str(sc), str(ROOT / "web" / "static" / "draft-broadcast-logic.js"), str(fx)], capture_output=True, text=True)
        print(proc.stdout, end="")
        check(proc.returncode == 0, "broadcast logic passes under Node" + (f": {proc.stderr.strip()}" if proc.returncode else ""))

        html = client.get("/static/draft-board.html").text
        for marker, why in [
            ('id="broadcast"', "broadcast panel"), ('id="bc-live" role="status" aria-live="polite"', "screen-reader live region"),
            ('id="bc-sound"', "sound toggle"), ('aria-pressed="false">Sound off', "sound off by default"), ('id="bc-replay"', "replay control"),
            ("let soundOn = false;", "muted unless the viewer turned it on"), ("typeof window.speechSynthesis", "speech is feature-detected"),
            ("catch (e) { /* speech is optional */ }", "speech failures are swallowed"), ("try { soundOn =", "stored preference read guarded"),
            ("announcements?after_pick=", "polls the announcement API from the last pick seen"), ("bcFailures", "reconnect state"),
            ("backfill", "backfill never spoken"), ("this draft's own picks", "states where commentary comes from"),
            ("draft-broadcast-logic.js", "logic module loaded"),
        ]:
            check(marker in html, f"page has {why}")
        check(not re.search(r"new Audio|<audio|<video|\.mp3|\.wav|spotify|webrtc|RTCPeerConnection|getUserMedia|https?://", html + (ROOT / "web/static/draft-broadcast-logic.js").read_text(encoding="utf-8"), re.I),
              "no audio assets, media, Spotify, chat or external URLs")
        inline = re.findall(r"<script>(.*?)</script>", html, re.S)
        (Path(temp_dir) / "i.js").write_text(inline[-1], encoding="utf-8")
        proc = subprocess.run([node, "--check", str(Path(temp_dir) / "i.js")], capture_output=True, text=True)
        check(proc.returncode == 0, "page script is syntactically valid" + (f": {proc.stderr.strip()}" if proc.returncode else ""))
        engine.dispose()

    print("\nAll draft broadcast checks passed.")


if __name__ == "__main__":
    main()
