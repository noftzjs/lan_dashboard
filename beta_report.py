"""One-off: the post-beta stats report, built once from a copy of the beta
database and written out as a single standalone HTML file.

    .venv\\Scripts\\python.exe beta_report.py beta_lan.db beta_report.html

Frozen by construction: the page is generated from a fixed copy of the data
and never recomputed, so it stays a record of the beta rather than a live
view that drifts from whatever screenshot someone took.

Decisions baked in (operator, 2026-10-20):
  * The eight short/full name pairs from the client-build-70009 surname bug
    are folded into one character each (MERGE).
  * Characters are grouped by the person playing them, using the Players
    groupings in the database plus the merges above.
  * Test and one-session names are dropped (EXCLUDE). "Gfdgfdgdg
    Gfdgfdgdf" is a real player's real name and stays.
  * Times are shown in US Eastern, fixed at UTC-5.
  * Quest counts are what this dashboard saw turned in (distinct quest
    ids), not the game's lifetime figure, which includes play from before
    the beta for characters that already existed.
  * Gold is opt-in, so the gold award ranks only the people sharing it,
    and says so. AFK is addon-measured and only since install; same rule.
"""
import html
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

MERGE = {
    "Kaede": "Kaede Windjammer",
    "Norrin": "Norrin Dawnstone",
    "Port": "Port Vehx",
    "Ozzman": "Ozzman Goeth",
    "Cyklades": "Cyklades Usa",
    "Bank": "Bank Ofozzman",
    "Ytzw": "Ytzw Ww",
    "Ytzm": "Ytzm Zz",
}
EXCLUDE = {"Test", "Test Afasdfae", "Cyk Bufftester", "Thanatu Mournveil", "Volice Korvino", "Ytz Zz"}
TZ = timezone(timedelta(hours=-5), "EST")
MAX_LEVEL_REACHED = 30          # nobody passed this during the beta
TIMED_LEVEL_FLOOR = 20          # below this a quest chain can ding a level in minutes; not a fair "fastest"
NIGHT_OWL_MIN_EVENTS = 200

CLASS_COLORS = {
    "WARRIOR": "#C79C6E", "PALADIN": "#F58CBA", "HUNTER": "#ABD473", "ROGUE": "#FFF569",
    "PRIEST": "#FFFFFF", "SHAMAN": "#0070DE", "MAGE": "#69CCF0", "WARLOCK": "#9482C9",
    "DRUID": "#FF7D0A",
}

esc = html.escape


def canon(name):
    return MERGE.get(name, name)


def fmt_duration(seconds):
    if not seconds:
        return "—"
    s = int(seconds)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def fmt_gold(copper):
    g, s = copper // 10000, copper % 10000 // 100
    return f"{g:,}g {s}s" if g else f"{s}s"


def fmt_day(iso):
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%b %d").replace(" 0", " ")


def parse_ts(iso):
    """event_time rows are UTC with an offset; the older `timestamp` column is naive server time."""
    if not iso:
        return None
    t = datetime.fromisoformat(iso)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# --- Load -------------------------------------------------------------------

def load(db_path):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    q = lambda s, *a: [dict(r) for r in db.execute(s, a)]

    snaps = [r for r in q("SELECT * FROM player_snapshots") if r["player"] not in EXCLUDE]
    history = [r for r in q("SELECT player, log_type, level, quest_id, xp_reward, event_time, timestamp, zone, played_total"
                            " FROM xp_history ORDER BY id") if r["player"] not in EXCLUDE]
    people = {r["id"]: r for r in q("SELECT * FROM people")}
    links = {r["player"]: r["person_id"] for r in q("SELECT * FROM character_person")}
    return snaps, history, people, links


# --- Characters: one record per merged name -------------------------------

