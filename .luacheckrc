-- luacheck config for the LanDashboard addon (tools/luacheck.exe, run by the
-- edit hook and by hand: tools/luacheck.exe LanDashboard). WoW addons run on
-- Lua 5.1 and talk to the game through globals, so those are declared here
-- rather than reported on every edit. Add a name when the addon starts using
-- a new API; a typo in an API name is exactly what this is meant to catch.
std = "lua51"

-- Long lines are a style choice in this file, and `self` on frame handlers
-- is the WoW convention even when unused.
max_line_length = false
ignore = { "212/self" }

-- The addon's own globals: SavedVariables from the .toc, the slash command
-- registration, and the two functions the settings panel reaches by name.
globals = {
    "LanDashboardDB",
    "LDB_WEIGHT_QUEST", "LDB_WEIGHT_LEVEL", "LDB_FLUSH_THRESHOLD", "LDB_ESCALATE_THRESHOLD",
    "LDB_AUTO_SYNC_ON_LOGIN", "LDB_VERBOSE",
    "LDB_ApplySavedDefaults", "LDB_RegisterSettingsPanel",
    "SLASH_LANDASHBOARD1", "SlashCmdList",
}

-- The WoW API the addon reads. Grouped the way the game documents them.
read_globals = {
    -- frames and UI
    "CreateFrame", "UIParent", "Settings", "ReloadUI", "InCombatLockdown",
    "ChatFrame_AddMessageEventFilter",
    -- unit and player info
    "UnitName", "UnitNameUnmodified", "UnitFullName", "UnitPVPName", "GetUnitName", "UnitGUID",
    "UnitLevel", "UnitXP", "UnitXPMax", "UnitClass", "UnitFactionGroup", "UnitIsAFK",
    "UnitAffectingCombat", "GetMoney", "GetAverageItemLevel", "GetGuildInfo",
    "GetRealmName", "GetNormalizedRealmName", "GetBuildInfo",
    -- zones, time, played
    "GetZoneText", "GetServerTime", "RequestTimePlayed", "TIME_PLAYED_TOTAL", "TIME_PLAYED_LEVEL",
    -- professions and skills
    "GetProfessions", "GetProfessionInfo", "GetNumSkillLines", "GetSkillLineInfo", "ExpandSkillHeader",
    -- achievements and statistics
    "GetStatistic", "GetStatisticsCategoryList", "GetAchievementInfo", "GetCategoryNumAchievements",
    -- namespaces
    "C_Map", "C_Timer", "C_PlayerInfo",
    -- Lua standard functions WoW exposes outside the 5.1 std set
    "time", "date",
}
