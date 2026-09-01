"""FPL Jota Legends/Legacy League qualification tracker + knockout engine."""

import argparse
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from itertools import groupby

BASE = "https://fantasy.premierleague.com/api"
CLASSIC_LEAGUE_ID = 423590
H2H_LEAGUE_ID = 423607
BASE_KNOCKOUT_START_GW = 34  # gw values in bracket_map.json are authored against this
TOP_N = 20
LEAGUES = ("classic", "h2h")
START_GW_CHOICES = tuple(range(28, 39))

_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(_ROOT, "state")
BRACKET_MAP_PATH = os.path.join(_ROOT, "bracket_map.json")

CENTURION_START = "2027-01"  # display label for the Centurion contest start
CENTURION_START_GW = 19  # first century from GW19 (January phase) onwards, Classic league
_UA = {"User-Agent": "Mozilla/5.0"}

# FPL's official Manager of the Month phase GW ranges (not calendar month of deadline).
MOM_PHASES = [
    ("August", 1, 2),
    ("September", 3, 5),
    ("October", 6, 9),
    ("November", 10, 12),
    ("December", 13, 18),
    ("January", 19, 23),
    ("February", 24, 27),
    ("March", 28, 30),
    ("April", 31, 33),
    ("May", 34, 38),
]


def _phase_for_gw(gw):
    for name, start, end in MOM_PHASES:
        if start <= gw <= end:
            return name
    return None


def fetch_json(url):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path


def get_bootstrap():
    return fetch_json(f"{BASE}/bootstrap-static/")


def get_current_event():
    events = get_bootstrap()["events"]
    for event in events:
        if event["is_current"]:
            return event
    for event in reversed(events):
        if event["finished"]:
            return event
    return events[0]


def get_all_events():
    return get_bootstrap()["events"]


POLL_DEADLINE_BUFFER = timedelta(hours=1)