def build_characters(snaps):
    chars = {}
    for s in sorted(snaps, key=lambda r: r["last_updated"] or ""):
        name = canon(s["player"])
        c = chars.setdefault(name, {"name": name, "aliases": set(), "level": 0, "played": 0, "gold": None,
                                    "gold_private": False, "item_level": None, "afk": None,
                                    "professions": [], "class": None, "faction": None})
        c["aliases"].add(s["player"])
        c["level"] = max(c["level"], s["level"] or 0)
        c["played"] = max(c["played"], s["played_total"] or 0)     # /played only ever grows
        c["class"] = s["class"] or c["class"]
        c["faction"] = s["faction"] or c["faction"]
        private = json.loads(s["private_fields"] or "[]")
        # Later snapshots win for the point-in-time fields; sorted by last_updated above.
        if "gold" in private:
            c["gold_private"], c["gold"] = True, None
        elif s["gold"] is not None:
            c["gold_private"], c["gold"] = False, s["gold"]
        if s["item_level"] is not None:
            c["item_level"] = s["item_level"]
        if s["afk_total"] is not None:
            c["afk"] = max(c["afk"] or 0, s["afk_total"])
        if s["professions"]:
            c["professions"] = json.loads(s["professions"])
    return chars


def build_people(chars, people, links):
    """Person -> list of character names. Unlinked characters are their own person."""
    groups = defaultdict(list)
    person_name = {}
    for name, c in chars.items():
        pid = next((links[a] for a in c["aliases"] if a in links), None)
        if pid is None:
            key = f"solo:{name}"
            person_name[key] = name
        else:
            key = f"person:{pid}"
            person_name[key] = people[pid]["name"]
        if name not in groups[key]:
            groups[key].append(name)
    return {person_name[k]: v for k, v in groups.items()}


# --- History-derived figures ---------------------------------------------

def crunch(chars, history):
    quests = defaultdict(set)
    quest_xp = Counter()
    deaths = defaultdict(Counter)
    zones = defaultdict(set)
    first_at_level = defaultdict(dict)       # name -> level -> first event_time (aware)
    levels_seen = defaultdict(set)
    night = Counter()
    events = Counter()
    heat = Counter()                         # (weekday, hour) EST
    first_event = {}
    last_event = {}
    span = [None, None]                      # first/last row by server timestamp, timed or not
    played_at_level = defaultdict(dict)      # name -> level -> lowest /played seen at that level
    for r in history:
        name = canon(r["player"])
        if name not in chars:
            continue
        ts = parse_ts(r["timestamp"])
        span[0] = ts if span[0] is None else min(span[0], ts)
        span[1] = ts if span[1] is None else max(span[1], ts)
        if r["log_type"] == "STATUS" and r["played_total"] is not None and r["level"]:
            pl = played_at_level[name]
            pl[r["level"]] = min(pl.get(r["level"], r["played_total"]), r["played_total"])
        t = parse_ts(r["event_time"])
        if t is not None:
            local = t.astimezone(TZ)
            events[name] += 1
            heat[(local.weekday(), local.hour)] += 1
            if local.hour < 6:
                night[name] += 1
            first_event[name] = min(first_event.get(name, t), t)
            last_event[name] = max(last_event.get(name, t), t)
        kind = r["log_type"]
        if kind == "QUEST":
            if r["quest_id"] not in quests[name]:
                quests[name].add(r["quest_id"])
                quest_xp[name] += r["xp_reward"] or 0
        elif kind == "DEATH":
            deaths[name][r["zone"] or "unknown zone"] += 1
        elif kind == "ZONE":
            if r["zone"]:
                zones[name].add(r["zone"])
        elif kind == "XP":
            levels_seen[name].add(r["level"])
            if t is not None:
                lv = first_at_level[name]
                if r["level"] not in lv or t < lv[r["level"]]:
                    lv[r["level"]] = t
    # Time spent on each level: first sighting of L to first sighting of L+1,
    # only where both are timed and consecutive.
    level_times = defaultdict(dict)
    for name, lv in first_at_level.items():
        for level, t in lv.items():
            nxt = lv.get(level + 1)
            if nxt is not None and nxt > t:
                level_times[name][level] = (nxt - t).total_seconds()
    # Played time per level, the same measure as the analytics page: /played
    # at the first status report on L+1 minus the same at L. Breaks and sleep
    # don't count, which is what makes "longest grind" fair.
    level_cost = defaultdict(dict)
    for name, pl in played_at_level.items():
        for level, played in pl.items():
            nxt = pl.get(level + 1)
            if nxt is not None and nxt > played:
                level_cost[name][level] = nxt - played
    return dict(quests=quests, quest_xp=quest_xp, deaths=deaths, zones=zones, first_at_level=first_at_level,
                levels_seen=levels_seen, level_times=level_times, level_cost=level_cost, night=night, events=events,
                heat=heat, first_event=first_event, last_event=last_event, span=span)


