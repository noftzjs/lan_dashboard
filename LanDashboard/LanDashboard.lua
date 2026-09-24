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
LanFrame:RegisterEvent("ADDON_LOADED")          -- The point SavedVariables are guaranteed restored
LanFrame:RegisterEvent("PLAYER_CONTROL_LOST")   -- Fires when a flight path departs — a safe reload window
LanFrame:RegisterEvent("PLAYER_FLAGS_CHANGED")  -- Fires on AFK toggle — another safe reload window
-- Closes an open AFK interval on the way out. pcall'd rather than registered
-- outright: this beta has already removed one event from addon access, and a
-- failed registration at load would take the whole addon down with it.
pcall(function() LanFrame:RegisterEvent("PLAYER_LOGOUT") end)

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
-- Defaults are applied twice, and that is deliberate.
--
-- Saved state has looked unreliable on this client (2026-09-24): nextId at 1
-- after days of play, an empty known-character ledger, an AFK total present in
-- one save and gone from the next. Most of the surrounding noise turned out to
-- be a different bug -- the watcher was parsing /ldb probe's report as the
-- event queue -- so this is NOT a confirmed diagnosis of the remainder.
--
-- Doing it in both places is cheap insurance either way. ADDON_LOADED is the
-- point the platform guarantees saved globals are in place, and initialising
-- there is the documented-safe pattern regardless of what this client does at
-- file scope. Whichever order applies, one call is the real one and the other
-- is a no-op: every assignment is an `or` that keeps an existing value.
--
-- So the same defaults are applied again at ADDON_LOADED, where the saved
-- globals are guaranteed to be in place. Whichever order this client actually
-- uses, one call is the real one and the other is a no-op: every assignment is
-- an `or` that keeps an existing value, so neither can clobber the other.
function LDB_ApplySavedDefaults()
    LanDashboardDB = LanDashboardDB or {}
    LanDashboardDB.events = LanDashboardDB.events or {}
    LanDashboardDB.nextId = LanDashboardDB.nextId or 1
    LanDashboardDB.known = LanDashboardDB.known or {}
    LanDashboardDB.private = LanDashboardDB.private or {}
    LanDashboardDB.defaults = LanDashboardDB.defaults or {}

    -- Sharing gold is opt-in rather than opt-out: it is the one figure testers
    -- actually asked to keep private, so the safe state is the default and a
    -- player chooses to reveal it. Applied once per install and then
    -- remembered, so someone who switches it on does not find it off again at
    -- the next login.
    if not LanDashboardDB.defaults.goldOptIn then
        LanDashboardDB.defaults.goldOptIn = true
        if LanDashboardDB.private.gold == nil then
            LanDashboardDB.private.gold = true
        end
    end

    LDB_VERBOSE = LDB_VERBOSE or false
    LDB_AUTO_SYNC_ON_LOGIN = LDB_AUTO_SYNC_ON_LOGIN or false
    LDB_WEIGHT_QUEST = LDB_WEIGHT_QUEST or 3
    LDB_WEIGHT_LEVEL = LDB_WEIGHT_LEVEL or 5
    LDB_FLUSH_THRESHOLD = LDB_FLUSH_THRESHOLD or 10
    LDB_ESCALATE_THRESHOLD = LDB_ESCALATE_THRESHOLD or 18
end

LDB_ApplySavedDefaults()

-- Characters this addon has already queued a PROFILE for (name -> true). Lets
-- the login sync prompt fire only the first time a character is ever seen
-- rather than on every login: anything already queued gets written out by the
-- normal logout save at the latest, so a repeat prompt adds nothing. Also
-- added to older saved tables that predate it. Clear with  /ldb forget
-- (e.g. after pointing the server at a fresh DB_FILE, when everyone needs to
-- register again).
-- (applied by LDB_ApplySavedDefaults, at load and again at ADDON_LOADED)