def should_poll():
    """False while the current GW's result is fully finalized and the next
    GW's deadline is still far off — nothing changes for us in that window,
    so the 5-min scheduled run can skip the heavy pipeline."""
    events = get_all_events()
    current = next((e for e in events if e["is_current"]), None)
    if current is None or not (current["finished"] and current.get("data_checked")):
        return True  # no current GW yet, or it's still live/unconfirmed

    nxt = next((e for e in events if e.get("is_next")), None)
    if nxt is None:
        return False  # season over, nothing left to poll for

    deadline = datetime.fromisoformat(nxt["deadline_time"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) >= deadline - POLL_DEADLINE_BUFFER


def _paginate_standings(league_kind, league_id, build_row, top_n=TOP_N):
    """Paginate league standings. top_n=None fetches the full league."""
    rows = []
    page = 1
    while top_n is None or len(rows) < top_n:
        data = fetch_json(
            f"{BASE}/leagues-{league_kind}/{league_id}/standings/?page_standings={page}"
        )
        results = data["standings"]["results"]
        if not results:
            break
        rows.extend(build_row(r) for r in results)
        if not data["standings"]["has_next"]:
            break
        page += 1
    return rows if top_n is None else rows[:top_n]


def fetch_classic_standings(league_id, top_n=TOP_N):
    return _paginate_standings(
        "classic",
        league_id,
        lambda r: {
            "rank": r["rank"],
            "entry_id": r["entry"],
            "team_name": r["entry_name"],
            "manager": r["player_name"],
            "gw_points": r["event_total"],
            "total_points": r["total"],
        },
        top_n,
    )


def fetch_h2h_standings(league_id, top_n=TOP_N):
    return _paginate_standings(
        "h2h",
        league_id,
        lambda r: {
            "rank": r["rank"],
            "entry_id": r["entry"],
            "team_name": r["entry_name"],
            "manager": r["player_name"],
            "total_points": r["total"],
            "matches_won": r["matches_won"],
            "matches_drawn": r["matches_drawn"],
            "matches_lost": r["matches_lost"],
            "points_for": r["points_for"],
        },
        top_n,
    )


def save_snapshot(gw, classic_rows, h2h_rows):
    path = os.path.join(STATE_DIR, f"standings_{gw}.json")
    return write_json(path, {"gw": gw, "classic": classic_rows, "h2h": h2h_rows})


def break_seed_ties(rows, gw):
    """Gapless rank order; ties broken by GW goals scored, then fewest conceded."""
    live_stats = None
    ordered = []
    for _, group_iter in groupby(sorted(rows, key=lambda r: r["rank"]), key=lambda r: r["rank"]):
        group = list(group_iter)
        if len(group) > 1:
            if live_stats is None:
                live_stats = get_live_stats(gw)

            def tie_key(row, _stats=live_stats):
                scored, conceded = squad_goals(get_entry_picks(row["entry_id"], gw), _stats)
                return (-scored, conceded, row["entry_id"])

            group.sort(key=tie_key)
        ordered.extend(group)
    return ordered


def write_seed_list(league_key, rows, gw):
    """Write a gapless T1..T20 seed list from the supplied league standings."""
    ordered_rows = break_seed_ties(rows, gw)
    seed_list = [
        {
            "seed": f"T{idx + 1}",
            "entry_id": row["entry_id"],
            "team_name": row["team_name"],
            "manager": row["manager"],
        }
        for idx, row in enumerate(ordered_rows)
    ]
    out_path = os.path.join(STATE_DIR, f"seed_list_{league_key}.json")
    write_json(out_path, seed_list)
    print(f"Wrote {out_path}")
    return seed_list


def lock_seed_lists(gw):
    path = os.path.join(STATE_DIR, f"standings_{gw}.json")
    if not os.path.exists(path):
        print(f"No snapshot found for GW{gw}. Run qualify mode for that GW first.")
        return {"status": "no_snapshot", "gw": gw}

    snapshot = read_json(path)
    written = []
    for league_key in LEAGUES:
        # FPL rank skips on ties (three at 6 -> next is 9). Re-derive gapless T1..T20.
        write_seed_list(league_key, snapshot[league_key], gw)
        written.append(league_key)
    return {"status": "ok", "gw": gw, "leagues": written}


def load_bracket_tail():
    """Fixed QF → SF → Final tree from bracket_map.json."""
    raw = read_json(BRACKET_MAP_PATH)
    return [m for key in ("gw36_qf", "gw37_sf", "gw38_final") for m in raw[key]]


def generate_bracket_draw():
    """Fixed GW34/GW35 seed pairings from the official bracket artwork."""
    gw34 = [
        {"match": 1, "gw": 34, "slots": ["T16", "T17"]},
        {"match": 2, "gw": 34, "slots": ["T13", "T20"]},
        {"match": 3, "gw": 34, "slots": ["T15", "T18"]},
        {"match": 4, "gw": 34, "slots": ["T14", "T19"]},
    ]
    gw35 = [
        {"match": 5, "gw": 35, "slots": ["T8", "T9"]},
        {"match": 6, "gw": 35, "slots": ["T5", "T12"]},
        {"match": 7, "gw": 35, "slots": ["T7", "T10"]},
        {"match": 8, "gw": 35, "slots": ["T6", "T11"]},
        {"match": 9, "gw": 35, "slots": ["T1", "winner:1"]},
        {"match": 10, "gw": 35, "slots": ["T4", "winner:2"]},
        {"match": 11, "gw": 35, "slots": ["T2", "winner:3"]},
        {"match": 12, "gw": 35, "slots": ["T3", "winner:4"]},
    ]
    return gw34, gw35


def bracket_draw_path(league_key):
    return os.path.join(STATE_DIR, f"bracket_draw_{league_key}.json")


def load_or_create_bracket_draw(league_key):
    path = bracket_draw_path(league_key)
    gw34, gw35 = generate_bracket_draw()
    expected = {"gw34_qualifier": gw34, "gw35_r16": gw35}
    if os.path.exists(path):
        saved = read_json(path)
        if saved == expected:
            return gw34 + gw35

    write_json(path, expected)
    print(f"Wrote {path} (fixed bracket seed map)")
    return gw34 + gw35


def load_seed_list(league_key, lock_gw):
    path = os.path.join(STATE_DIR, f"seed_list_{league_key}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} missing — run qualify mode through GW{lock_gw} first")
    return {row["seed"]: row for row in read_json(path)}


def bracket_state_path(league_key):
    return os.path.join(STATE_DIR, f"bracket_state_{league_key}.json")


def _empty_slot(seed, entry_id=None, team_name=None, manager=None):
    return {"seed": seed, "entry_id": entry_id, "team_name": team_name, "manager": manager}


def init_bracket(league_key, start_gw=BASE_KNOCKOUT_START_GW):
    lock_gw = start_gw - 1
    seed_map = load_seed_list(league_key, lock_gw)
    matches = load_or_create_bracket_draw(league_key) + load_bracket_tail()
    offset = start_gw - BASE_KNOCKOUT_START_GW
    state = {}
    for m in matches:
        slots = []
        for token in m["slots"]:
            if token.startswith("T"):
                seed_row = seed_map.get(token)
                slots.append(
                    _empty_slot(
                        token,
                        seed_row["entry_id"] if seed_row else None,
                        seed_row["team_name"] if seed_row else None,
                        seed_row.get("manager") if seed_row else None,
                    )
                )
            else:
                slots.append(_empty_slot(token))
        state[str(m["match"])] = {
            "gw": m["gw"] + offset,
            "slots": slots,
            "winner_entry_id": None,
            "tiebreak_reason": None,
            "tiebreak_detail": None,
            "provisional": None,
        }
    path = bracket_state_path(league_key)
    write_json(path, state)
    print(f"Wrote {path} (knockout starts GW{start_gw})")
    return state


def get_entry_picks(entry_id, gw):
    return fetch_json(f"{BASE}/entry/{entry_id}/event/{gw}/picks/")


def get_live_stats(gw):
    data = fetch_json(f"{BASE}/event/{gw}/live/")
    return {el["id"]: el["stats"] for el in data["elements"]}


def squad_goals(picks, live_stats):
    scored = conceded = 0
    for p in picks["picks"]:
        if p["multiplier"] > 0:
            stats = live_stats.get(p["element"], {})
            scored += stats.get("goals_scored", 0)
            conceded += stats.get("goals_conceded", 0)
    return scored, conceded


def resolve_match_winner(match_no, gw, entry_a, entry_b, live_stats=None, classic_ranks=None, gw_points_lookup=None):
    picks_a = get_entry_picks(entry_a, gw)
    picks_b = get_entry_picks(entry_b, gw)

    # picks' entry_history.points is accurate once a GW settles, but lags/stays
    # stale (often 0) while it's still live. gw_points_lookup — Classic league
    # standings' event_total, the same field the Current GW Rankings panel
    # uses — is genuinely real-time, so prefer it when resolving the live GW.
    gw_points_lookup = gw_points_lookup or {}
    score_a = gw_points_lookup.get(entry_a, picks_a["entry_history"]["points"])
    score_b = gw_points_lookup.get(entry_b, picks_b["entry_history"]["points"])

    if live_stats is None:
        live_stats = get_live_stats(gw)
    scored_a, conceded_a = squad_goals(picks_a, live_stats)
    scored_b, conceded_b = squad_goals(picks_b, live_stats)
    classic_ranks = classic_ranks or {}
    rank_a = classic_ranks.get(entry_a)
    rank_b = classic_ranks.get(entry_b)
    detail = {
        "entry_a": {
            "entry_id": entry_a,
            "gw_points": score_a,
            "goals_scored": scored_a,
            "goals_conceded": conceded_a,
            "classic_rank": rank_a,
        },
        "entry_b": {
            "entry_id": entry_b,
            "gw_points": score_b,
            "goals_scored": scored_b,
            "goals_conceded": conceded_b,
            "classic_rank": rank_b,
        },
    }

    if score_a != score_b:
        return (entry_a if score_a > score_b else entry_b), "gw_points", detail
    if scored_a != scored_b:
        return (entry_a if scored_a > scored_b else entry_b), "goals_scored", detail
    if conceded_a != conceded_b:
        return (entry_a if conceded_a < conceded_b else entry_b), "goals_conceded", detail
    if rank_a is None or rank_b is None:
        raise ValueError(
            f"Classic league rank missing for knockout match {match_no}: "
            f"entry {entry_a} rank={rank_a}, entry {entry_b} rank={rank_b}"
        )
    return (entry_a if (rank_a, entry_a) < (rank_b, entry_b) else entry_b), "classic_rank", detail


def classic_ranks_through_gw(target_gw, history_cache, live_points_lookup=None):
    """Classic league rank as of target_gw only (cumulative points through that
    GW), for the KO tiebreak — not whatever the live/current standing is now.
    history_cache maps entry_id -> that entry's get_entry_history()['current']
    list, fetched once per entry and reused across every GW resolved in a run.
    live_points_lookup (Classic standings' event_total) supplies target_gw's
    own points when target_gw is the live/in-progress GW, since its finalized
    figure isn't in history yet."""
    totals = []
    for eid, history in history_cache.items():
        past = sum(
            h["points"] for h in history
            if h.get("event") is not None and h["event"] < target_gw
        )
        if live_points_lookup and eid in live_points_lookup:
            this_gw = live_points_lookup[eid]
        else:
            this_gw = next((h["points"] for h in history if h.get("event") == target_gw), 0)
        totals.append((eid, past + this_gw))
    totals.sort(key=lambda x: -x[1])
    ranks = {}
    rank = 0
    last_total = None
    for eid, total in totals:
        if total != last_total:
            rank += 1
            last_total = total
        ranks[eid] = rank
    return ranks


def _winner_slot_index(state):
    index = {}
    for m in state.values():
        for slot in m["slots"]:
            seed = slot["seed"]
            if seed.startswith("winner:"):
                index.setdefault(seed, []).append(slot)
    return index


def propagate_winner(winner_slots, match_no, winner_entry_id, winner_team_name, winner_manager):
    for slot in winner_slots.get(f"winner:{match_no}", ()):
        slot["entry_id"] = winner_entry_id
        slot["team_name"] = winner_team_name
        slot["manager"] = winner_manager


def advance_bracket(league_key, live=False):
    """Resolve finished (or live-preview) matches. live=True marks in-progress GW provisional."""
    path = bracket_state_path(league_key)
    if not os.path.exists(path):
        print(f"No bracket state for {league_key}. Run init-bracket first.")
        return {"status": "missing_bracket_state", "league": league_key}

    state = read_json(path)
    events = get_all_events()
    events_by_id = {e["id"]: e for e in events}
    current_event = next((e for e in events if e["is_current"]), None)
    classic_gw_points = None
    classic_history_cache = None
    classic_ranks_by_gw = {}
    winner_slots = _winner_slot_index(state)
    live_stats_by_gw = {}

    for match_no in sorted(state, key=int):
        m = state[match_no]
        if m["winner_entry_id"] is not None and not m.get("provisional"):
            continue
        slot_a, slot_b = m["slots"]
        if slot_a["entry_id"] is None or slot_b["entry_id"] is None:
            continue

        match_event = events_by_id.get(m["gw"])
        if not match_event:
            continue
        is_final = match_event["finished"] and match_event.get("data_checked")
        is_live_now = live and current_event and match_event["id"] == current_event["id"]
        if not is_final and not is_live_now:
            continue

        gw = m["gw"]
        if gw not in live_stats_by_gw:
            live_stats_by_gw[gw] = get_live_stats(gw)
        if classic_history_cache is None:
            classic_rows = fetch_classic_standings(CLASSIC_LEAGUE_ID, top_n=None)
            classic_gw_points = {row["entry_id"]: row["gw_points"] for row in classic_rows}
            classic_history_cache = {
                row["entry_id"]: get_entry_history(row["entry_id"]).get("current", [])
                for row in classic_rows
            }
        if gw not in classic_ranks_by_gw:
            live_lookup = classic_gw_points if is_live_now else None
            classic_ranks_by_gw[gw] = classic_ranks_through_gw(gw, classic_history_cache, live_lookup)
        classic_ranks = classic_ranks_by_gw[gw]

        winner_id, reason, detail = resolve_match_winner(
            int(match_no),
            gw,
            slot_a["entry_id"],
            slot_b["entry_id"],
            live_stats_by_gw[gw],
            classic_ranks,
            # event_total only reflects the *current* GW, so it's only valid
            # for the live-in-progress match, not a historical finished one.
            classic_gw_points if is_live_now else None,
        )
        winner_name = slot_a["team_name"] if winner_id == slot_a["entry_id"] else slot_b["team_name"]
        winner_manager = slot_a.get("manager") if winner_id == slot_a["entry_id"] else slot_b.get("manager")
        m["provisional"] = not is_final
        m["winner_entry_id"] = winner_id
        m["tiebreak_reason"] = reason
        m["tiebreak_detail"] = detail
        propagate_winner(winner_slots, match_no, winner_id, winner_name, winner_manager)
        print(f"Match {match_no} (GW{gw}, {league_key}): winner={winner_name} ({reason})")

    write_json(path, state)
    return state


def current_season_label():
    """e.g. '26/27' — derived from the first event's deadline (PL seasons run
    roughly Aug-May, so a deadline before July belongs to the prior year's
    season start)."""
    events = get_all_events()
    first_deadline = events[0]["deadline_time"]
    year = int(first_deadline[:4])
    month = int(first_deadline[5:7])
    start_year = year if month >= 7 else year - 1
    return f"{start_year % 100:02d}/{(start_year + 1) % 100:02d}"


def append_season_champions():
    """Once the real season's GW38 Final is fully settled and both knockout
    brackets have a champion, auto-append this season's row to every title
    this app can compute confidently: Classic League (final standings),
    Jota Legends / Diogo Legacy (the KO bracket finals themselves), King of
    the Ring, and First Centurion. H2H League and Poky FPL Cup are left as
    '-' placeholders instead of a guess — H2H League has its own internal
    knockout process from GW35 onward that this app doesn't track (it's not
    simply final standings rank), and Poky FPL Cup has no data source in
    this app at all. Both need one manual edit to
    state/champions_history.json per season."""
    final_gw = BASE_KNOCKOUT_START_GW + 4  # 38
    events_by_id = {e["id"]: e for e in get_all_events()}
    final_event = events_by_id.get(final_gw)
    if not final_event or not (final_event["finished"] and final_event.get("data_checked")):
        return {"status": "skipped", "reason": "final_gw_not_settled"}

    def final_match(league_key):
        path = bracket_state_path(league_key)
        if not os.path.exists(path):
            return None
        return read_json(path).get("19")

    classic_final = final_match("classic")
    h2h_final = final_match("h2h")
    for league_key, m in (("classic", classic_final), ("h2h", h2h_final)):
        if not m or m.get("provisional") or not m.get("winner_entry_id"):
            return {"status": "skipped", "reason": f"{league_key}_final_not_resolved"}

    season = current_season_label()
    path = os.path.join(STATE_DIR, "champions_history.json")
    if not os.path.exists(path):
        return {"status": "skipped", "reason": "champions_history_missing"}
    history = read_json(path)
    by_title = {g["title"]: g for cluster in history for g in cluster["groups"]}
    if any(row[0] == season for row in by_title["Classic League"]["rows"]):
        return {"status": "skipped", "reason": "already_appended", "season": season}

    def slot_manager(match, entry_id):
        slot = next((s for s in match["slots"] if s["entry_id"] == entry_id), None)
        return slot["manager"] if slot else "-"

    def other_slot_manager(match, winner_id):
        slot = next((s for s in match["slots"] if s["entry_id"] != winner_id), None)
        return slot["manager"] if slot else "-"

    classic_top3 = fetch_classic_standings(CLASSIC_LEAGUE_ID, top_n=3)
    by_title["Classic League"]["rows"].insert(0, [
        season,
        classic_top3[0]["manager"] if len(classic_top3) > 0 else "-",
        classic_top3[1]["manager"] if len(classic_top3) > 1 else "-",
        classic_top3[2]["manager"] if len(classic_top3) > 2 else "-",
    ])
    by_title["H2H League"]["rows"].insert(0, [season, "-", "-", "-"])

    jota_winner = classic_final["winner_entry_id"]
    by_title["Jota Legends League"]["rows"].insert(0, [
        season, slot_manager(classic_final, jota_winner), other_slot_manager(classic_final, jota_winner),
    ])

    diogo_winner = h2h_final["winner_entry_id"]
    by_title["Diogo Legacy League"]["rows"].insert(0, [
        season, slot_manager(h2h_final, diogo_winner), other_slot_manager(h2h_final, diogo_winner),
    ])

    by_title["Poky FPL Cup"]["rows"].insert(0, [season, "-"])

    contests_path = os.path.join(STATE_DIR, "contests.json")
    contests = read_json(contests_path) if os.path.exists(contests_path) else {}
    king = contests.get("king_of_the_ring") or {}
    king_names = " & ".join(w["manager"] for w in king.get("winners", [])) or "-"
    by_title["King of the Ring"]["rows"].insert(0, [season, king_names])

    centurion = contests.get("centurion") or {}
    cent_names = "-"
    if centurion.get("achieved"):
        cent_names = " & ".join(w["manager"] for w in centurion.get("winners", [])) or "-"
    by_title["First Centurion of the Year"]["rows"].insert(0, [season, cent_names])

    write_json(path, history)
    print(f"Wrote {path} (appended season {season} — H2H League and Poky FPL Cup need manual entry)")
    return {"status": "ok", "season": season}


def build_dashboard():
    dashboard = {
        lk: read_json(p) if os.path.exists(p := bracket_state_path(lk)) else None
        for lk in LEAGUES
    }
    out_path = os.path.join(STATE_DIR, "dashboard.json")
    write_json(out_path, dashboard)
    print(f"Wrote {out_path}")
    return dashboard


def build_current_standings():
    """Full-league (all players) live standings for the current GW, both leagues."""
    event = get_current_event()
    payload = {
        "gw": event["id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classic": fetch_classic_standings(CLASSIC_LEAGUE_ID, top_n=None),
        "h2h": fetch_h2h_standings(H2H_LEAGUE_ID, top_n=None),
    }
    path = os.path.join(STATE_DIR, "standings_current.json")
    write_json(path, payload)
    print(f"Wrote {path}")
    return payload


def get_entry_history(entry_id):
    return fetch_json(f"{BASE}/entry/{entry_id}/history/")


def fetch_classic_league_members(league_id=CLASSIC_LEAGUE_ID):
    """All Classic league managers (Poky contests use Classic only)."""
    return fetch_classic_standings(league_id, top_n=None)


def _contest_person(row):
    return {
        "entry_id": row["entry_id"],
        "team_name": row["team_name"],
        "manager": row["manager"],
    }


def _break_score_ties(rows, gw, live_stats_by_gw):
    """MoW / Centurion: goals scored, then fewest conceded; leftover ties stay shared."""
    if len(rows) <= 1:
        return rows, False
    if gw not in live_stats_by_gw:
        live_stats_by_gw[gw] = get_live_stats(gw)
    live_stats = live_stats_by_gw[gw]
    keyed = []
    for r in rows:
        scored, conceded = squad_goals(get_entry_picks(r["entry_id"], gw), live_stats)
        keyed.append(((-scored, conceded), r))
    keyed.sort(key=lambda x: (x[0][0], x[0][1], x[1]["entry_id"]))
    best = keyed[0][0]
    winners = [r for k, r in keyed if k == best]
    return winners, len(winners) > 1


def compute_contests(league_id=CLASSIC_LEAGUE_ID):
    """Build MoW / MoM / King / Centurion from Classic league entry histories."""
    members = fetch_classic_league_members(league_id)
    events = get_all_events()
    events_by_id = {e["id"]: e for e in events}
    current_gw = next((e["id"] for e in events if e.get("is_current")), None)
    live_stats_by_gw = {}

    # entry_id -> {gw: points}  (FPL points already net of transfer hits)
    by_entry = {}
    meta = {}
    season_history = {}
    for row in members:
        eid = row["entry_id"]
        meta[eid] = _contest_person(row)
        history = get_entry_history(eid)
        by_entry[eid] = {
            h["event"]: h["points"]
            for h in history.get("current", [])
            if h.get("event") is not None
        }
        if current_gw is not None:
            # The per-entry history endpoint can lag the league-standings endpoint
            # (event_total) by a few minutes while a GW is live — use the same
            # fresher source the standings tab already reads from.
            by_entry[eid][current_gw] = row["gw_points"]
        season_history[eid] = sorted(
            (
                {
                    "season_name": h["season_name"],
                    "total_points": h["total_points"],
                    "rank": h.get("rank"),
                }
                for h in history.get("past", [])
            ),
            key=lambda s: s["season_name"],
            reverse=True,
        )

    scored_gws = sorted({gw for scores in by_entry.values() for gw in scores})
    # Include finished GWs + current (provisional) if anyone has a score
    active_gws = []
    for gw in scored_gws:
        ev = events_by_id.get(gw)
        if not ev:
            continue
        if ev["finished"] or ev.get("is_current"):
            active_gws.append(gw)

    # --- Manager of the Week ---
    mow = []
    for gw in active_gws:
        rows = [
            {**meta[eid], "points": scores[gw]}
            for eid, scores in by_entry.items()
            if gw in scores
        ]
        if not rows:
            continue
        best = max(r["points"] for r in rows)
        ev = events_by_id[gw]
        provisional = bool(ev.get("is_current") and not ev["finished"])
        if provisional and best <= 0:
            # GW just went live; matches haven't kicked off yet, no real scores —
            # keep the row (it'll fill in as live points come in on the next poll)
            # instead of showing every manager in the league as a "winner".
            winners, shared = [], False
        else:
            tied = [r for r in rows if r["points"] == best]
            winners, shared = _break_score_ties(tied, gw, live_stats_by_gw)
        mow.append({
            "gw": gw,
            "points": best,
            "shared": shared,
            "provisional": provisional,
            "winners": [{k: w[k] for k in ("entry_id", "team_name", "manager")} for w in winners],
        })

    # --- Manager of the Month (FPL's official phase GW ranges, not calendar month) ---
    month_scores = {}  # "August" -> {entry_id: total}
    month_gws = {}
    for gw in active_gws:
        ev = events_by_id[gw]
        phase = _phase_for_gw(gw)
        if phase is None:
            continue
        month_gws.setdefault(phase, []).append(gw)
        bucket = month_scores.setdefault(phase, {})
        for eid, scores in by_entry.items():
            if gw in scores:
                bucket[eid] = bucket.get(eid, 0) + scores[gw]

    mom = []
    for phase_name, _, _ in MOM_PHASES:
        bucket = month_scores.get(phase_name)
        if not bucket:
            continue
        best = max(bucket.values())
        winners = [
            {**meta[eid], "points": pts}
            for eid, pts in bucket.items()
            if pts == best
        ]
        mom.append({
            "month": phase_name,
            "label": phase_name,
            "points": best,
            "shared": len(winners) > 1,
            "provisional": any(
                events_by_id[gw].get("is_current") and not events_by_id[gw]["finished"]
                for gw in month_gws[phase_name]
            ),
            "gameweeks": sorted(month_gws[phase_name]),
            "winners": [{k: w[k] for k in ("entry_id", "team_name", "manager")} for w in winners],
        })

    # --- King of the Ring (highest single GW of the season) ---
    best_score = None
    king_rows = []
    for eid, scores in by_entry.items():
        for gw, pts in scores.items():
            if gw not in active_gws:
                continue
            if best_score is None or pts > best_score:
                best_score = pts
                king_rows = [{**meta[eid], "points": pts, "gw": gw}]
            elif pts == best_score:
                king_rows.append({**meta[eid], "points": pts, "gw": gw})
    king = {
        "points": best_score,
        "shared": len(king_rows) > 1,
        "provisional": any(
            events_by_id[r["gw"]].get("is_current") and not events_by_id[r["gw"]]["finished"]
            for r in king_rows
        ),
        "winners": [
            {k: r[k] for k in ("entry_id", "team_name", "manager", "gw", "points")}
            for r in king_rows
        ] if king_rows else [],
    }

    # --- First Centurion (from Jan 2027): first 100+ GW, Classic league ---
    centurion = {
        "achieved": False,
        "shared": False,
        "provisional": False,
        "winners": [],
        "best": None,
        "starts": CENTURION_START,
    }
    best_from_start = None
    for gw in active_gws:
        if gw < CENTURION_START_GW:
            continue
        rows = [
            {**meta[eid], "points": scores[gw], "gw": gw}
            for eid, scores in by_entry.items()
            if gw in scores
        ]
        for r in rows:
            if best_from_start is None or r["points"] > best_from_start["points"]:
                best_from_start = r
        centuries = [r for r in rows if r["points"] >= 100]
        if not centuries:
            continue
        best = max(r["points"] for r in centuries)
        tied = [r for r in centuries if r["points"] == best]
        winners, shared = _break_score_ties(tied, gw, live_stats_by_gw)
        centurion = {
            "achieved": True,
            "shared": shared,
            "provisional": bool(events_by_id[gw].get("is_current") and not events_by_id[gw]["finished"]),
            "gw": gw,
            "points": best,
            "starts": CENTURION_START,
            "winners": [
                {k: w[k] for k in ("entry_id", "team_name", "manager")}
                for w in winners
            ],
            "best": None,
        }
        break
    if not centurion["achieved"] and best_from_start:
        centurion["best"] = {
            k: best_from_start[k] for k in ("entry_id", "team_name", "manager", "gw", "points")
        }

    nxt = next((e for e in events if e.get("is_next")), None)
    current_event = events_by_id.get(current_gw) if current_gw is not None else None

    payload = {
        "league": "classic",
        "league_id": league_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "next_deadline": nxt["deadline_time"] if nxt else None,
        # Mirrors should_poll(): only once the current GW is finished AND
        # data-checked does "time until next deadline" mean anything — while
        # it's still live, updates are ongoing regardless of how far the next
        # deadline is.
        "current_gw_finalized": bool(current_event and current_event["finished"] and current_event.get("data_checked")),
        "updated_from_gws": active_gws,
        "manager_of_the_week": mow,
        "manager_of_the_month": mom,
        "king_of_the_ring": king,
        "centurion": centurion,
    }
    path = os.path.join(STATE_DIR, "contests.json")
    write_json(path, payload)
    print(f"Wrote {path} (Classic league contests)")

    history_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries": {
            str(eid): {**meta[eid], "seasons": season_history[eid]}
            for eid in meta
        },
    }
    history_path = os.path.join(STATE_DIR, "season_history.json")
    write_json(history_path, history_payload)
    print(f"Wrote {history_path} (per-manager past FPL seasons)")

    return payload


