local LanFrame = CreateFrame("Frame")

-- Register the updated set of event hooks
LanFrame:RegisterEvent("PLAYER_XP_UPDATE")
LanFrame:RegisterEvent("QUEST_TURNED_IN")
LanFrame:RegisterEvent("PLAYER_LEVEL_UP")
LanFrame:RegisterEvent("ZONE_CHANGED_NEW_AREA") -- Fires on dungeon transitions / loading screens
LanFrame:RegisterEvent("ZONE_CHANGED")          -- Fires when stepping over subzone thresholds
LanFrame:RegisterEvent("PLAYER_ENTERING_WORLD") -- Fires on login/ui reload/zone transitions
LanFrame:RegisterEvent("PLAYER_GUILD_UPDATE")   -- Fires when the player joins/leaves/is kicked from a guild
LanFrame:RegisterEvent("PLAYER_DEAD")           -- Fires on death — the LAN's favourite statistic
LanFrame:RegisterEvent("TIME_PLAYED_MSG")       -- The reply to RequestTimePlayed()
-- COMBAT_LOG_EVENT_UNFILTERED intentionally NOT registered: Blizzard has
-- removed addon access to it entirely in the Midnight beta (interface
-- 16001+) — RegisterEvent() on it is flatly forbidden and throws
-- ADDON_ACTION_FORBIDDEN, which blocked this addon's whole load, not just
-- kill scoring. Classic-flavor realms (MoP Classic, TBC Anniversary) still
-- support it fine, so this could come back behind a client-version check
-- if this addon ever needs to run on both. For now, kill-based scoring is
-- unavailable; quest and level-up weights carry the reload-trigger system.
LanFrame:RegisterEvent("PLAYER_CONTROL_LOST")   -- Fires when a flight path departs — a safe reload window
LanFrame:RegisterEvent("PLAYER_FLAGS_CHANGED")  -- Fires on AFK toggle — another safe reload window

-- Internal memory to check if data changed before spamming the chat log
local currentZone = ""

--============================================================
-- EVENT QUEUE (SavedVariables)
-- WoWChatLog.txt only flushes to disk on a full client exit — not /reload,
-- not logout to character select (confirmed empirically). SavedVariables
-- DOES flush on /reload, so the real event data lives here now. The watcher
-- reads this table, not the chat log, for PROFILE/ZONE/XP/QUEST data — so
-- nothing here needs to reach the chat frame at all (see LDB_VERBOSE).
--
-- Never cleared by the addon itself — it has no way to know what a watcher
-- has already read. nextId only ever grows; a watcher tracks the highest
-- index it's already forwarded and only sends what's new past that.
--============================================================
LanDashboardDB = LanDashboardDB or { events = {}, nextId = 1 }

-- Characters this addon has already queued a PROFILE for (name -> true). Lets
-- the login sync prompt fire only the first time a character is ever seen
-- rather than on every login: anything already queued gets written out by the
-- normal logout save at the latest, so a repeat prompt adds nothing. Also
-- added to older saved tables that predate it. Clear with  /ldb forget
-- (e.g. after pointing the server at a fresh DB_FILE, when everyone needs to
-- register again).
LanDashboardDB.known = LanDashboardDB.known or {}

-- Debug echo, off by default: every queued event printed to chat. This used
-- to be how the data actually left the game (the watcher read the chat log),
-- so it had to print — but SavedVariables carries the data now and the echo
-- is just noise: PLAYER_XP_UPDATE alone fires on every mob kill and quest
-- reward, which floods the chat frame during normal play. Turn it back on
-- with /ldb verbose when diagnosing what the addon is or isn't recording.
LDB_VERBOSE = LDB_VERBOSE or false

-- The realm's clock, not the player's PC clock: every player's events then sit
-- on one shared timeline even if their machines disagree, which is what makes
-- cross-player charts ("who levelled faster") meaningful. time() is a local
-- fallback in case a client ever lacks GetServerTime.
local function eventTime()
    if GetServerTime then
        return GetServerTime()
    end
    return time()
end