# --- Awards ----------------------------------------------------------------

def owner_of(people):
    return {c: p for p, cs in people.items() for c in cs}


def awards(chars, people, h):
    who = owner_of(people)
    out = []

    def add(title, winner, figure, detail, note=None):
        out.append(dict(title=title, winner=winner, figure=figure, detail=detail, note=note))

    # First to the top: earliest first sighting of the max level, in-game clock.
    reached = [(h["first_at_level"][n][MAX_LEVEL_REACHED], n) for n in chars
               if MAX_LEVEL_REACHED in h["first_at_level"].get(n, {})]
    if reached:
        reached.sort()
        t, n = reached[0]
        runners = ", ".join(f"{esc(m)} ({fmt_day(tt.isoformat())})" for tt, m in reached[1:])
        add("First to 30", who[n], fmt_day(t.isoformat()), f"{esc(n)} got there first.",
            f"Then {runners}." if runners else None)

    # Most quests, per person, with the character that carried it.
    per_person = Counter({p: sum(len(h["quests"][c]) for c in cs) for p, cs in people.items()})
    if per_person:
        p, n = per_person.most_common(1)[0]
        top_char = max(people[p], key=lambda c: len(h["quests"][c]))
        add("Quest machine", p, f"{n:,} quests", f"{esc(top_char)} alone turned in {len(h['quests'][top_char]):,}.",
            "Quests this dashboard saw turned in, not the game's lifetime count.")

    per_person = Counter({p: sum(sum(h["deaths"][c].values()) for c in cs) for p, cs in people.items()})
    if per_person and per_person.most_common(1)[0][1]:
        p, n = per_person.most_common(1)[0]
        zone_count = Counter()
        for c in people[p]:
            zone_count.update(h["deaths"][c])
        z, zn = zone_count.most_common(1)[0]
        add("Most deaths", p, f"{n} deaths", f"{zn} of them in {esc(z)}.")

    per_person = Counter({p: sum(chars[c]["played"] for c in cs) for p, cs in people.items()})
    if per_person:
        p, n = per_person.most_common(1)[0]
        add("Most time played", p, fmt_duration(n), f"Across {len(people[p])} character{'s' if len(people[p]) > 1 else ''}, by the game's own /played.")

    per_char = {c: len(h["zones"][c]) for c in chars if h["zones"][c]}
    if per_char:
        c = max(per_char, key=per_char.get)
        add("Explorer", who[c], f"{per_char[c]} zones", f"{esc(c)} set foot in the most places.")

    costs = [(secs, lvl, c) for c, lc in h["level_cost"].items() for lvl, secs in lc.items()]
    fast = [x for x in costs if x[1] >= TIMED_LEVEL_FLOOR]
    if fast:
        secs, lvl, c = min(fast)
        add("Fastest level", who[c], fmt_duration(secs), f"{esc(c)}, level {lvl} to {lvl + 1}, in played time.",
            f"Levels {TIMED_LEVEL_FLOOR} and up only; the early ones take minutes for everyone.")
    if costs:
        secs, lvl, c = max(costs)
        add("Longest grind", who[c], fmt_duration(secs), f"{esc(c)} spent that long on level {lvl}.",
            "Played time on that level, by the game's /played, so breaks and sleep don't count.")

    owls = {c: h["night"][c] / h["events"][c] for c in chars if h["events"][c] >= NIGHT_OWL_MIN_EVENTS and h["night"][c]}
    if owls:
        c = max(owls, key=owls.get)
        add("Night owl", who[c], f"{owls[c]:.0%}", f"of {esc(c)}'s activity landed between midnight and 6 am.",
            "Eastern time.")

    sharing = {c: chars[c]["gold"] for c in chars if chars[c]["gold"] is not None and not chars[c]["gold_private"]}
    if sharing:
        per_person = Counter()
        for c, g in sharing.items():
            per_person[who[c]] += g
        p, g = per_person.most_common(1)[0]
        hidden = sum(1 for c in chars if chars[c]["gold_private"])
        add("Richest (among those sharing)", p, fmt_gold(g), f"Gold is opt-in, and {hidden} character{'s' if hidden != 1 else ''} kept it private.")

    afk = {c: chars[c]["afk"] for c in chars if chars[c]["afk"]}
    if afk:
        c = max(afk, key=afk.get)
        add("Most AFK", who[c], fmt_duration(afk[c]), f"{esc(c)}, standing around.",
            "Addon-measured, and only since it was installed.")

    geared = {c: chars[c]["item_level"] for c in chars if chars[c]["item_level"]}
    if geared:
        c = max(geared, key=geared.get)
        add("Best geared", who[c], f"ilvl {geared[c]:.1f}", f"{esc(c)}.")

    skills = [(p["rank"], p["name"], c) for c in chars for p in chars[c]["professions"]]
    if skills:
        rank, prof, c = max(skills)
        add("Master craftsman", who[c], f"{esc(prof)} {rank}", f"{esc(c)}'s highest skill.")
    return out