def force_qualify_snapshot(gw, lock=False):
    """Fetch live standings labeled as gw (bypasses finished gate) — for testing."""
    path = save_snapshot(
        gw,
        fetch_classic_standings(CLASSIC_LEAGUE_ID),
        fetch_h2h_standings(H2H_LEAGUE_ID),
    )
    result = {"status": "ok", "gw": gw, "snapshot_path": path}
    if lock:
        result["locked"] = lock_seed_lists(gw)
    return result


def reseed_live(league_key, start_gw=BASE_KNOCKOUT_START_GW):
    """Live-preview reseed of the qualification bracket while the last qualifying
    GW (start_gw - 1) is still in progress. No-ops until that GW is actually live."""
    lock_gw = start_gw - 1
    event = get_current_event()
    if event["id"] != lock_gw or not event.get("is_current") or event["finished"]:
        result = {
            "status": "skipped",
            "reason": "qualifying_gw_not_live",
            "league": league_key,
            "requested_gw": lock_gw,
            "current_gw": event["id"],
        }
        print(result)
        return result

    rows = (
        fetch_classic_standings(CLASSIC_LEAGUE_ID)
        if league_key == "classic"
        else fetch_h2h_standings(H2H_LEAGUE_ID)
    )
    seed_list = write_seed_list(league_key, rows, lock_gw)
    init_bracket(league_key, start_gw=start_gw)
    result = {
        "status": "ok",
        "gw": lock_gw,
        "league": league_key,
        "seeds": len(seed_list),
    }
    print(result)
    return result