-- Events carry the moment they HAPPENED, stamped here as they're queued.
-- Without this the only timestamp is the one the watcher adds when it POSTs a
-- batch, which is delivery time, not play time: a whole evening of events
-- collapses onto the handful of instants the player happened to sync at, and
-- every time-based chart becomes meaningless. The stamp goes immediately after
-- the log type, where the field is always a bare number — the trailing fields
-- (zone, guild) are free text that can contain commas, so appending there
-- would be ambiguous.
local function emit(playerName, logType, payload)
    local line = string.format("%s,%s,%d,%s", playerName, logType, eventTime(), payload)
    if LDB_VERBOSE then
        print("[DASHBOARD] " .. line)
    end
    LanDashboardDB.events[LanDashboardDB.nextId] = line
    LanDashboardDB.nextId = LanDashboardDB.nextId + 1
end

--============================================================
-- RELOAD TRIGGER TUNING
-- Persisted via SavedVariables (see the .toc), specifically so tuning
-- survives the very reloads this system triggers — the "or" fallback
-- below only applies a default the first time, never overwriting a value
-- already saved from a previous session.
-- Change live with e.g.  /run LDB_WEIGHT_QUEST = 5
-- and check current state anytime with  /ldb
--============================================================
-- LDB_WEIGHT_KILL removed: kill-based scoring needed COMBAT_LOG_EVENT_UNFILTERED,
-- which is unavailable on this client (see the registration comment above).
LDB_WEIGHT_QUEST = LDB_WEIGHT_QUEST or 3        -- score added per quest turned in
LDB_WEIGHT_LEVEL = LDB_WEIGHT_LEVEL or 5        -- score added per level-up
LDB_FLUSH_THRESHOLD = LDB_FLUSH_THRESHOLD or 10     -- a safe window reloads once score >= this
LDB_ESCALATE_THRESHOLD = LDB_ESCALATE_THRESHOLD or 18 -- suggest a manual /reload if score reaches this with no safe window taken
-- EXPERIMENTAL, off by default: also try to reload by itself a few seconds
-- after a fresh login so a character reaches the dashboard with zero clicks.
-- Untested on this client — if the addon isn't allowed to call ReloadUI()
-- there, it throws ADDON_ACTION_BLOCKED (the sync button still appears as the
-- fallback). Try it with  /run LDB_AUTO_SYNC_ON_LOGIN = true  and turn it back
-- off the same way if BugSack shows that error.
LDB_AUTO_SYNC_ON_LOGIN = LDB_AUTO_SYNC_ON_LOGIN or false

-- Session-only scoring state — deliberately NOT saved, so it resets to 0
-- exactly when a reload actually happens (that reload IS the flush).
local LDB_score = 0
local LDB_escalated = false

--============================================================
-- SYNC BUTTON
-- The addon cannot reliably call ReloadUI() itself. Confirmed via a real
-- ADDON_ACTION_BLOCKED error at a flight path departure: PLAYER_CONTROL_LOST
-- fires downstream of a Blizzard-protected action (taking a taxi), so any
-- addon code triggered by it inherits that taint, and Reload() is a
-- protected function tainted code isn't allowed to call (at minimum during
-- combat lockdown; whether it's blocked more broadly on this beta is
-- unverified). Instead of self-triggering a reload from inside that event
-- handler, the addon shows this button at each safe window and lets the
-- player's own click do it. NOT verified that a click gets past the block —
-- this is still addon code, so if it throws ADDON_ACTION_BLOCKED too, the
-- next step is an action-button template rather than a plain button.
--============================================================
local syncButton = CreateFrame("Button", "LanDashboardSyncButton", UIParent, "UIPanelButtonTemplate")
syncButton:SetSize(160, 26)
syncButton:SetPoint("TOP", UIParent, "TOP", 0, -60)
syncButton:SetFrameStrata("HIGH")
syncButton:SetText("Sync LAN Dashboard")
syncButton:Hide()
syncButton:SetScript("OnClick", function()
    -- Still possible to click this mid-combat (e.g. a fight started right
    -- after a safe window showed it) — ReloadUI() is protected during combat
    -- lockdown, so check again here rather than let that throw.
    -- InCombatLockdown() is the flag the protection actually keys off;
    -- UnitAffectingCombat() can disagree with it around the edges of a fight.
    if InCombatLockdown() or UnitAffectingCombat("player") then
        print("|cffc0645a[LAN Dashboard] Can't sync mid-combat — click again once combat ends.|r")
        return
    end
    ReloadUI()
end)

-- Dismiss without syncing. A child of syncButton, so it hides with it and
-- comes back whenever the next safe window shows the button again.
local closeButton = CreateFrame("Button", nil, syncButton, "UIPanelCloseButton")
closeButton:SetSize(26, 26)
closeButton:SetPoint("LEFT", syncButton, "RIGHT", 2, 0)
closeButton:SetScript("OnClick", function()
    syncButton:Hide()
end)

