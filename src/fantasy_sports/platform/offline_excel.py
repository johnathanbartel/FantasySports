"""
Offline Draft Mode, phase 4: the Excel round-trip. Download a template built from the session, fill it in offline (dropdown
player and team lists), upload it, check the diff, and commit it as one transaction.

Template: sheets Instructions, Picks (the grid), Players and Teams (hidden, protected), _Meta (very hidden, protected). The
PlayerID column is an INDEX/MATCH formula on the exact DisplayName ("Last, First - TEAM POS"); INDEX/MATCH is used instead of
XLOOKUP because it works in every Excel version and in LibreOffice.

Upload: the file is untrusted. It is parsed with openpyxl in data_only mode (cached values only; a formula is never evaluated),
the player and team lists are rebuilt server-side from the session (the hidden sheets in the file are ignored), and the whole
upload is rejected with row-level errors if anything fails. No fuzzy matching: a PlayerID that resolves wins; otherwise the
Player text must equal a DisplayName exactly.

Functions flush, they do not commit.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill, Protection
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy.orm import Session

from fantasy_sports.platform import offline_draft as od
from fantasy_sports.platform.db.models.core import DraftableAsset, RealTeam, Team
from fantasy_sports.platform.db.models.offline_draft import OfflineDraftPick, OfflineDraftSession

TEMPLATE_VERSION = 1
SUPPORTED_VERSIONS = {1}
MAX_BYTES = 2 * 1024 * 1024
MAX_UNZIPPED = 40 * 1024 * 1024
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}
RED = PatternFill("solid", start_color="FFC7CE", end_color="FFC7CE")
INPUT = PatternFill("solid", start_color="FFF9DB", end_color="FFF9DB")


class ExcelError(od.OfflineDraftError):
    """A refused upload. .errors holds the row-level messages."""

    def __init__(self, errors: list[str]):
        super().__init__(errors[0] if errors else "Invalid upload")
        self.errors = errors


# ---- names -------------------------------------------------------------------------------------------------------------------
def _last_first(full: str) -> tuple[str, str]:
    parts = full.split()
    if len(parts) < 2:
        return full, ""
    if parts[-1].lower() in SUFFIXES and len(parts) > 2:
        return f"{parts[-2]} {parts[-1]}", " ".join(parts[:-2])
    return parts[-1], " ".join(parts[:-1])


def display_names(db: Session, s: OfflineDraftSession) -> dict[int, dict]:
    """asset_id -> {name, pos, team}: unique, sortable 'Last, First – TEAM POS' names for the session's pool."""
    from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
    assets = db.query(DraftableAsset).filter(DraftableAsset.id.in_(s.settings_json["pool"] or [0])).order_by(DraftableAsset.id).all()
    team_ids = {a.player.real_team_id for a in assets if a.player is not None and a.player.real_team_id}
    teams = {r.id: (r.external_ref or r.name) for r in db.query(RealTeam).filter(RealTeam.id.in_(team_ids or {0}))}
    out, used = {}, {}
    for a in assets:
        pos = draftable_asset_position(a) or ""
        team = teams.get(a.player.real_team_id, "") if a.player is not None else ""
        if a.player is not None:
            last, first = _last_first(a.player.full_name)
            base = (f"{last}, {first}" if first else last)
        else:
            base = draftable_asset_display_name(a)
        label = f"{base} – {' '.join(x for x in (team, pos) if x)}".strip(" –") if (team or pos) else base
        used[label] = used.get(label, 0) + 1
        if used[label] > 1:
            label = f"{label} #{used[label]}"
        out[a.id] = {"name": label, "pos": pos, "team": team}
    return out


def team_labels(db: Session, s: OfflineDraftSession) -> dict[int, str]:
    names = {t.id: t.name for t in db.query(Team).filter(Team.id.in_(s.settings_json["team_order"]))}
    seen, out = {}, {}
    for tid in s.settings_json["team_order"]:
        label = names[tid]
        seen[label] = seen.get(label, 0) + 1
        out[tid] = label if seen[label] == 1 else f"{label} ({tid})"
    return out