-- Debug echo, off by default: every queued event printed to chat. This used
-- to be how the data actually left the game (the watcher read the chat log),
-- so it had to print — but SavedVariables carries the data now and the echo
-- is just noise: PLAYER_XP_UPDATE alone fires on every mob kill and quest
-- reward, which floods the chat frame during normal play. Turn it back on
-- with /ldb verbose when diagnosing what the addon is or isn't recording.
-- LDB_VERBOSE default: see LDB_ApplySavedDefaults

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
-- Tuning knobs, defaulted in LDB_ApplySavedDefaults:
--   LDB_WEIGHT_QUEST       score added per quest turned in
--   LDB_WEIGHT_LEVEL       score added per level-up
--   LDB_FLUSH_THRESHOLD    a safe window reloads once score >= this
--   LDB_ESCALATE_THRESHOLD suggest a manual /reload if score reaches this
--                          with no safe window taken
-- EXPERIMENTAL, off by default: also try to reload by itself a few seconds
-- after a fresh login so a character reaches the dashboard with zero clicks.
-- Untested on this client — if the addon isn't allowed to call ReloadUI()
-- there, it throws ADDON_ACTION_BLOCKED (the sync button still appears as the
-- fallback). Try it with  /run LDB_AUTO_SYNC_ON_LOGIN = true  and turn it back
-- off the same way if BugSack shows that error.
-- LDB_AUTO_SYNC_ON_LOGIN default: see LDB_ApplySavedDefaults

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

--============================================================
-- AFK TIME, PROFESSIONS, ITEM LEVEL
--============================================================
-- All three ride along on STATUS rather than getting their own log type. That
-- is a deployment decision, not a style one: an older watcher does not skip an
-- unknown log type harmlessly -- it treats the line as malformed, advances
-- sent_count past it, and the event is gone. Extra fields on an existing type
-- are forwarded by every watcher and ignored by every older server.

-- Time spent flagged AFK. Accumulated here because the game does not report
-- it: /played counts time logged in, not time idle.
--
-- Unlike /played, which the realm server vouches for, this number is ours. It
-- only counts time since the addon was installed and it resets if
-- SavedVariables is wiped, which is why the dashboard labels it as an
-- estimate rather than implying the game agrees.
local afkSince = nil   -- Session-local on purpose: a crash mid-AFK loses the
                       -- open interval, which is better than persisting a
                       -- start time that may never be closed and would later
                       -- read as days of idling.

local function afkTotalFor(playerName)
    return (LanDashboardDB.afk and LanDashboardDB.afk[playerName]) or 0
end

-- Closes an open AFK interval and banks it. Safe to call when none is open.
local function afkAccumulate(playerName)
    if not afkSince or not playerName then return end
    local elapsed = eventTime() - afkSince
    afkSince = nil
    if elapsed <= 0 then return end
    LanDashboardDB.afk = LanDashboardDB.afk or {}
    LanDashboardDB.afk[playerName] = afkTotalFor(playerName) + elapsed
end

-- A field the player has asked not to share. Sent as an empty slot rather than
-- omitted, because STATUS is positional and dropping a field would shift every
-- field after it.
local function isPrivate(field)
    return (LanDashboardDB.private and LanDashboardDB.private[field]) and true or false
end