def reseed_live_classic_test(source_gw=None, label_gw=BASE_KNOCKOUT_START_GW):
    """Classic-only smoke test: show the FULL bracket tree (T1-T20 direct seeds,
    TBD placeholders for R16 onward) exactly like the real thing, but only the
    Qualifier round's data source rotates live. `source_gw` is the real GW that
    round's data comes from (rotates with the calendar — defaults to whichever
    GW is currently live); `label_gw` is the fixed display anchor the frontend
    maps that round to (default GW34). Every other round keeps its real,
    not-yet-live GW number (35-38) so it stays correctly TBD/unresolved
    instead of fake-progressing on early-season data."""
    event = get_current_event()
    gw = source_gw if source_gw is not None else event["id"]
    if gw >= label_gw - 1:
        result = {"status": "skipped", "reason": "real_qualifying_window_reached", "source_gw": gw}
        print(result)
        return result
    if event["id"] != gw or not event.get("is_current") or event["finished"]:
        result = {
            "status": "skipped",
            "reason": "no_live_gw_to_test",
            "requested_gw": gw,
            "current_gw": event["id"],
        }
        print(result)
        return result

    rows = fetch_classic_standings(CLASSIC_LEAGUE_ID)
    seed_list = write_seed_list("classic", rows, gw)
    seed_map = {row["seed"]: row for row in seed_list}
    matches = load_or_create_bracket_draw("classic") + load_bracket_tail()
    state = {}
    for m in matches:
        slots = []
        for token in m["slots"]:
            if token.startswith("T"):
                seed_row = seed_map.get(token)
                slots.append(_empty_slot(
                    token,
                    seed_row["entry_id"] if seed_row else None,
                    seed_row["team_name"] if seed_row else None,
                    seed_row.get("manager") if seed_row else None,
                ))
            else:
                slots.append(_empty_slot(token))
        state[str(m["match"])] = {
            "gw": gw if m["gw"] == label_gw else m["gw"],
            "slots": slots,
            "winner_entry_id": None,
            "tiebreak_reason": None,
            "tiebreak_detail": None,
            "provisional": None,
        }
    path = bracket_state_path("classic")
    write_json(path, state)
    print(f"Wrote {path} (classic qualifier test: source GW{gw}, displayed as GW{label_gw})")
    return {"status": "ok", "source_gw": gw, "label_gw": label_gw, "league": "classic", "seeds": len(seed_list)}