# --- Page -------------------------------------------------------------------

CSS = """
:root { --bg: #0e0e11; --card: #17171c; --card-2: #1d1d24; --border: #26262f; --text: #ececf2; --muted: #8d8d99;
        --accent: #f3a73c; --bar: #3987e5; --h0: #23232b; --h1: #305170; --h2: #36699e; --h3: #3c7fc9; --h4: #4491ec; }
* { box-sizing: border-box; }
body { margin: 0; padding: 20px; background: var(--bg); color: var(--text);
       font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 14px; }
.wrap { max-width: 1140px; margin: 0 auto; }
header { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; flex-wrap: wrap;
         border-bottom: 1px solid var(--border); padding-bottom: 14px; margin-bottom: 18px; }
h1 { font-size: 24px; margin: 0; color: var(--accent); }
header .sub { color: var(--muted); font-size: 13px; }
.panel { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; margin-bottom: 16px; }
.panel h2 { font-size: 15px; margin: 0 0 2px; }
.panel .answers { color: var(--muted); font-size: 13px; margin: 0 0 16px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.kpi { background: var(--card-2); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.kpi .label { font-size: 12px; color: var(--muted); }
.kpi .value { font-size: 28px; font-weight: 600; line-height: 1.15; margin-top: 2px; font-variant-numeric: tabular-nums; }
.kpi .sub { font-size: 12px; color: var(--muted); }
.awards { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }
.award { background: var(--card-2); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.award .title { font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
.award .winner { font-size: 20px; font-weight: 600; color: var(--accent); margin-top: 4px; }
.award .figure { font-size: 15px; font-variant-numeric: tabular-nums; margin-top: 2px; }
.award .detail { font-size: 12.5px; color: var(--muted); margin-top: 6px; }
.award .note { font-size: 11.5px; color: var(--muted); opacity: .8; margin-top: 6px; font-style: italic; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px 7px 0; border-bottom: 1px solid var(--border); vertical-align: middle; }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.leader td { color: var(--accent); }
.rk { width: 30px; color: var(--muted); font-variant-numeric: tabular-nums; }
.chip { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 7px; vertical-align: 0; }
.muted { color: var(--muted); }
.bars .row { display: grid; grid-template-columns: 170px 1fr 48px; align-items: center; gap: 10px; margin: 5px 0; font-size: 13px; }
.bars .track { height: 16px; }
.bars .fill { height: 100%; background: var(--bar); border-radius: 0 4px 4px 0; min-width: 2px; }
.bars .row.leader .fill { background: var(--accent); }
.bars .n { text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
.heat { display: grid; grid-template-columns: 36px repeat(24, 1fr); gap: 2px; font-size: 11px; color: var(--muted); }
.heat .cell { aspect-ratio: 1.4; border-radius: 2px; background: var(--h0); }
.heat .h1 { background: var(--h1); } .heat .h2 { background: var(--h2); } .heat .h3 { background: var(--h3); } .heat .h4 { background: var(--h4); }
.heat .lbl { align-self: center; }
.heat .hr { text-align: center; }
.legend { display: flex; gap: 14px; align-items: center; font-size: 12px; color: var(--muted); margin-top: 10px; }
.legend .sw { display: inline-block; width: 11px; height: 11px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.foot { color: var(--muted); font-size: 12px; line-height: 1.5; }
.foot li { margin-bottom: 4px; }
@media (max-width: 640px) { .bars .row { grid-template-columns: 110px 1fr 44px; } .heat { font-size: 9px; } }
"""