def settings_hash(s: OfflineDraftSession) -> str:
    keys = ("rounds", "team_order", "cell_owners", "budget", "min_bid", "pool")
    blob = json.dumps({k: s.settings_json.get(k) for k in keys}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---- template ----------------------------------------------------------------------------------------------------------------
def build_template(db: Session, s: OfflineDraftSession) -> bytes:
    names = display_names(db, s)
    labels = team_labels(db, s)
    n_teams = len(labels)
    rounds = s.settings_json["rounds"]
    picks = od.live_picks(db, s.id)
    wb = Workbook()

    ins = wb.active
    ins.title = "Instructions"
    lines = [
        ("Offline draft template", True),
        (f"League draft: {s.format} · {rounds} {'rounds' if s.format == 'snake' else 'roster spots'} · {n_teams} teams", False),
        ("", False),
        ("1. Fill the yellow cells on the Picks sheet. Choose Team and Player from the dropdowns - type to filter.", False),
        ("2. Leave rows you don't need empty. Don't edit grey cells; the sheet is protected (no password) to keep the layout intact.", False),
        ("3. The hidden PlayerID column looks the player up by exact name (INDEX/MATCH). If you paste a name, it must match the dropdown exactly.", False),
        ("4. Save as .xlsx and upload it in the commissioner console. The upload is checked row by row; nothing is imported if any row fails.", False),
        ("", False),
        ("Rules", True),
        (f"Each player can appear once. Each team holds at most {rounds} players.", False),
    ]
    if s.format == "auction":
        lines.append((f"Price is a whole number, at least {s.settings_json['min_bid']}. A team's prices can't total more than its budget (see the summary block).", False))
    else:
        lines.append(("Team is pre-filled from the draft order (including traded picks). Change it only to correct a trade. Keeper: type Y.", False))
    for i, (text, bold) in enumerate(lines, start=1):
        c = ins.cell(row=i, column=1, value=text)
        c.font = Font(bold=bold, size=14 if i == 1 else 11)
    ins.column_dimensions["A"].width = 120

    # Players / Teams (hidden, protected)
    pl = wb.create_sheet("Players")
    pl.append(["PlayerID", "DisplayName", "Pos", "Team"])
    ordered = sorted(names.items(), key=lambda kv: kv[1]["name"])
    for aid, d in ordered:
        pl.append([aid, d["name"], d["pos"], d["team"]])
    tm = wb.create_sheet("Teams")
    tm.append(["TeamName", "TeamID"])
    for tid, label in labels.items():
        tm.append([label, tid])
    last_p, last_t = max(2, len(ordered) + 1), max(2, n_teams + 1)
    wb.defined_names["PlayerNames"] = DefinedName("PlayerNames", attr_text=f"Players!$B$2:$B${last_p}")
    wb.defined_names["TeamNames"] = DefinedName("TeamNames", attr_text=f"Teams!$A$2:$A${last_t}")

    meta = wb.create_sheet("_Meta")
    for k, v in (("session_id", s.id), ("league_id", s.league_id), ("format", s.format), ("template_version", TEMPLATE_VERSION),
                 ("generated_at", od.utcnow().isoformat()), ("settings_hash", settings_hash(s))):
        meta.append([k, v])

    ws = wb.create_sheet("Picks", 1)
    by_cell = {(p.board_round, p.board_slot): p for p in picks if p.board_round}
    head_fill = PatternFill("solid", start_color="DDE3F5", end_color="DDE3F5")
    if s.format == "snake":
        header = ["Round", "Pick", "Overall", "Team", "Player", "PlayerID", "Keeper"]
        ws.append(header)
        for r in range(1, rounds + 1):
            for slot in range(1, n_teams + 1):
                p = by_cell.get((r, slot))
                owner = p.team_id if p else od.cell_owner(s, r, slot)
                ws.append([r, slot, od.overall(s, r, slot), labels[owner], names[p.draftable_asset_id]["name"] if p else None,
                           None, "Y" if p and p.is_keeper else None])
        last = ws.max_row
        cols = {"team": "D", "player": "E", "id": "F", "extra": "G"}
        editable = ["D", "E", "G"]
    else:
        header = ["Seq", "Team", "Player", "Price", "PlayerID"]
        ws.append(header)
        rows = rounds * n_teams
        sales = [p for p in picks if p.price is not None]
        for i in range(1, rows + 1):
            p = sales[i - 1] if i <= len(sales) else None
            ws.append([i, labels[p.team_id] if p else None, names[p.draftable_asset_id]["name"] if p else None,
                       p.price if p else None, None])
        last = ws.max_row
        cols = {"team": "B", "player": "C", "id": "E", "extra": "D"}
        editable = ["B", "C", "D"]
    for r in range(2, last + 1):
        ws[f"{cols['id']}{r}"] = f'=IFERROR(INDEX(Players!$A:$A,MATCH({cols["player"]}{r},Players!$B:$B,0)),"")'
    for c in range(1, len(header) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font, cell.fill = Font(bold=True), head_fill
        ws.column_dimensions[get_column_letter(c)].width = 36 if header[c - 1] in ("Team", "Player") else 11
    ws.freeze_panes = "A2"
    ws.column_dimensions[cols["id"]].hidden = True

    dv_team = DataValidation(type="list", formula1="=TeamNames", allow_blank=True, showErrorMessage=True,
                             errorTitle="Team", error="Choose a team from the list.")
    dv_player = DataValidation(type="list", formula1="=PlayerNames", allow_blank=True, showErrorMessage=True,
                               errorTitle="Player", error="Use the dropdown: pick a player from the list.")
    ws.add_data_validation(dv_team)
    ws.add_data_validation(dv_player)
    dv_team.add(f"{cols['team']}2:{cols['team']}{last}")
    dv_player.add(f"{cols['player']}2:{cols['player']}{last}")
    if s.format == "auction":
        dv_price = DataValidation(type="whole", operator="greaterThanOrEqual", formula1=str(s.settings_json["min_bid"]),
                                  allow_blank=True, showErrorMessage=True, errorTitle="Price",
                                  error=f"A whole number, at least {s.settings_json['min_bid']}.")
        ws.add_data_validation(dv_price)
        dv_price.add(f"D2:D{last}")
    pc = cols["player"]
    ws.conditional_formatting.add(f"{pc}2:{pc}{last}", FormulaRule(formula=[f'AND({pc}2<>"",COUNTIF(${pc}$2:${pc}${last},{pc}2)>1)'], fill=RED))
    for col in editable:
        for r in range(2, last + 1):
            ws[f"{col}{r}"].protection = Protection(locked=False)
            ws[f"{col}{r}"].fill = INPUT

    if s.format == "auction":
        ws["G1"], ws["H1"], ws["I1"], ws["J1"] = "Team", "Budget", "Spent", "Remaining"
        for c in "GHIJ":
            ws[f"{c}1"].font, ws[f"{c}1"].fill = Font(bold=True), head_fill
        for i, (tid, label) in enumerate(labels.items(), start=2):
            ws[f"G{i}"] = label
            ws[f"H{i}"] = od.team_budget(db, s, tid)
            ws[f"I{i}"] = f"=SUMIF($B$2:$B${last},G{i},$D$2:$D${last})"
            ws[f"J{i}"] = f"=H{i}-I{i}"
        ws.column_dimensions["G"].width = 28
        ws.conditional_formatting.add(f"J2:J{n_teams + 1}", CellIsRule(operator="lessThan", formula=["0"], fill=RED, font=Font(color="9C0006", bold=True)))

    for sheet in (ws, pl, tm, meta):
        sheet.protection.sheet = True
    pl.sheet_state = "hidden"
    tm.sheet_state = "hidden"
    meta.sheet_state = "veryHidden"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---- upload ------------------------------------------------------------------------------------------------------------------
@dataclass
class Entry:
    row: int
    team_id: int
    asset_id: int
    round: int | None = None
    slot: int | None = None
    price: int | None = None
    keeper: bool = False


@dataclass
class Parsed:
    entries: list[Entry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == int(v):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def _text(v) -> str | None:
    if v is None:
        return None
    t = str(v).strip()
    if not t or t.startswith("="):                    # a formula string means "no cached value": treat as blank, never evaluate
        return None
    return t


def parse_upload(db: Session, s: OfflineDraftSession, data: bytes) -> Parsed:
    """Validates an uploaded workbook against the session. Raises ExcelError with every row-level problem."""
    if len(data) > MAX_BYTES:
        raise ExcelError([f"The file is too large (limit {MAX_BYTES // (1024 * 1024)} MB)"])
    if not data.startswith(b"PK"):
        raise ExcelError(["Upload an .xlsx file (this is not an Excel workbook)"])
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
                raise ExcelError(["The workbook is too large when unpacked"])
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    except ExcelError:
        raise
    except Exception:
        raise ExcelError(["Could not read the file as an .xlsx workbook"])

    # 1. _Meta
    if "_Meta" not in wb.sheetnames or "Picks" not in wb.sheetnames:
        raise ExcelError(["This is not a draft template from this site (missing _Meta or Picks sheet)"])
    meta = {str(r[0].value): r[1].value for r in wb["_Meta"].iter_rows(min_row=1, max_col=2) if r[0].value is not None}
    if _int(meta.get("session_id")) != s.id:
        raise ExcelError(["This template was generated for a different draft"])
    if _int(meta.get("template_version")) not in SUPPORTED_VERSIONS:
        raise ExcelError(["This template version is not supported; download a fresh one"])
    if meta.get("format") != s.format:
        raise ExcelError([f"This template is for a {meta.get('format')} draft, but this draft is {s.format}"])
    warnings = []
    if meta.get("settings_hash") != settings_hash(s):
        warnings.append("Draft settings changed since this template was downloaded (rounds, order, budget or pool)")

    names = display_names(db, s)
    by_name = {d["name"]: aid for aid, d in names.items()}
    labels = team_labels(db, s)
    by_team = {v: k for k, v in labels.items()}
    ws = wb["Picks"]
    header = {str(c.value).strip().lower(): c.column for c in ws[1] if c.value is not None}
    need = ["round", "pick", "overall", "team", "player", "playerid", "keeper"] if s.format == "snake" else ["seq", "team", "player", "price", "playerid"]
    missing = [h for h in need if h not in header]
    if missing:
        raise ExcelError([f"The Picks sheet is missing column(s): {', '.join(missing)}"])
    errors: list[str] = []
    rounds, n = s.settings_json["rounds"], len(labels)
    entries: list[Entry] = []
    seen_asset: dict[int, int] = {}
    seen_cell: dict[tuple, int] = {}
    totals: dict[int, int] = {}
    counts: dict[int, int] = {}
    for rn in range(2, ws.max_row + 1):
        def val(h):
            return ws.cell(row=rn, column=header[h]).value
        player, team, pid, price, keeper = _text(val("player")), _text(val("team")), val("playerid"), val("price") if s.format == "auction" else None, _text(val("keeper")) if s.format == "snake" else None
        pid_i = _int(pid) if not (isinstance(pid, str) and pid.startswith("=")) else None
        if not any([player, pid_i, price not in (None, ""), keeper]):
            continue                                      # an empty slot (team alone, or a blank row)
        # 2. player
        asset = None
        if pid_i is not None and pid_i in names:
            asset = pid_i
            if player and by_name.get(player) not in (None, asset):
                errors.append(f'Row {rn}: "{player}" does not match its PlayerID. Use the dropdown.')
                continue
        elif player:
            asset = by_name.get(player)
        if asset is None:
            errors.append(f'Row {rn}: "{player or pid}" is not a valid player. Use the dropdown.' if (player or pid) else f"Row {rn}: no player chosen")
            continue
        # 3. team
        team_id = by_team.get(team) if team else None
        if team_id is None:
            errors.append(f'Row {rn}: "{team or ""}" is not a valid team. Use the dropdown.' if team else f"Row {rn}: no team chosen")
            continue
        e = Entry(row=rn, team_id=team_id, asset_id=asset)
        # 4. duplicates
        if asset in seen_asset:
            errors.append(f'Row {rn}: "{names[asset]["name"]}" is also on row {seen_asset[asset]}')
            continue
        seen_asset[asset] = rn
        if s.format == "snake":
            e.round, e.slot = _int(val("round")), _int(val("pick"))
            if e.round is None or e.slot is None or not (1 <= e.round <= rounds and 1 <= e.slot <= n):
                errors.append(f"Row {rn}: Round {val('round')} / Pick {val('pick')} is not on the board")
                continue
            ov = _int(val("overall"))
            if ov is not None and ov != od.overall(s, e.round, e.slot):
                errors.append(f"Row {rn}: Overall {ov} doesn't match Round {e.round}, Pick {e.slot}")
                continue
            if (e.round, e.slot) in seen_cell:
                errors.append(f"Row {rn}: Round {e.round}, Pick {e.slot} is also on row {seen_cell[(e.round, e.slot)]}")
                continue
            seen_cell[(e.round, e.slot)] = rn
            if keeper:
                if keeper.upper() != "Y":
                    errors.append(f'Row {rn}: Keeper must be Y or blank (found "{keeper}")')
                    continue
                e.keeper = True
        else:
            e.price = _int(price)
            if e.price is None or e.price < s.settings_json["min_bid"]:
                errors.append(f"Row {rn}: Price must be a whole number of at least {s.settings_json['min_bid']}")
                continue
            totals[team_id] = totals.get(team_id, 0) + e.price
        counts[team_id] = counts.get(team_id, 0) + 1
        entries.append(e)
    # 5. roster and budget limits
    for tid, c in counts.items():
        if c > rounds:
            errors.append(f"{labels[tid]} has {c} players; the limit is {rounds}")
    for tid, spent in totals.items():
        budget = od.team_budget(db, s, tid)
        if spent > budget:
            errors.append(f"{labels[tid]} spends {spent} but its budget is {budget}")
    if errors:
        raise ExcelError(sorted(errors, key=lambda m: int(m.split()[1].rstrip(":")) if m.startswith("Row ") else 10**9))
    return Parsed(entries=entries, warnings=warnings)


def plan(db: Session, s: OfflineDraftSession, parsed: Parsed, mode: str) -> dict:
    """What committing would do: picks to add, picks it would replace (replace_all), unchanged, skipped (fill_empty)."""
    if mode not in ("replace_all", "fill_empty"):
        raise od.OfflineDraftError("mode must be replace_all or fill_empty")
    existing = od.live_picks(db, s.id)
    key = lambda p: (p.board_round, p.board_slot) if s.format == "snake" else ("asset", p.draftable_asset_id)
    ekey = lambda e: (e.round, e.slot) if s.format == "snake" else ("asset", e.asset_id)
    ex_by_key = {key(p): p for p in existing}
    same = lambda p, e: p.draftable_asset_id == e.asset_id and p.team_id == e.team_id and (s.format == "auction" and p.price == e.price or s.format == "snake")
    unchanged, add, skipped, replace = [], [], [], []
    taken = {p.draftable_asset_id for p in existing}
    if mode == "replace_all":
        for e in parsed.entries:
            cur = ex_by_key.get(ekey(e))
            (unchanged if cur is not None and same(cur, e) else add).append(e)
        unchanged_keys = {ekey(e) for e in unchanged}
        replace = [p for p in existing if key(p) not in unchanged_keys]
    else:
        for e in parsed.entries:
            cur = ex_by_key.get(ekey(e))
            if cur is not None and same(cur, e):
                unchanged.append(e)
            elif cur is not None or e.asset_id in taken:
                skipped.append((e, "cell already filled" if cur is not None and s.format == "snake" else "player already on the board"))
            else:
                add.append(e)
        # combined roster / budget limits with what the board already holds
        errors = []
        rounds = s.settings_json["rounds"]
        labels = team_labels(db, s)
        for tid in {e.team_id for e in add}:
            have = [p for p in existing if p.team_id == tid]
            new = [e for e in add if e.team_id == tid]
            if len(have) + len(new) > rounds:
                errors.append(f"{labels[tid]} would have {len(have) + len(new)} players; the limit is {rounds}")
            if s.format == "auction":
                spent = sum(p.price or 0 for p in have) + sum(e.price for e in new)
                if spent > od.team_budget(db, s, tid):
                    errors.append(f"{labels[tid]} would spend {spent} against a budget of {od.team_budget(db, s, tid)}")
        if errors:
            raise ExcelError(errors)
    return {"mode": mode, "add": add, "unchanged": unchanged, "replace": replace, "skipped": skipped}


def describe(db: Session, s: OfflineDraftSession, parsed: Parsed, p: dict) -> dict:
    names = display_names(db, s)
    labels = team_labels(db, s)

    def row(e):
        return {"row": e.row, "team": labels[e.team_id], "player": names[e.asset_id]["name"],
                "where": f"R{e.round}.{e.slot}" if e.round else f"${e.price}"}
    return {"mode": p["mode"], "warnings": parsed.warnings, "rows": len(parsed.entries),
            "add": [row(e) for e in p["add"]], "unchanged": len(p["unchanged"]),
            "replace": [{"team": labels[x.team_id], "player": names.get(x.draftable_asset_id, {}).get("name", "?"),
                         "where": f"R{x.board_round}.{x.board_slot}" if x.board_round else f"${x.price}"} for x in p["replace"]],
            "skipped": [{**row(e), "why": why} for e, why in p["skipped"]]}


def commit(db: Session, s: OfflineDraftSession, parsed: Parsed, mode: str, user_id: int | None) -> dict:
    """One transaction: (replace_all) void what the upload replaces, then add the uploaded picks in row order (entry_seq follows
    row order, source 'upload'). The draft lands in review."""
    od._require_open(s)
    p = plan(db, s, parsed, mode)
    sp = db.begin_nested()
    try:
        for x in p["replace"]:
            od.void_pick(db, s, x.id, user_id, "replaced by upload")
        for e in p["add"]:
            od._record(db, s, e.team_id, e.asset_id, e.round, e.slot, e.price, "upload", user_id, None, e.keeper, False, f"user:{user_id}")
        sp.commit()
    except od.OfflineDraftError:
        sp.rollback()
        raise
    od.audit(db, s, f"user:{user_id}", "upload", None, {"mode": mode, "added": len(p["add"]), "voided": len(p["replace"]),
                                                       "unchanged": len(p["unchanged"]), "skipped": len(p["skipped"])})
    if s.status in ("setup", "live", "paused"):
        before = s.status
        s.status = "review"
        from fantasy_sports.platform import offline_live
        offline_live.on_status_change(db, s, before)
    od._bump(s)
    db.flush()
    return {"added": len(p["add"]), "voided": len(p["replace"]), "unchanged": len(p["unchanged"]), "skipped": len(p["skipped"])}