local function addScore(amount, reasonLabel)
    LDB_score = LDB_score + amount
    if not LDB_escalated and LDB_score >= LDB_ESCALATE_THRESHOLD then
        LDB_escalated = true
        print(string.format("|cffc0645a[LAN Dashboard] %d pending updates queued (last: %s) — no safe window yet, click Sync LAN Dashboard (top of screen) whenever it's convenient.|r", LDB_score, reasonLabel))
        syncButton:Show()
    end
end

-- Called at each candidate safe window. Below threshold: does nothing, the
-- window just passes unused. At/above threshold: shows the sync button
-- rather than reloading directly — see the SYNC BUTTON block above for why.
local function tryFlush(reasonLabel)
    if LDB_score < LDB_FLUSH_THRESHOLD then
        return
    end
    print(string.format("|cff7fb069[LAN Dashboard] %d updates ready (%s) — click Sync LAN Dashboard (top of screen) to send them.|r", LDB_score, reasonLabel))
    syncButton:Show()
end

--============================================================
-- STATUS: the whole "where this character stands" snapshot
-- Level and XP are here because PLAYER_XP_UPDATE only fires when XP moves,
-- so a character that cannot gain XP — at the level cap, 20 on this beta —
-- never sent any and sat on the server's default level 1. Gold and time
-- played ride along because they have no change event worth listening to:
-- gold moves on every loot and vendor sale, and time played only exists
-- when asked for. Sampling all of it together at a few meaningful moments
-- is both cheaper and more useful than chasing each one separately.
--============================================================
local LDB_playedTotal, LDB_playedLevel = 0, 0   -- seconds, from TIME_PLAYED_MSG
local LDB_awaitingPlayed = false                -- a reply we asked for is in flight
local LDB_statusPending = false                 -- ...and a STATUS is waiting on it

local function sendStatus(playerName)
    local level = UnitLevel("player") or 1
    local currentXP = UnitXP("player") or 0
    -- At the cap the game reports 0 XP required for the next level, because
    -- there isn't one. Passed straight through rather than faked into a
    -- number; the server reads a 0 here as "at the cap".
    local maxXP = UnitXPMax("player") or 0
    -- Copper, the unit the game counts in. Converting to gold is the
    -- dashboard's job, so no precision is thrown away here.
    local money = (GetMoney and GetMoney()) or 0
    emit(playerName, "STATUS", string.format("%d,%d,%d,%d,%d,%d",
        level, currentXP, maxXP, money, LDB_playedTotal, LDB_playedLevel))
end

-- Time played is only knowable by asking the server and waiting for the
-- reply, so a status send becomes: ask, then emit when the answer lands.
-- The timer is the safety net — if the reply never comes (a disconnect at
-- the wrong moment), the snapshot still goes out with whatever played
-- figures we last had rather than being dropped.
local function requestStatus(playerName)
    if not RequestTimePlayed then
        sendStatus(playerName)
        return
    end
    LDB_statusPending = true
    LDB_awaitingPlayed = true
    RequestTimePlayed()
    C_Timer.After(5, function()
        LDB_awaitingPlayed = false
        if LDB_statusPending then
            LDB_statusPending = false
            sendStatus(playerName)
        end
    end)
end