def class_chip(cls):
    return f'<span class="chip" style="background:{CLASS_COLORS.get(cls, "#7a7a85")}"></span>'


def render(chars, people, h, award_list, span):
    who = owner_of(people)
    names = sorted(chars, key=lambda c: (-chars[c]["level"], -len(h["quests"][c]), c))
    n_people = len(people)
    total_quests = sum(len(v) for v in h["quests"].values())
    total_deaths = sum(sum(v.values()) for v in h["deaths"].values())
    total_played = sum(c["played"] for c in chars.values())
    levels_gained = sum(max(h["levels_seen"][c]) - min(h["levels_seen"][c]) for c in chars if h["levels_seen"][c])
    total_events = sum(h["events"].values())

    kpis = [
        ("Players", f"{n_people}", f"{len(chars)} characters"),
        ("Levels gained", f"{levels_gained}", "across everyone"),
        ("Quests turned in", f"{total_quests:,}", "seen by the dashboard"),
        ("Deaths", f"{total_deaths}", "and counting"),
        ("Time played", fmt_duration(total_played), "all characters, by /played"),
        ("Events recorded", f"{total_events:,}", "with an in-game clock"),
    ]
    kpi_html = "".join(f'<div class="kpi"><div class="label">{l}</div><div class="value">{v}</div><div class="sub">{s}</div></div>'
                       for l, v, s in kpis)

    award_html = "".join(
        f'<div class="award"><div class="title">{a["title"]}</div><div class="winner">{esc(a["winner"])}</div>'
        f'<div class="figure">{a["figure"]}</div><div class="detail">{a["detail"]}</div>'
        + (f'<div class="note">{a["note"]}</div>' if a["note"] else "") + "</div>"
        for a in award_list)

    rows = []
    for i, c in enumerate(names, 1):
        ch = chars[c]
        ilvl = f"{ch['item_level']:.1f}" if ch["item_level"] else "—"
        rows.append(
            f'<tr{" class=leader" if i == 1 else ""}><td class="rk">{i}</td>'
            f'<td>{class_chip(ch["class"])}{esc(c)}</td><td class="muted">{esc(who[c]) if who[c] != c else ""}</td>'
            f'<td class="num">{ch["level"]}</td><td class="num">{len(h["quests"][c]):,}</td>'
            f'<td class="num">{sum(h["deaths"][c].values())}</td><td class="num">{fmt_duration(ch["played"])}</td>'
            f'<td class="num">{len(h["zones"][c]) or "—"}</td>'
            f'<td class="num">{ilvl}</td></tr>')
    standings = "".join(rows)

    prow = []
    people_sorted = sorted(people, key=lambda p: (-max(chars[c]["level"] for c in people[p]),
                                                  -sum(len(h["quests"][c]) for c in people[p])))
    for i, p in enumerate(people_sorted, 1):
        cs = people[p]
        best = max(cs, key=lambda c: chars[c]["level"])
        prow.append(
            f'<tr{" class=leader" if i == 1 else ""}><td class="rk">{i}</td><td>{esc(p)}</td>'
            f'<td class="muted">{", ".join(esc(c) for c in sorted(cs, key=lambda c: -chars[c]["level"]))}</td>'
            f'<td class="num">{chars[best]["level"]}</td><td class="num">{sum(len(h["quests"][c]) for c in cs):,}</td>'
            f'<td class="num">{sum(sum(h["deaths"][c].values()) for c in cs)}</td>'
            f'<td class="num">{fmt_duration(sum(chars[c]["played"] for c in cs))}</td></tr>')
    people_table = "".join(prow)

    zone_deaths = Counter()
    for c in chars:
        zone_deaths.update(h["deaths"][c])
    top_zones = zone_deaths.most_common(8)
    zmax = top_zones[0][1] if top_zones else 1
    zones_html = "".join(
        f'<div class="row{" leader" if i == 0 else ""}"><span>{esc(z)}</span>'
        f'<div class="track"><div class="fill" style="width:{100 * n / zmax:.1f}%"></div></div><span class="n">{n}</span></div>'
        for i, (z, n) in enumerate(top_zones))

    hmax = max(h["heat"].values()) if h["heat"] else 1
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    cells = ['<span></span>'] + [f'<span class="hr">{hr if hr % 3 == 0 else ""}</span>' for hr in range(24)]
    for d in range(7):
        cells.append(f'<span class="lbl">{days[d]}</span>')
        for hr in range(24):
            n = h["heat"].get((d, hr), 0)
            step = 0 if not n else min(4, 1 + int(3 * n / hmax))
            cells.append(f'<span class="cell h{step}" title="{days[d]} {hr:02d}:00 — {n} events"></span>')
    heat_html = "".join(cells)

    merged = ", ".join(f"{esc(a)} → {esc(b)}" for a, b in MERGE.items())
    dropped = ", ".join(esc(x) for x in sorted(EXCLUDE))

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>4L Beta Report</title><style>{CSS}</style></head>
<body><div class="wrap">
<header><h1>4L Beta Report</h1><span class="sub">{span} · {n_people} players · {len(chars)} characters</span></header>

