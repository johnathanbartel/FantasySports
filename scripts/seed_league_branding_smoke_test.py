"""
League logo and custom URL through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file
(league_branding_smoke_test.db): slug rules, uniqueness, commissioner-only changes, the /l/<slug> redirect, logo upload
checks (type sniffing, size cap), serving, removal and the logo_url shown on the dashboard.

    .venv-api/Scripts/python.exe scripts/seed_league_branding_smoke_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

db_path = Path(os.environ.get("BRANDING_SMOKE_DB", "league_branding_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import User
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app, follow_redirects=False)
db = SessionLocal()
seed_hockey_reference_data(db)
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "other")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def make_league(name):
    resp = client.post("/leagues", json={"name": name, "commissioner_user_id": users["commish"].id,
                                         "universe_codes": ["nhl"], "size": 4}, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    return resp.json()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
one, two = make_league("Branding One"), make_league("Branding Two")
check(one["slug"] is None and one["logo_url"] is None, "a new league has no slug or logo")

# --- custom URL -------------------------------------------------------------------------------------------------------
def put_slug(lid, slug, who="commish"):
    return client.put(f"/leagues/{lid}/slug", json={"slug": slug}, headers=HDR[who])


resp = put_slug(one["id"], "  The-Pond  ")
check(resp.status_code == 200 and resp.json()["slug"] == "the-pond", "slug is trimmed and lowercased")
check(put_slug(two["id"], "THE-POND").status_code == 409, "a taken slug (any case) is refused")
for bad in ("ab", "x" * 41, "has space", "-lead", "trail-", "dou--ble", "12345", "admin", "under_score", "dot.dot"):
    check(put_slug(two["id"], bad).status_code == 400, f"slug {bad!r} is refused")
check(put_slug(one["id"], "other-name", who="other").status_code == 403, "only the commissioner sets the slug")
check(put_slug(one["id"], "the-pond").status_code == 200, "re-saving your own slug is fine")
resp = client.get("/l/The-Pond")
check(resp.status_code == 307 and resp.headers["location"] == f"/static/league.html?league_id={one['id']}", "the /l/ URL redirects")
check(client.get("/l/nope-nope").status_code == 404, "an unknown /l/ URL is a 404")
check(client.get("/leagues/by-slug/the-pond").json()["id"] == one["id"], "by-slug lookup works")
check(put_slug(one["id"], None).json()["slug"] is None and client.get("/l/the-pond").status_code == 404, "null clears the slug")
check(put_slug(two["id"], "the-pond").status_code == 200, "a cleared slug can be reused")

# --- logo ---------------------------------------------------------------------------------------------------------------
def put_logo(lid, data, who="commish", ctype="image/png"):
    return client.put(f"/leagues/{lid}/logo", content=data, headers={**HDR[who], "Content-Type": ctype})


check(client.get(f"/leagues/{one['id']}/logo").status_code == 404, "no logo yet is a 404")
check(put_logo(one["id"], PNG, who="other").status_code == 403, "only the commissioner uploads")
check(put_logo(one["id"], b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>", ctype="image/png").status_code == 400,
      "SVG / non-images are refused even if labelled as PNG")
check(put_logo(one["id"], PNG + b"\x00" * (512 * 1024)).status_code == 413, "over 512 KB is refused")
resp = put_logo(one["id"], PNG)
check(resp.status_code == 200 and resp.json()["logo_url"].startswith(f"/leagues/{one['id']}/logo?v="), "upload returns logo_url")
img = client.get(f"/leagues/{one['id']}/logo")
check(img.status_code == 200 and img.content == PNG and img.headers["content-type"] == "image/png", "logo is served as stored")
check(client.get(f"/leagues/{one['id']}").json()["logo_url"] == resp.json()["logo_url"], "GET league shows logo_url")
dash = client.get("/leagues/mine/dashboard", headers=HDR["commish"]).json()
check({d["league_id"]: d["logo_url"] for d in dash}[one["id"]] == resp.json()["logo_url"], "dashboard shows logo_url")
check(client.get(f"/leagues/{two['id']}/logo").status_code == 404, "another league's logo is separate")
check(client.delete(f"/leagues/{one['id']}/logo", headers=HDR["other"]).status_code == 403, "only the commissioner removes")
check(client.delete(f"/leagues/{one['id']}/logo", headers=HDR["commish"]).json()["logo_url"] is None, "logo removed")
check(client.get(f"/leagues/{one['id']}/logo").status_code == 404, "removed logo is a 404")

print(f"league branding smoke test passed ({checks} checks)")
