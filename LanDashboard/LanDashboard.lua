local LanFrame = CreateFrame("Frame")

-- Register the updated set of event hooks
LanFrame:RegisterEvent("PLAYER_XP_UPDATE")
LanFrame:RegisterEvent("QUEST_TURNED_IN")
LanFrame:RegisterEvent("PLAYER_LEVEL_UP")
LanFrame:RegisterEvent("ZONE_CHANGED_NEW_AREA") -- Fires on dungeon transitions / loading screens
LanFrame:RegisterEvent("ZONE_CHANGED")          -- Fires when stepping over subzone thresholds
LanFrame:RegisterEvent("PLAYER_ENTERING_WORLD") -- Fires on login/ui reload/zone transitions
LanFrame:RegisterEvent("PLAYER_GUILD_UPDATE")   -- Fires when the player joins/leaves/is kicked from a guild
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
-- DOES flush on /reload, so the real event data lives here now, not just
-- in the chat frame. print() below is kept purely for the player's own
-- visual confirmation that things are firing — the watcher (once built)
-- will read this table, not the chat log, for PROFILE/ZONE/XP/QUEST data.
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

local function emit(line)
    print("[DASHBOARD] " .. line)
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
    emit(string.format("%s,PROFILE,%s,%s,%s", playerName, faction, classToken, guildName))
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
            emit(string.format("%s,ZONE,%s", playerName, currentZone))

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
            emit(string.format("%s,ZONE,%s", playerName, currentZone))
        end

    -- 3. CORE XP & LEVEL LOGGING HANDLERS
    elseif event == "PLAYER_XP_UPDATE" then
        local currentXP = UnitXP("player")
        local maxXP = UnitXPMax("player")
        local currentLevel = UnitLevel("player")

        if currentXP == 0 and currentLevel == 1 then return end

        emit(string.format("%s,XP,%d,%d,%d", playerName, currentLevel, currentXP, maxXP))

    elseif event == "QUEST_TURNED_IN" then
        local questID, xpReward = ...
        emit(string.format("%s,QUEST,%d,%d", playerName, questID, xpReward or 0))
        addScore(LDB_WEIGHT_QUEST, "quest")
        print(string.format("|cffe8a33d[LAN Dashboard] Score: %d|r", LDB_score))

    elseif event == "PLAYER_LEVEL_UP" then
        local newLevel = ...
        local maxXP = UnitXPMax("player")
        emit(string.format("%s,XP,%d,0,%d", playerName, newLevel, maxXP))
        addScore(LDB_WEIGHT_LEVEL, "level up")

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

SLASH_LANDASHBOARD1 = "/ldb"
SlashCmdList["LANDASHBOARD"] = function(msg)
    if msg == "sync" then
        syncButton:Show()
        print("|cffe8a33d[LAN Dashboard] Sync button shown — click it (top of screen) to test.|r")
        return
    end
    if msg == "forget" then
        LanDashboardDB.known = {}
        print("|cffe8a33d[LAN Dashboard] Forgot all known characters — the next login of each will prompt to sync again.|r")
        return
    end
    print(string.format(
        "|cffe8a33d[LAN Dashboard] score=%d  flush=%d  escalate=%d  queued=%d  |  weights: quest=%d level=%d  |  auto-sync on login: %s|r",
        LDB_score, LDB_FLUSH_THRESHOLD, LDB_ESCALATE_THRESHOLD, LanDashboardDB.nextId - 1,
        LDB_WEIGHT_QUEST, LDB_WEIGHT_LEVEL, tostring(LDB_AUTO_SYNC_ON_LOGIN)
    ))
end

print("|cffcd7f32LAN Dashboard v2.9.1 Initialized! Type /ldb to check reload-trigger status, /ldb sync to test the sync button.|r")