<div class="panel"><h2>The beta in numbers</h2><p class="answers">Everything the dashboard recorded while the testers kicked the tyres.</p>
<div class="kpis">{kpi_html}</div></div>

<div class="panel"><h2>Awards</h2><p class="answers">Per player, with the character that earned it.</p>
<div class="awards">{award_html}</div></div>

<div class="panel"><h2>Final standings</h2><p class="answers">Every character, by level then quests.</p>
<table><thead><tr><th></th><th>Character</th><th>Player</th><th class="num">Level</th><th class="num">Quests</th>
<th class="num">Deaths</th><th class="num">Played</th><th class="num">Zones</th><th class="num">ilvl</th></tr></thead>
<tbody>{standings}</tbody></table></div>

<div class="panel"><h2>By player</h2><p class="answers">Characters folded into the person playing them.</p>
<table><thead><tr><th></th><th>Player</th><th>Characters</th><th class="num">Best level</th><th class="num">Quests</th>
<th class="num">Deaths</th><th class="num">Played</th></tr></thead><tbody>{people_table}</tbody></table></div>

<div class="panel"><h2>Where it went wrong</h2><p class="answers">Deaths by zone, across everyone.</p>
<div class="bars">{zones_html}</div></div>

<div class="panel"><h2>When everyone played</h2><p class="answers">Events per hour of the week, Eastern time. Brighter is busier.</p>
<div class="heat">{heat_html}</div>
<div class="legend"><span><span class="sw" style="background:var(--h0)"></span>quiet</span><span><span class="sw" style="background:var(--h1)"></span></span><span><span class="sw" style="background:var(--h2)"></span></span><span><span class="sw" style="background:var(--h3)"></span></span><span><span class="sw" style="background:var(--h4)"></span>busiest ({hmax} events)</span></div></div>

<div class="panel"><h2>How this was counted</h2><ul class="foot">
<li>Generated once from a copy of the beta database taken {datetime.now(TZ).strftime("%Y-%m-%d")}. It will not change.</li>
<li>A client patch on 23 Sep briefly dropped surnames, so eight characters logged under two names. They are counted as one: {merged}.</li>
<li>Test and one-session names were left out: {dropped}.</li>
<li>Quests are the ones this dashboard saw turned in. The game's own lifetime count includes play from before the beta.</li>
<li>Gold is shared by choice, so the gold award only ranks those sharing. AFK time is measured by the addon and only since it was installed.</li>
<li>Times are US Eastern. Level timings use the in-game clock on each event; the first 184 events of the beta predate that and are not timed.</li>
</ul></div>
</div></body></html>"""


def main(db_path, out_path):
    snaps, history, people_rows, links = load(db_path)
    chars = build_characters(snaps)
    people = build_people(chars, people_rows, links)
    h = crunch(chars, history)
    award_list = awards(chars, people, h)
    start = h["span"][0].astimezone(TZ)
    end = h["span"][1].astimezone(TZ)
    span = f"{start.strftime('%b %d').replace(' 0', ' ')} – {end.strftime('%b %d, %Y').replace(' 0', ' ')}"
    page = render(chars, people, h, award_list, span)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {out_path}: {len(people)} players, {len(chars)} characters, {len(award_list)} awards, {span}")
    for a in award_list:
        print(f"  {a['title']:32} {a['winner']:20} {a['figure']}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "beta_lan.db",
         sys.argv[2] if len(sys.argv) > 2 else "beta_report.html")
