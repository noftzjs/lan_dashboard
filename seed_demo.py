"""Seed a plausible LAN weekend into a throwaway database.

Charts designed against four hand-typed rows look fine and then fall apart on
real data. This generates the shapes a real event produces: characters who
start at different times, level at different rates, stall for a few hours,
die, go AFK, earn gold, and one who rolls an alt on day two.

Writes only to the scratchpad test DB. Nothing here touches the real local
database or production.
"""
import datetime
import math
import os
import random
import sys

import requests

BASE = os.environ.get("SEED_BASE", "http://127.0.0.1:5099")
random.seed(20261104)          # the launch date, so runs are reproducible

START = datetime.datetime(2026, 11, 4, 18, 0, 0)     # Friday evening
LAN_HOURS = 52                                        # through Sunday night

# name, class, faction, guild, pace (levels/hour at the start), first-login hour
ROSTER = [
    ("Ytzw Ww",            "WARRIOR", "Alliance", "Lan Gang",   0.95, 0.0),
    ("Cyk Alliance",       "PRIEST",  "Alliance", "Lan Gang",   0.80, 0.4),
    ("Kaede Windjammer",   "MAGE",    "Alliance", "Lan Gang",   1.10, 1.2),
    ("Port Vehx",          "WARLOCK", "Horde",    "Boosted",    0.72, 0.0),
    ("Thanatu Mournveil",  "HUNTER",  "Horde",    "Boosted",    0.88, 2.5),
    ("Sneakyboi Redux",    "ROGUE",   "Horde",    "Boosted",    0.60, 6.0),
    ("Bram Ironhollow",    "PALADIN", "Alliance", "Lan Gang",   0.98, 26.0),  # day-2 alt
]

ZONES_A = ["Elwynn Forest", "Westfall", "Loch Modan", "Redridge Mountains",
           "Duskwood", "Stormwind City", "Wetlands"]
ZONES_H = ["Durotar", "The Barrens", "Silverpine Forest", "Hillsbrad Foothills",
           "Thousand Needles", "Orgrimmar", "Stonetalon Mountains"]
PROFESSIONS = [
    ("Herbalism", 150), ("Mining", 150), ("Tailoring", 150), ("Skinning", 150),
    ("Blacksmithing", 150), ("Enchanting", 150),
]

LEVEL_CAP = 20
posted = rejected = 0
carry: dict[str, float] = {}   # fractional levels carried between steps


def post(when: datetime.datetime, payload: str):
    global posted, rejected
    r = requests.post(f"{BASE}/api/log-update",
                      json={"timestamp": when.isoformat(), "data": payload}, timeout=10)
    if r.json().get("status") == "error":
        rejected += 1
        if rejected < 6:
            print("  REJECTED:", r.json().get("detail"), "->", payload[:80])
    else:
        posted += 1


def stamp(when: datetime.datetime) -> int:
    return int(when.timestamp())


def xp_for(level: int) -> int:
    """Roughly Classic-shaped: each level costs more than the last."""
    return int(400 + (level ** 2.1) * 38)


for name, cls, faction, guild, pace, first_hour in ROSTER:
    zones = ZONES_A if faction == "Alliance" else ZONES_H
    t = START + datetime.timedelta(hours=first_hour)
    post(t, f"{name},PROFILE,{stamp(t)},{faction},{cls},{guild}")
    post(t, f"{name},ZONE,{stamp(t)},{zones[0]}")

    level, gold, played, afk, deaths = 1, random.randint(0, 900), 0, 0, 0
    # Two professions picked up an hour or two in, then trained over the weekend.
    my_profs = random.sample(PROFESSIONS, 2)
    prof_rank = {p[0]: 0 for p in my_profs}

    hour = first_hour
    while hour < LAN_HOURS and level < LEVEL_CAP:
        step = random.uniform(0.6, 1.4)
        hour += step
        t = START + datetime.timedelta(hours=hour)

        # Nobody plays 52 hours straight: a long sleep each night, and the
        # occasional couple of hours away. Those gaps are the whole reason a
        # time axis is more informative than a bar of totals.
        hour_of_day = (START + datetime.timedelta(hours=hour)).hour
        if 3 <= hour_of_day < 10:
            hour += random.uniform(4.0, 6.5)
            continue

        played += int(step * 3600)
        if random.random() < 0.22:
            idle = int(random.uniform(300, 2400))
            afk += idle

        # Levelling slows as the curve steepens.
        # Fractional levels carry over, so a sub-1.0 rate still advances rather
        # than truncating to zero every step -- which is what kept everyone
        # stuck in the low teens on the first run.
        rate = pace * (1.0 - 0.55 * (level / LEVEL_CAP)) * random.uniform(0.7, 1.3)
        carry[name] = carry.get(name, 0.0) + rate * step
        gained, carry[name] = int(carry[name]), carry[name] % 1.0
        for _ in range(gained):
            if level >= LEVEL_CAP:
                break
            level += 1
            post(t, f"{name},XP,{stamp(t)},{level},{xp_for(level)},{xp_for(level)}")

        if random.random() < 0.45:
            post(t, f"{name},ZONE,{stamp(t)},{random.choice(zones)}")
        if random.random() < 0.30:
            deaths += 1
            # DEATH is level then zone, the zone being the rest of the line.
            post(t, f"{name},DEATH,{stamp(t)},{level},{random.choice(zones)}")
        for _ in range(random.randint(0, 3)):
            post(t, f"{name},QUEST,{stamp(t)},{random.randint(2, 9999)},{random.randint(80, 2400)}")

        gold += random.randint(-1200, 5200)
        gold = max(0, gold)
        for prof_name, _cap in my_profs:
            prof_rank[prof_name] = min(150, prof_rank[prof_name] + random.randint(0, 6))

        current = int(xp_for(level) * random.uniform(0.05, 0.95))
        maxxp = 0 if level >= LEVEL_CAP else xp_for(level)
        current = 0 if maxxp == 0 else min(current, maxxp)
        ilvl = round(4 + level * 0.78 + random.uniform(-0.6, 0.6), 2)
        prof_fields = ",".join(f"{n}:{prof_rank[n]}:150" for n, _ in my_profs)
        # A couple of players keep their gold to themselves.
        gold_field = "" if name in ("Port Vehx", "Bram Ironhollow") else str(gold)
        post(t, f"{name},STATUS,{stamp(t)},{level},{current},{maxxp},{gold_field},"
                f"{played},{int(played * random.uniform(0.05, 0.25))},"
                f"{ilvl},{afk},{prof_fields}")

print(f"seeded: {posted} events accepted, {rejected} rejected")
if rejected:
    sys.exit(1)