-- Slots 1-2 are the primaries and 3-5 the secondaries on this client (measured
-- with /ldb probe, 2026-09-24 -- First Aid sits where retail puts archaeology).
-- The slot number and the spellbook index are different numbers and do not
-- correlate here, so the returned index is what gets passed on, never the slot.
local function professionFields()
    if type(GetProfessions) ~= "function" or type(GetProfessionInfo) ~= "function" then
        return nil
    end
    local ok, a, b, c, d, e = pcall(GetProfessions)
    if not ok then return nil end
    local indices = { a, b, c, d, e }
    local fields = {}
    for slot = 1, 5 do
        local index = indices[slot]
        if index then
            local okInfo, name, _, rank, maxRank = pcall(GetProfessionInfo, index)
            if okInfo and type(name) == "string" and tonumber(rank) and tonumber(maxRank) then
                -- Commas and colons are this payload's own separators. A name
                -- carrying one would make the line unparseable, the server
                -- would reject it, and the event would be destroyed -- the
                -- watcher's sent_count has already moved past it by then. No
                -- real profession name contains either; this is here so a
                -- localised or renamed one cannot cost a player their data.
                name = name:gsub("[,:]", " ")
                fields[#fields + 1] = string.format("%s:%d:%d", name, rank, maxRank)
            end
        end
    end
    return fields
end

-- Retail returns overall, equipped, pvp, and this client matches that shape.
-- Equipped is the honest number for a dashboard: overall counts upgrades being
-- carried but not worn, which would flatter a player hoarding them.
local function equippedItemLevel()
    if type(GetAverageItemLevel) ~= "function" then return nil end
    local ok, overall, equipped = pcall(GetAverageItemLevel)
    if not ok then return nil end
    return tonumber(equipped) or tonumber(overall)
end

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
    -- An empty slot means "the player declined to share this". The server maps
    -- it to a withheld field rather than to zero -- which matters, because a
    -- character genuinely can be broke.
    local goldField = isPrivate("gold") and "" or string.format("%d", money)

    local head = string.format("%d,%d,%d,%s,%d,%d",
        level, currentXP, maxXP, goldField, LDB_playedTotal, LDB_playedLevel)

    local professions = professionFields()
    local itemLevel = equippedItemLevel()
    if professions == nil and itemLevel == nil then
        -- Nothing in the tail is obtainable on this client, so send the plain
        -- six-field form. An older server would have ignored the tail anyway,
        -- but this keeps the payload honest rather than padding it with empty
        -- slots that would read as deliberate refusals.
        emit(playerName, "STATUS", head)
        return
    end

    local itemLevelField = ""
    if itemLevel and not isPrivate("item_level") then
        itemLevelField = string.format("%.2f", itemLevel)
    end
    local afkField = isPrivate("afk_total") and "" or
        string.format("%d", afkTotalFor(playerName))

    -- The trailing comma is load-bearing: it makes the profession list present
    -- but empty, which the server reads as "this character has none". Without
    -- it the field is absent, and absent means "an older addon that cannot
    -- report professions" -- so the server would keep stale ones instead.
    emit(playerName, "STATUS", string.format("%s,%s,%s,%s",
        head, itemLevelField, afkField, table.concat(professions or {}, ",")))
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
    elseif event == "ADDON_LOADED" then
        -- Fires for every addon that loads, so check it is ours first. Read
        -- from varargs, the same way every other branch here does.
        local loadedAddon = ...
        if loadedAddon == "LanDashboard" then
            LDB_ApplySavedDefaults()
        end

    elseif event == "PLAYER_CONTROL_LOST" then
        tryFlush("flight path")

    elseif event == "PLAYER_FLAGS_CHANGED" then
        if UnitIsAFK("player") then
            -- Opening edge. Guarded because this event also fires for flag
            -- changes that have nothing to do with AFK, and restarting the
            -- clock on each would undercount a long idle.
            if not afkSince then afkSince = eventTime() end
            tryFlush("afk")
        else
            afkAccumulate(UnitName("player"))
        end

    elseif event == "PLAYER_LOGOUT" then
        -- Bank an interval that is still open, so a session ending while AFK
        -- still counts. SavedVariables is written after this fires.
        afkAccumulate(UnitName("player"))
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

-- Every entry below is here because a current or backlogged feature depends on
-- the answer -- this is not a catalogue of the API surface for its own sake.
-- Each is { function name, args... }; the args are harmless reads.
local PROBE_CALLS = {
    -- Identity. Drives the open "WoW Forever" player-keying question: if the
    -- single-server model is real, realm may be empty or constant, which is
    -- exactly what the dashboard currently keys players on.
    { "UnitName", "player" },
    { "UnitFullName", "player" },
    { "GetRealmName" },
    { "GetNormalizedRealmName" },
    { "UnitGUID", "player" },
    { "UnitFactionGroup", "player" },
    { "GetGuildInfo", "player" },
    -- Location. Zone is currently stored as a localised display string; a map
    -- ID would be stable across clients and languages.
    { "GetZoneText" },
    { "GetSubZoneText" },
    { "GetRealZoneText" },
    -- Progress and the stats already tracked, plus AFK for that backlog item.
    { "GetMoney" },
    { "UnitXP", "player" },
    { "UnitXPMax", "player" },
    { "GetXPExhaustion" },
    { "UnitIsAFK", "player" },
    { "GetAverageItemLevel" },
    -- LoggingCombat() with no argument reports the current state without
    -- changing it. This one decides whether "damage done" is even reachable:
    -- the combat log file is useless if nobody can turn it on automatically.
    { "LoggingCombat" },
    -- Statistics would be a shortcut for several "more data points" items
    -- (deaths, quests completed) without tracking them ourselves.
    { "GetStatistic", 60 },
    { "GetAchievementInfo", 60 },
}

local PROBE_NAMESPACES = {
    { "C_Map", { "GetBestMapForUnit", "GetMapInfo", "GetPlayerMapPosition" } },
    { "C_PlayerInfo", { "GetClass", "GetRace", "UnitIsSameServer" } },
    { "C_QuestLog", { "GetNumQuestLogEntries", "GetInfo", "IsQuestFlaggedCompleted" } },
    { "C_TradeSkillUI", { "GetBaseProfessionInfo", "GetTradeSkillLine" } },
    { "C_AchievementInfo", { "GetSupercedingAchievements" } },
    { "C_Container", { "GetContainerNumSlots" } },
    { "C_CurrencyInfo", { "GetCurrencyInfo" } },
    { "C_DateAndTime", { "GetServerTimeLocal" } },
    -- Whether the settings panel can register itself in the game's own options
    -- menu, instead of only opening from /ldb. The two lineages disagree here
    -- (retail uses Settings.*, Classic used InterfaceOptions_AddCategory) and
    -- this client is neither, so it gets probed rather than guessed at.
    { "Settings", { "RegisterCanvasLayoutCategory", "RegisterAddOnCategory", "OpenToCategory" } },
}

-- COMBAT_LOG_EVENT_UNFILTERED is deliberately NOT in this list. It is already
-- known removed, and registering a forbidden event is what raises the
-- ADDON_ACTION_FORBIDDEN dialog that broke the addon before -- re-testing it
-- would cost a tester an intrusive popup to learn nothing new.
local PROBE_EVENTS = {
    "PLAYER_FLAGS_CHANGED", "TIME_PLAYED_MSG", "PLAYER_MONEY", "PLAYER_XP_UPDATE",
    "PLAYER_LEVEL_UP", "PLAYER_DEAD", "QUEST_TURNED_IN", "ZONE_CHANGED_NEW_AREA",
    "SKILL_LINES_CHANGED", "TRADE_SKILL_UPDATE", "ACHIEVEMENT_EARNED",
    "PLAYER_EQUIPMENT_CHANGED", "UPDATE_FACTION", "CHAT_MSG_SYSTEM",
}

local function describeReturns(...)
    local count = select("#", ...)
    if count == 0 then return "(no return)" end
    local parts = {}
    for i = 1, count do
        parts[#parts + 1] = tostring((select(i, ...)))
    end
    return table.concat(parts, " | ")
end

-- Calls a global safely and renders whatever came back. Written so a nil in
-- the middle of a return list is preserved rather than silently truncated.
local function callInfo(name, ...)
    local fn = _G[name]
    if type(fn) ~= "function" then
        return (_G[name] ~= nil) and ("present but " .. type(_G[name])) or "absent"
    end
    local function handle(ok, ...)
        if not ok then return "errored: " .. tostring((select(1, ...))) end
        return describeReturns(...)
    end
    return handle(pcall(fn, ...))
end

local function namespaceInfo(name, members)
    local ns = _G[name]
    if type(ns) ~= "table" then return "absent" end
    local have = {}
    for _, member in ipairs(members) do
        have[#have + 1] = member .. "=" .. ((ns[member] ~= nil) and "yes" or "no")
    end
    return table.concat(have, " ")
end

-- Whether an event can be registered at all. Each is unregistered immediately;
-- registering does not make it fire, so this observes without subscribing.
local function probeEvents()
    local results = {}
    local okFrame, frame = pcall(CreateFrame, "Frame")
    if not okFrame or not frame then
        results["(all)"] = "CreateFrame unavailable"
        return results
    end
    for _, event in ipairs(PROBE_EVENTS) do
        local ok, err = pcall(frame.RegisterEvent, frame, event)
        if ok then
            results[event] = "ok"
            pcall(frame.UnregisterEvent, frame, event)
        else
            results[event] = "REFUSED: " .. tostring(err)
        end
    end
    return results
end

-- Slot-aware, unlike the v3.1.0 probe. GetProfessions returns fixed positions
-- and this client returned First Aid -- which modern retail does not have --
-- so its slot meanings are demonstrably not retail's and have to be read off
-- rather than assumed. The list constructor keeps the holes, so position is
-- preserved where pairs() would have dropped it.
local function probeProfessionSlots()
    if type(GetProfessions) ~= "function" then return nil end
    local ok, a, b, c, d, e = pcall(GetProfessions)
    if not ok then return nil end
    local indices = { a, b, c, d, e }
    local slots = {}
    for slot = 1, 5 do
        local index = indices[slot]
        if index == nil then
            slots["slot" .. slot] = "(empty)"
        else
            local okInfo, name, _, rank, maxRank = pcall(GetProfessionInfo, index)
            if okInfo and type(name) == "string" then
                slots["slot" .. slot] = string.format("%s %s/%s (book index %s)",
                    name, tostring(rank), tostring(maxRank), tostring(index))
            else
                slots["slot" .. slot] = "index " .. tostring(index) .. " unreadable"
            end
        end
    end
    return slots
end

-- A stable map ID would be a better zone key than the localised name the
-- dashboard stores today; this reports whether one is obtainable.
local function probeMap()
    if type(C_Map) ~= "table" or type(C_Map.GetBestMapForUnit) ~= "function" then
        return "C_Map.GetBestMapForUnit absent"
    end
    local ok, mapId = pcall(C_Map.GetBestMapForUnit, "player")
    if not ok or not mapId then return "no map id for player" end
    local okInfo, info = pcall(C_Map.GetMapInfo, mapId)
    local name = (okInfo and type(info) == "table" and info.name) or "?"
    return string.format("id %s = %s", tostring(mapId), tostring(name))
end

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
    local report = { addon = "3.5.0" }
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
    report.professionSlots = probeProfessionSlots()
    report.map = probeMap()

    report.calls = {}
    for _, entry in ipairs(PROBE_CALLS) do
        report.calls[entry[1]] = callInfo(entry[1], entry[2], entry[3])
    end
    report.namespaces = {}
    for _, entry in ipairs(PROBE_NAMESPACES) do
        report.namespaces[entry[1]] = namespaceInfo(entry[1], entry[2])
    end
    -- NOT "events": the watcher finds the queue by searching for the first
    -- ["events"] in the saved file, and ["probe"] sorts ahead of the real
    -- ["events"] -- so a key by that name here gets parsed as the event queue.
    report.eventRegistration = probeEvents()

    LanDashboardDB.probe = report

    local present = {}
    for _, name in ipairs(PROBE_FUNCS) do
        present[#present + 1] = name .. "=" .. tostring(report.present[name])
    end

    print("|cffe8a33d[LAN Dashboard] Client API probe|r")
    print("  client: " .. report.client)
    print("  present: " .. table.concat(present, " "))
    reportSection("retail GetProfessions()", report.retail, report.retailError)
    reportSection("classic skill list", report.classic, report.classicError)

    print("|cffe8a33d  profession slots:|r")
    if report.professionSlots then
        for slot = 1, 5 do
            print("    slot" .. slot .. ": " .. tostring(report.professionSlots["slot" .. slot]))
        end
    else
        print("    unavailable")
    end
    print("|cffe8a33d  map:|r " .. tostring(report.map))

    local refused = {}
    for _, event in ipairs(PROBE_EVENTS) do
        if report.eventRegistration[event] ~= "ok" then
            refused[#refused + 1] = event
        end
    end
    print(string.format("|cffe8a33d  events:|r %d of %d registrable%s",
        #PROBE_EVENTS - #refused, #PROBE_EVENTS,
        (#refused > 0) and (" -- REFUSED: " .. table.concat(refused, ", ")) or ""))

    local absent = {}
    for _, entry in ipairs(PROBE_CALLS) do
        if report.calls[entry[1]] == "absent" then absent[#absent + 1] = entry[1] end
    end
    print(string.format("|cffe8a33d  calls:|r %d probed%s", #PROBE_CALLS,
        (#absent > 0) and (" -- absent: " .. table.concat(absent, ", ")) or " -- all present"))
    for _, entry in ipairs(PROBE_NAMESPACES) do
        print("    " .. entry[1] .. ": " .. tostring(report.namespaces[entry[1]]))
    end
    print("|cffe8a33d  (full values are in SavedVariables, not printed here)|r")
    print("|cffe8a33d  Saved to LanDashboardDB.probe -- type /reload, then send me the probe block from SavedVariables.|r")
end

--============================================================
-- SETTINGS PANEL
--============================================================
-- A slash command nobody remembers is not really a setting, and a toggle whose
-- state you cannot see is worse -- a player who forgets whether they hid their
-- gold has no way to check short of watching the dashboard. Everything
-- adjustable lives here instead, with its current state on screen.
--
-- Built from plain frames and coloured textures rather than SetBackdrop: that
-- moved behind a mixin template in later clients and this one is a revamp of
-- uncertain lineage. Only two templates are used and both are already proven
-- in this addon, since the sync button uses them.

local configFrame

local PRIVACY_OPTIONS = {
    { field = "gold", label = "Share my gold",
      hint = "Your total, shown on the dashboard as 12g34s56c." },
    { field = "item_level", label = "Share my item level",
      hint = "Average item level of what you have equipped." },
    { field = "afk_total", label = "Share my AFK time",
      hint = "Time flagged AFK, measured by this addon." },
}

-- Checked means shared. The stored flag is the opposite ("private"), but a
-- settings panel reads better in the positive and sharing is the default, so
-- the inversion happens here rather than in the player's head.
local function isShared(field)
    return not isPrivate(field)
end

local function setShared(field, shared)
    LanDashboardDB.private = LanDashboardDB.private or {}
    -- nil rather than false, so a shared field leaves no entry behind.
    LanDashboardDB.private[field] = (not shared) or nil
end

local function makeToggle(parent, yOffset, label, hint, get, set)
    local box
    local ok = pcall(function()
        box = CreateFrame("CheckButton", nil, parent, "UICheckButtonTemplate")
    end)
    if not ok or not box then
        -- Unlike the two templates above, this one is not proven on this
        -- client. The fallback carries its state in its own text and needs no
        -- template at all, so the panel cannot be broken by a missing one.
        box = CreateFrame("Button", nil, parent)
        box.mark = box:CreateFontString(nil, "ARTWORK", "GameFontNormalLarge")
        box.mark:SetPoint("LEFT", box, "LEFT", 0, 0)
    end
    box:SetSize(26, 26)
    box:SetPoint("TOPLEFT", parent, "TOPLEFT", 16, yOffset)

    local title = parent:CreateFontString(nil, "ARTWORK", "GameFontNormal")
    title:SetPoint("LEFT", box, "RIGHT", 4, 0)
    title:SetText(label)

    local sub = parent:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
    sub:SetPoint("TOPLEFT", title, "BOTTOMLEFT", 0, -3)
    sub:SetWidth(300)
    sub:SetJustifyH("LEFT")
    sub:SetText(hint)

    function box:Refresh()
        local on = get()
        if self.SetChecked then self:SetChecked(on) end
        if self.mark then
            self.mark:SetText(on and "|cff4ade80[x]|r" or "|cff8d8d99[  ]|r")
        end
        title:SetText(label)
    end

    box:SetScript("OnClick", function(self)
        set(not get())
        self:Refresh()
    end)
    return box
end

local function buildConfigFrame()
    local f = CreateFrame("Frame", "LanDashboardConfig", UIParent)
    f:SetSize(380, 322)
    f:SetPoint("CENTER")
    f:SetFrameStrata("DIALOG")
    f:EnableMouse(true)
    f:SetMovable(true)
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", f.StartMoving)
    f:SetScript("OnDragStop", f.StopMovingOrSizing)
    f:Hide()

    -- The rim is drawn first and slightly larger; the fill sits on top and
    -- inset, leaving a 1px edge. Cheaper than a nine-slice and it cannot go
    -- missing on a client that moved the backdrop API.
    local rim = f:CreateTexture(nil, "BACKGROUND")
    rim:SetPoint("TOPLEFT", f, "TOPLEFT", -1, 1)
    rim:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", 1, -1)
    rim:SetColorTexture(0.95, 0.66, 0.24, 0.5)

    local fill = f:CreateTexture(nil, "BORDER")
    fill:SetAllPoints(f)
    fill:SetColorTexture(0.055, 0.055, 0.07, 0.95)

    local title = f:CreateFontString(nil, "ARTWORK", "GameFontNormalLarge")
    title:SetPoint("TOPLEFT", f, "TOPLEFT", 16, -14)
    title:SetText("|cfff3a73cLAN Dashboard|r")

    local version = f:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
    version:SetPoint("LEFT", title, "RIGHT", 6, -1)
    version:SetText("v3.5.0")

    local close = CreateFrame("Button", nil, f, "UIPanelCloseButton")
    close:SetPoint("TOPRIGHT", f, "TOPRIGHT", -2, -2)
    close:SetScript("OnClick", function() f:Hide() end)

    local heading = f:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
    heading:SetPoint("TOPLEFT", f, "TOPLEFT", 16, -44)
    heading:SetText("|cff8d8d99WHAT THE DASHBOARD MAY SHOW|r")

    f.toggles = {}
    local y = -62
    for _, option in ipairs(PRIVACY_OPTIONS) do
        local field = option.field
        f.toggles[#f.toggles + 1] = makeToggle(f, y, option.label, option.hint,
            function() return isShared(field) end,
            function(value) setShared(field, value) end)
        y = y - 44
    end

    local divider = f:CreateTexture(nil, "ARTWORK")
    divider:SetPoint("TOPLEFT", f, "TOPLEFT", 16, y - 4)
    divider:SetSize(348, 1)
    divider:SetColorTexture(1, 1, 1, 0.08)

    f.toggles[#f.toggles + 1] = makeToggle(f, y - 18, "Show events in chat",
        "Prints each recorded event. Useful for debugging, noisy otherwise.",
        function() return LDB_VERBOSE and true or false end,
        function(value) LDB_VERBOSE = value end)

    local footer = f:CreateFontString(nil, "ARTWORK", "GameFontDisableSmall")
    footer:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 16, 44)
    footer:SetWidth(348)
    footer:SetJustifyH("LEFT")
    -- Says plainly that nothing takes effect until the next sync. Without it a
    -- player unticks gold, sees it still on the dashboard, and reasonably
    -- concludes the setting is broken.
    footer:SetText("Changes apply at your next sync. Hidden values are never sent " ..
                   "to the server, so they cannot appear on the dashboard.")

    local sync = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    sync:SetSize(120, 24)
    sync:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 16, 14)
    sync:SetText("Sync now")
    sync:SetScript("OnClick", function()
        f:Hide()
        -- Reuses the existing button rather than duplicating its combat
        -- checks; that path is the one proven safe against taint in-game.
        syncButton:Show()
    end)

    local done = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    done:SetSize(90, 24)
    done:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -16, 14)
    done:SetText("Close")
    done:SetScript("OnClick", function() f:Hide() end)

    function f:RefreshAll()
        for _, toggle in ipairs(self.toggles) do toggle:Refresh() end
    end
    return f
end

local function showConfig()
    if not configFrame then
        local ok, built = pcall(buildConfigFrame)
        if not ok or not built then
            print("|cffe8a33d[LAN Dashboard] Couldn't open the settings window. " ..
                  "Use /ldb private gold instead.|r")
            return
        end
        configFrame = built
    end
    configFrame:RefreshAll()
    configFrame:Show()
end

SLASH_LANDASHBOARD1 = "/ldb"
SlashCmdList["LANDASHBOARD"] = function(msg)
    if msg == "" or msg == "config" or msg == "options" then
        showConfig()
        return
    end
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
    local privateField = msg:match("^private%s+(%S+)$")
    if privateField then
        local allowed = { gold = true, item_level = true, afk_total = true }
        if not allowed[privateField] then
            print("|cffe8a33d[LAN Dashboard] Can't hide '" .. privateField ..
                  "'. Try: gold, item_level, afk_total.|r")
            return
        end
        LanDashboardDB.private = LanDashboardDB.private or {}
        LanDashboardDB.private[privateField] = not LanDashboardDB.private[privateField]
        local hidden = LanDashboardDB.private[privateField]
        print(string.format(
            "|cffe8a33d[LAN Dashboard] %s is now %s. It updates on your next sync.|r",
            privateField, hidden and "HIDDEN from the dashboard" or "shared again"))
        return
    end
    if msg == "private" then
        local shown = {}
        for _, field in ipairs({ "gold", "item_level", "afk_total" }) do
            shown[#shown + 1] = field .. "=" .. (isPrivate(field) and "hidden" or "shared")
        end
        print("|cffe8a33d[LAN Dashboard] " .. table.concat(shown, "  ") ..
              "  |  /ldb private gold to toggle.|r")
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
    -- Reached by /ldb status, or by any unrecognised argument, so a typo
    -- still produces something rather than silence.
    print(string.format(
        "|cffe8a33d[LAN Dashboard] score=%d  flush=%d  escalate=%d  queued=%d  |  weights: quest=%d level=%d  |  auto-sync on login: %s  |  echo: %s|r",
        LDB_score, LDB_FLUSH_THRESHOLD, LDB_ESCALATE_THRESHOLD, LanDashboardDB.nextId - 1,
        LDB_WEIGHT_QUEST, LDB_WEIGHT_LEVEL, tostring(LDB_AUTO_SYNC_ON_LOGIN), tostring(LDB_VERBOSE)
    ))
end

print("|cffcd7f32LAN Dashboard v3.5.0 loaded. Type /ldb to open settings.|r")