def run_qualify(start_gw=BASE_KNOCKOUT_START_GW):
    lock_gw = start_gw - 1
    event = get_current_event()
    gw = event["id"]
    if not event["finished"] or not event.get("data_checked"):
        print(
            f"GW{gw} not finalized yet "
            f"(finished={event['finished']}, data_checked={event.get('data_checked')}). "
            "Skipping snapshot."
        )
        return

    h2h_rows = fetch_h2h_standings(H2H_LEAGUE_ID) if gw >= lock_gw else []
    path = save_snapshot(gw, fetch_classic_standings(CLASSIC_LEAGUE_ID), h2h_rows)
    print(f"Saved snapshot: {path}")
    if gw >= lock_gw:
        lock_seed_lists(lock_gw)


def league_targets(league):
    return LEAGUES if league == "both" else (league,)


def scheduled_targets(league, start_gw):
    """Like league_targets, but drops h2h entirely (no fetch attempted) until
    the real qualifying GW (start_gw - 1) is actually live."""
    targets = league_targets(league)
    if "h2h" in targets and get_current_event()["id"] < start_gw - 1:
        targets = tuple(lk for lk in targets if lk != "h2h")
    return targets


def main():
    parser = argparse.ArgumentParser(description="FPL league qualification tracker + knockout engine")
    parser.add_argument(
        "--mode",
        choices=["qualify", "live-reseed", "live-reseed-test", "draw", "init-bracket", "advance", "dashboard", "contests", "standings", "archive-season", "should-poll"],
        default="qualify",
    )
    parser.add_argument("--gw", type=int, help="Force a specific GW snapshot instead of current event")
    parser.add_argument(
        "--lock",
        action="store_true",
        help="With --gw: also lock seed lists from this snapshot",
    )
    parser.add_argument(
        "--league",
        choices=LEAGUES + ("both",),
        default="both",
        help="Target league for init-bracket/advance",
    )
    parser.add_argument(
        "--start-gw",
        type=int,
        choices=START_GW_CHOICES,
        default=BASE_KNOCKOUT_START_GW,
        help="Gameweek knockout begins; qualification locks the GW before this",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="With advance: also resolve the in-progress GW (provisional)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With draw: rewrite the fixed GW34/GW35 seed map",
    )
    args = parser.parse_args()
    targets = league_targets(args.league)

    if args.mode == "qualify":
        if args.gw:
            print(force_qualify_snapshot(args.gw, lock=args.lock))
        else:
            run_qualify(args.start_gw)
    elif args.mode == "live-reseed":
        for lk in scheduled_targets(args.league, args.start_gw):
            reseed_live(lk, args.start_gw)
    elif args.mode == "live-reseed-test":
        reseed_live_classic_test(args.gw, args.start_gw)
    elif args.mode == "draw":
        for lk in targets:
            if args.force:
                path = bracket_draw_path(lk)
                if os.path.exists(path):
                    os.remove(path)
            load_or_create_bracket_draw(lk)
    elif args.mode == "init-bracket":
        for lk in targets:
            init_bracket(lk, args.start_gw)
    elif args.mode == "advance":
        for lk in scheduled_targets(args.league, args.start_gw):
            advance_bracket(lk, live=args.live)
    elif args.mode == "dashboard":
        build_dashboard()
    elif args.mode == "contests":
        compute_contests()
    elif args.mode == "standings":
        build_current_standings()
    elif args.mode == "archive-season":
        print(append_season_champions())
    elif args.mode == "should-poll":
        print("true" if should_poll() else "false")


if __name__ == "__main__":
    main()