-- Asking for time played makes the server print the usual /played lines to
-- chat. Only the ones this addon asked for are hidden — a player typing
-- /played themselves still sees their answer. Matching is done against the
-- game's own localised templates rather than English text.
local function looksLikeTimePlayedLine(message)
    for _, template in ipairs({ TIME_PLAYED_TOTAL, TIME_PLAYED_LEVEL }) do
        if type(template) == "string" then
            local prefix = template:match("^(.-)%%s")
            if prefix and prefix ~= "" and message:sub(1, #prefix) == prefix then
                return true
            end
        end
    end
    return false
end

if ChatFrame_AddMessageEventFilter then
    ChatFrame_AddMessageEventFilter("CHAT_MSG_SYSTEM", function(_, _, message)
        return LDB_awaitingPlayed and looksLikeTimePlayedLine(message or "")
    end)
end

-- Shared by login and by any later change to faction/class/guild, so a
-- player who changes guilds mid-session doesn't stay stale on the
-- dashboard until their next login.
local function sendProfile(playerName)
    local faction = UnitFactionGroup("player") or "Unknown"
    -- classToken is the stable, non-localized name (e.g. "WARRIOR"), safe
    -- to key colors/logic off; className is locale-specific display text.
    local _, classToken = UnitClass("player")
    classToken = classToken or "UNKNOWN"
    -- GetGuildInfo returns multiple parameters; we grab the first one (name).
    -- Empty string, not a "No Guild" placeholder — a real guild could
    -- literally be named that, and the dashboard needs to tell "not in a
    -- guild" apart from "in a guild named No Guild."
    local guildName = GetGuildInfo("player") or ""

    -- guildName is last since it's the only field that can contain commas
    emit(playerName, "PROFILE", string.format("%s,%s,%s", faction, classToken, guildName))
end

LanFrame:SetScript("OnEvent", function(self, event, ...)
    local playerName = UnitName("player")

    -- 1. TRACK PROFILE DATA (Faction, Class, Guild) ON LOGIN / ZONE TRANSITIONS
    if event == "PLAYER_ENTERING_WORLD" then
        local isInitialLogin, isReload = ...

        -- Skip on our own (or any manual) /reload — nothing about the
        -- character's profile or location actually changed, so this would
        -- just queue a redundant PROFILE+ZONE pair every single time the
        -- reload-trigger system does its job.
        if not isReload then
            sendProfile(playerName)

            -- Also check the zone right away upon loading into the game
            currentZone = GetZoneText() or "Unknown"
            emit(playerName, "ZONE", currentZone)

            -- ...and the full snapshot (level, XP, gold, time played), which
            -- otherwise only arrives when one of those happens to change.
            requestStatus(playerName)

            -- The PROFILE/ZONE just queued only exist in memory until
            -- SavedVariables is written, and nothing else would prompt for
            -- that yet: the score stays 0 until the first quest or level-up,
            -- so a brand-new character used to sit invisible on the
            -- dashboard until the player happened to /reload or log out.
            -- Prompt at login for those — but only the first time this
            -- character is ever seen (see LanDashboardDB.known); a repeat
            -- login has already been through a save, so re-prompting every
            -- time is just noise. Score-based prompts still apply as usual.
            if isInitialLogin and not LanDashboardDB.known[playerName] then
                LanDashboardDB.known[playerName] = true
                print("|cff7fb069[LAN Dashboard] Logged in — click Sync LAN Dashboard (top of screen) to appear on the dashboard.|r")
                syncButton:Show()
                if LDB_AUTO_SYNC_ON_LOGIN then
                    C_Timer.After(3, function()
                        if not (InCombatLockdown() or UnitAffectingCombat("player")) then
                            ReloadUI()
                        end
                    end)
                end
            end
        end

        -- Any other PLAYER_ENTERING_WORLD (portals, dungeons, taxis landing,
        -- hearths) is a loading screen already happening — prompting here
        -- means the reload's hitch has a good chance of hiding inside one
        -- that was going to happen anyway.
        if not isInitialLogin and not isReload then
            tryFlush("loading screen")
        end

    -- 1b. RE-SEND PROFILE WHEN GUILD OR FACTION CHANGES MID-SESSION
    elseif event == "PLAYER_GUILD_UPDATE" or event == "PLAYER_FACTION_CHANGED" then
        sendProfile(playerName)

    -- 2. TRACK ZONE CHANGE TRANSITIONS
    elseif event == "ZONE_CHANGED_NEW_AREA" or event == "ZONE_CHANGED" then
        local newZone = GetZoneText() or "Unknown"
        if newZone ~= currentZone and newZone ~= "" then
            currentZone = newZone
            emit(playerName, "ZONE", currentZone)
        end

    -- 3. CORE XP & LEVEL LOGGING HANDLERS
    elseif event == "PLAYER_XP_UPDATE" then
        local currentXP = UnitXP("player")
        local maxXP = UnitXPMax("player")
        local currentLevel = UnitLevel("player")

        if currentXP == 0 and currentLevel == 1 then return end

        emit(playerName, "XP", string.format("%d,%d,%d", currentLevel, currentXP, maxXP))

    elseif event == "QUEST_TURNED_IN" then
        local questID, xpReward = ...
        emit(playerName, "QUEST", string.format("%d,%d", questID, xpReward or 0))
        addScore(LDB_WEIGHT_QUEST, "quest")
        print(string.format("|cffe8a33d[LAN Dashboard] Score: %d|r", LDB_score))

    -- 3b. DEATHS
    -- Level and zone ride along so the dashboard can say *where* someone died
    -- without having to correlate against the surrounding ZONE events, which
    -- may not have been queued recently (a death in the same zone as the last
    -- one emits no ZONE event at all).
    elseif event == "TIME_PLAYED_MSG" then
        local totalSeconds, levelSeconds = ...
        LDB_playedTotal = totalSeconds or LDB_playedTotal
        LDB_playedLevel = levelSeconds or LDB_playedLevel
        LDB_awaitingPlayed = false
        if LDB_statusPending then
            LDB_statusPending = false
            sendStatus(playerName)
        end

    elseif event == "PLAYER_DEAD" then
        local deathZone = GetZoneText() or "Unknown"
        emit(playerName, "DEATH", string.format("%d,%s", UnitLevel("player") or 0, deathZone))

    elseif event == "PLAYER_LEVEL_UP" then
        local newLevel = ...
        local maxXP = UnitXPMax("player")
        emit(playerName, "XP", string.format("%d,0,%d", newLevel, maxXP))
        addScore(LDB_WEIGHT_LEVEL, "level up")
        -- The per-level timer restarts here, and gold has usually moved too.
        requestStatus(playerName)

    -- 4. RELOAD-TRIGGER SAFE WINDOWS (kill-based scoring removed — see the
    -- COMBAT_LOG_EVENT_UNFILTERED registration comment above)
    elseif event == "PLAYER_CONTROL_LOST" then
        tryFlush("flight path")

    elseif event == "PLAYER_FLAGS_CHANGED" then
        if UnitIsAFK("player") then
            tryFlush("afk")
        end
    end
end)

-- ---------------------------------------------------------------------------
-- Profession API probe (/ldb probe)
--
-- Which profession API this client exposes is an open question: the .toc
-- declares interface 16001, which matches neither Classic's 115xx nor
-- retail's 11xxxx numbering, so this is a revamped client and may well use
-- retail's GetProfessions() rather than Classic's skill list. This beta has
-- already removed COMBAT_LOG_EVENT_UNFILTERED from addon access mid-project,
-- so nothing below assumes a function exists, or that its return signature is
-- what the documentation claims: every call goes through pcall and reports the
-- failure instead of erroring the addon.
--
-- Results are written to LanDashboardDB.probe as well as printed, so they
-- survive a /reload and can be read straight out of SavedVariables rather than
-- transcribed out of a chat window by a tester.

local PROBE_FUNCS = {
    "GetProfessions", "GetProfessionInfo",                       -- retail path
    "GetNumSkillLines", "GetSkillLineInfo", "ExpandSkillHeader",  -- classic path
    "C_TradeSkillUI",                                            -- modern namespace
}

local PROBE_CHAT_LIMIT = 12

local function probeRetail()
    if not GetProfessions or not GetProfessionInfo then
        return nil, "GetProfessions/GetProfessionInfo absent"
    end
    -- GetProfessions returns up to five values, any of which may be nil; a
    -- table constructor drops the nils, and pairs skips the resulting holes.
    local ok, indices = pcall(function() return { GetProfessions() } end)
    if not ok then
        return nil, "GetProfessions() errored: " .. tostring(indices)
    end
    local found = {}
    for _, index in pairs(indices) do
        local okInfo, name, _, rank, maxRank = pcall(GetProfessionInfo, index)
        -- Insist on a string: a revamped client returning some other shape
        -- should read as unreadable rather than be reported as a profession
        -- literally named "42".
        if okInfo and type(name) == "string" then
            found[#found + 1] = string.format("%s %s/%s", tostring(name), tostring(rank), tostring(maxRank))
        else
            found[#found + 1] = string.format("index %s -> unreadable (%s)", tostring(index), tostring(name))
        end
    end
    return found
end

local function probeClassic()
    if not GetNumSkillLines or not GetSkillLineInfo then
        return nil, "GetNumSkillLines/GetSkillLineInfo absent"
    end
    -- A collapsed header hides its children from the index list entirely, so
    -- anything that enumerates without expanding first silently misses the
    -- professions it is looking for.
    if ExpandSkillHeader then
        pcall(ExpandSkillHeader, 0)
    end
    local okCount, count = pcall(GetNumSkillLines)
    if not okCount then
        return nil, "GetNumSkillLines() errored: " .. tostring(count)
    end
    -- Distinct from the above on purpose: "it threw" and "it answered with
    -- something that isn't a count" mean different things when diagnosing a
    -- client we can't inspect directly.
    if type(count) ~= "number" then
        return nil, "GetNumSkillLines() returned " .. type(count) .. ", not a number"
    end
    -- Each entry is recorded with the header it sits under, because that is
    -- what decides whether professions can be told apart from weapon skills,
    -- languages and riding -- the real risk with this path.
    local found = {}
    local header = "(none)"
    for i = 1, count do
        local ok, name, isHeader, _, rank, _, _, maxRank = pcall(GetSkillLineInfo, i)
        if ok and type(name) == "string" then
            if isHeader then
                header = tostring(name)
            else
                found[#found + 1] = string.format("[%s] %s %s/%s",
                    header, tostring(name), tostring(rank), tostring(maxRank))
            end
        end
    end
    return found
end

local function reportSection(label, found, err)
    if not found then
        print(string.format("|cffe8a33d  %s:|r unavailable -- %s", label, tostring(err)))
        return
    end
    print(string.format("|cffe8a33d  %s:|r %d entries", label, #found))
    for i = 1, math.min(#found, PROBE_CHAT_LIMIT) do
        print("    " .. found[i])
    end
    if #found > PROBE_CHAT_LIMIT then
        print(string.format("    ... and %d more (full list in SavedVariables)", #found - PROBE_CHAT_LIMIT))
    end
end

local function runProbe()
    local report = { addon = "3.1.0" }
    report.when = (date and date("%Y-%m-%d %H:%M:%S")) or tostring(time and time() or "?")

    local okBuild, version, build, buildDate, tocVersion = pcall(GetBuildInfo)
    if okBuild then
        report.client = string.format("%s build %s (%s) toc %s",
            tostring(version), tostring(build), tostring(buildDate), tostring(tocVersion))
    else
        report.client = "GetBuildInfo() errored: " .. tostring(version)
    end

    report.present = {}
    for _, name in ipairs(PROBE_FUNCS) do
        report.present[name] = (_G[name] ~= nil)
    end

    report.retail, report.retailError = probeRetail()
    report.classic, report.classicError = probeClassic()
    LanDashboardDB.probe = report

    local present = {}
    for _, name in ipairs(PROBE_FUNCS) do
        present[#present + 1] = name .. "=" .. tostring(report.present[name])
    end

    print("|cffe8a33d[LAN Dashboard] Profession API probe|r")
    print("  client: " .. report.client)
    print("  present: " .. table.concat(present, " "))
    reportSection("retail GetProfessions()", report.retail, report.retailError)
    reportSection("classic skill list", report.classic, report.classicError)
    print("|cffe8a33d  Saved to LanDashboardDB.probe -- type /reload, then send me the probe block from SavedVariables.|r")
end

SLASH_LANDASHBOARD1 = "/ldb"
SlashCmdList["LANDASHBOARD"] = function(msg)
    if msg == "sync" then
        syncButton:Show()
        print("|cffe8a33d[LAN Dashboard] Sync button shown — click it (top of screen) to test.|r")
        return
    end
    if msg == "verbose" then
        LDB_VERBOSE = not LDB_VERBOSE
        print(string.format("|cffe8a33d[LAN Dashboard] Event echo %s.|r", LDB_VERBOSE and "ON — every recorded event will print here" or "off"))
        return
    end
    if msg == "probe" then
        runProbe()
        return
    end
    if msg == "forget" then
        LanDashboardDB.known = {}
        print("|cffe8a33d[LAN Dashboard] Forgot all known characters — the next login of each will prompt to sync again.|r")
        return
    end
    print(string.format(
        "|cffe8a33d[LAN Dashboard] score=%d  flush=%d  escalate=%d  queued=%d  |  weights: quest=%d level=%d  |  auto-sync on login: %s  |  echo: %s|r",
        LDB_score, LDB_FLUSH_THRESHOLD, LDB_ESCALATE_THRESHOLD, LanDashboardDB.nextId - 1,
        LDB_WEIGHT_QUEST, LDB_WEIGHT_LEVEL, tostring(LDB_AUTO_SYNC_ON_LOGIN), tostring(LDB_VERBOSE)
    ))
end

print("|cffcd7f32LAN Dashboard v3.1.0 Initialized! Type /ldb to check reload-trigger status, /ldb sync to test the sync button, /ldb probe to report this client's profession API.|r")
