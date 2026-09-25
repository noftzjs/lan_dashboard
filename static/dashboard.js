// Shared between index.html (spectator dashboard) and roster.html (operator
// roster manager): server address resolution, the websocket connection and
// live player-state cache, escaping helpers, and the focus-preserving
// re-render helpers. Page-specific rendering stays inline in each page.

// When this page is served BY the dashboard server itself (browsing to
// http://<host>:5000/), window.location.host is already correct and no
// setup is needed. If a page was instead copied and opened directly
// (file://), fall back to a value remembered in this browser, prompting
// once if there isn't one yet.
function resolveServerAddress() {
    if (window.location.host) {
        return { host: window.location.host, protocol: window.location.protocol === "https:" ? "wss:" : "ws:" };
    }
    let stored = null;
    try { stored = localStorage.getItem("dashboardServerIp"); } catch (e) { /* private browsing, etc. */ }
    const address = stored || prompt("Dashboard server address (e.g. 192.168.1.50:5000):", "127.0.0.1:5000");
    try { localStorage.setItem("dashboardServerIp", address); } catch (e) { /* ignore */ }
    return { host: address, protocol: "ws:" };
}
const SERVER = resolveServerAddress();
const API_BASE = `${SERVER.protocol === "wss:" ? "https:" : "http:"}//${SERVER.host}`;

// Standard WoW class colors, keyed by the addon's non-localized class token.
const CLASS_COLORS = {
    WARRIOR: "#C79C6E", PALADIN: "#F58CBA", HUNTER: "#ABD473", ROGUE: "#FFF569",
    PRIEST: "#FFFFFF", SHAMAN: "#0070DE", MAGE: "#69CCF0", WARLOCK: "#9482C9",
    DRUID: "#FF7D0A",
};

let playerData = {}; // Active local copy of current player states, keyed by name

// Set from the server's INIT message (STALE_AFTER_MINUTES there); 15 min
// until it arrives so a page never renders with "nothing is stale."
let staleAfterSeconds = 15 * 60;

// The server sends each player's age (idle_seconds) rather than a timestamp,
// since the browser's clock and the server's can disagree. Age at receipt
// plus however long this tab has held it is the age now — that's all the
// comparison needs, and it keeps working between websocket messages.
function stampReceived(state) {
    state._receivedAt = Date.now();
    return state;
}

// A character the server has never heard an event from (idle_seconds null)
// counts as stale — nothing says they're actually playing. A field that's
// missing entirely means the server predates this feature (the page is
// served fresh from disk, so it can briefly run ahead of a server that
// hasn't been restarted) — don't hide everyone in that case.
function isStale(player) {
    if (player.idle_seconds === undefined) return false;
    if (player.idle_seconds === null) return true;
    const idleNow = player.idle_seconds + (Date.now() - player._receivedAt) / 1000;
    return idleNow > staleAfterSeconds;
}

// Never trust text from the game, the network, or another operator's edits
// when placing it into HTML — escape it first.
function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
}

// Only allow http(s) links through to href attributes, so a malicious
// stream_url can't smuggle in a javascript: URI.
function safeUrl(value) {
    if (!value) return null;
    try {
        const url = new URL(value);
        if (url.protocol === "http:" || url.protocol === "https:") return url.href;
    } catch (e) { /* not a valid absolute URL */ }
    return null;
}

// Which platform a stream URL points to, purely from hostname — used only
// to pick an icon. This never checks or implies whether the stream is
// actually live; there's no way to know that without polling each
// platform's API, so the badge is deliberately just "here's their channel."
function detectStreamPlatform(url) {
    try {
        const host = new URL(url).hostname.toLowerCase().replace(/^www\./, "");
        if (host.endsWith("twitch.tv")) return "twitch";
        if (host.endsWith("youtube.com") || host.endsWith("youtu.be")) return "youtube";
        if (host.endsWith("kick.com")) return "kick";
    } catch (e) { /* not a valid URL */ }
    return "other";
}

// Simplified, not pixel-perfect brand marks — good enough to recognize at
// a glance in a 20px badge without pulling in external logo assets (this
// project avoids runtime CDN dependencies for LAN-reliability reasons).
const STREAM_PLATFORM_ICONS = {
    // All four are drawn on the same 24x24 grid and optically centred on
    // (12, 12) — the earlier set was centred a unit off, which showed as a
    // lopsided glyph once the badge sat next to a 30px name in Big screen.
    // A play triangle's centroid is a third of its width from the base, so
    // its points are chosen to put that centroid on 12, not its bounding box.
    twitch: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6" fill="#9146FF"/><rect x="8.2" y="7" width="2.6" height="10" rx="1" fill="#fff"/><rect x="13.2" y="7" width="2.6" height="10" rx="1" fill="#fff"/></svg>`,
    youtube: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6" fill="#FF0000"/><path d="M9.8 7.4 L16.8 12 L9.8 16.6 Z" fill="#fff"/></svg>`,
    kick: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6" fill="#53FC18"/><path d="M7.4 6.6h3.1v3.6l3.1-3.6h3.9l-4.2 5.4 4.2 5.4h-3.9l-3.1-3.7v3.7H7.4z" fill="#000"/></svg>`,
    other: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6" fill="#3b3b46"/><path d="M9.8 7.4 L16.8 12 L9.8 16.6 Z" fill="#e8e8ef"/></svg>`,
};

// One badge per link. Shared by both dashboard layouts so the markup, the
// safe-URL check and the platform detection can't drift apart between them.
const STREAM_PLATFORM_LABELS = { twitch: "Twitch", youtube: "YouTube", kick: "Kick", other: "their stream" };

function streamBadgesHtml(player) {
    const links = (player.stream_urls || []).map(safeUrl).filter(Boolean);
    if (!links.length) return "";
    const badges = links.map(href => {
        const platform = detectStreamPlatform(href);
        return `<a class="stream-badge" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer"
                   title="Watch on ${STREAM_PLATFORM_LABELS[platform]}">${STREAM_PLATFORM_ICONS[platform]}</a>`;
    }).join("");
    return `<span class="stream-badges">${badges}</span>`;
}

// Connects the shared websocket feed. onUpdate is called after playerData
// changes, so each page can re-render however it needs to.
function connectWebSocket(onUpdate) {
    const ws = new WebSocket(`${SERVER.protocol}//${SERVER.host}/ws/dashboard`);
    const statusDiv = document.getElementById("status");

    ws.onopen = () => {
        if (statusDiv) { statusDiv.textContent = "Live Connected"; statusDiv.className = "connected"; }
    };
    ws.onclose = () => {
        if (statusDiv) { statusDiv.textContent = "Disconnected (Reconnecting...)"; statusDiv.className = "disconnected"; }
        setTimeout(() => connectWebSocket(onUpdate), 3000); // Auto-reconnect if server bumps
    };
    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.event === "INIT") {
            playerData = msg.data;
            Object.values(playerData).forEach(stampReceived);
            if (msg.stale_after_seconds != null) staleAfterSeconds = msg.stale_after_seconds;
        } else if (msg.event === "PLAYER_UPDATE") {
            playerData[msg.player] = stampReceived(msg.state);
        }
        if (onUpdate) onUpdate();
    };
}

// Kept in sync with the server's own caps in main.py (TAG_MAX_LENGTH /
// TAGS_MAX_COUNT) — this is just the first line of defense so the UI gives
// immediate feedback; the server enforces its own limits regardless of
// what any client sends.
const TAG_MAX_LENGTH = 30;
const TAGS_MAX_COUNT = 3;
const STREAM_URLS_MAX_COUNT = 3;

// Persists a stream link / tag edit. tagsCsv is the raw comma-separated
// text as typed; splitting/trimming happens here so both pages agree on
// the rules. Returns the server's response body ({status, state}).
async function postRosterMeta(name, streamsCsv, tagsCsv) {
    const tags = (tagsCsv ?? "").split(",")
        .map(t => t.trim().slice(0, TAG_MAX_LENGTH))
        .filter(Boolean)
        .slice(0, TAGS_MAX_COUNT);
    // Comma-separated like tags. A comma is legal in a URL but effectively
    // never appears in a channel link, and one field per platform would mean
    // rebuilding the roster row every time a new platform shows up.
    const stream_urls = (streamsCsv ?? "").split(",")
        .map(u => u.trim())
        .filter(Boolean)
        .slice(0, STREAM_URLS_MAX_COUNT);
    const response = await fetch(`${API_BASE}/api/roster/${encodeURIComponent(name)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stream_urls, tags }),
    });
    const body = await response.json();
    if (body.state) {
        playerData[name] = stampReceived(body.state); // Reflect immediately, don't wait on the broadcast
    }
    return body;
}

// A max_xp of 0 is the game's way of saying "no next level" — the character
// is at the cap, so a percentage of nothing is meaningless and the bar is
// full. Shared so every view words it the same way.
function isMaxLevel(player) {
    return player.max_xp === 0;
}

function progressText(player) {
    if (isMaxLevel(player)) return "Level cap reached";
    return `${player.pct}% · ${player.current_xp.toLocaleString()} / ${player.max_xp.toLocaleString()}`;
}

// Copper is what the game counts in and what the server stores, so the
// conversion to g/s/c lives here, at the point of display.
function goldParts(copper) {
    return {
        gold: Math.floor(copper / 10000),
        silver: Math.floor((copper % 10000) / 100),
        copper: copper % 100,
    };
}

// Money the way the game writes it: each unit in its own coin colour, run
// together as 12g34s56c. Leading zero units are dropped exactly as the
// in-game money frame drops them, so 87 copper reads "87c", not "0g0s87c".
//
// NOTE: this returns HTML, so it belongs in innerHTML and never in an
// attribute. exactGold() is the plain-text form for title tooltips.
function formatGold(copper) {
    if (copper === null || copper === undefined) return "\u2014";
    const parts = goldParts(copper);
    const out = [];
    if (parts.gold > 0) {
        out.push(`<span class="coin-g">${parts.gold.toLocaleString()}g</span>`);
    }
    if (parts.gold > 0 || parts.silver > 0) {
        out.push(`<span class="coin-s">${parts.silver}s</span>`);
    }
    out.push(`<span class="coin-c">${parts.copper}c</span>`);
    return out.join("");
}

// A withheld field and a never-reported one both arrive as null, so the
// difference lives in private_fields. They must not render alike: an em dash
// reads as "no data yet", which would make a deliberate choice look like a
// broken watcher -- and would have someone chasing a bug that isn't there.
function isWithheld(player, field) {
    return Array.isArray(player.private_fields) && player.private_fields.includes(field);
}

const WITHHELD_GOLD = "This player chose not to share their gold";

// Returns HTML (like formatGold, which it wraps), so innerHTML only.
function goldHtml(player) {
    if (isWithheld(player, "gold")) {
        return `<span class="withheld">private</span>`;
    }
    return formatGold(player.gold);
}

// Plain text, for title attributes.
function goldTitle(player) {
    return isWithheld(player, "gold") ? WITHHELD_GOLD : exactGold(player.gold);
}

// True when there is something to show at all -- either a value or a
// deliberate refusal. Used by the layouts that omit gold entirely when absent.
function hasGoldToShow(player) {
    return player.gold != null || isWithheld(player, "gold");
}

function exactGold(copper) {
    if (copper === null || copper === undefined) return "No gold recorded yet";
    const parts = goldParts(copper);
    return `${parts.gold.toLocaleString()}g ${parts.silver}s ${parts.copper}c`
        + ` (${copper.toLocaleString()} copper)`;
}

// Item level is a float (12.5 at level 18), so it keeps one decimal -- the
// half point is real and rounding it away would flatten characters that are
// genuinely a tier apart at low level.
function formatItemLevel(player) {
    if (isWithheld(player, "item_level")) return "private";
    if (player.item_level === null || player.item_level === undefined) return "\u2014";
    return player.item_level.toFixed(1);
}

// Professions arrive in slot order with empty slots dropped. The slot itself
// isn't carried, so primaries and secondaries aren't distinguishable here --
// they're shown as one list rather than guessing from position.
function professionsText(player) {
    const list = player.professions || [];
    if (!list.length) return "";
    return list.map(p => `${p.name} ${p.rank}/${p.max_rank}`).join(", ");
}

function professionsHtml(player) {
    const list = player.professions || [];
    if (!list.length) return `<span class="csub">\u2014</span>`;
    return list
        .map(p => `<span class="prof">${escapeHtml(p.name)}<b>${p.rank}</b></span>`)
        .join("");
}

// Playtime can run to days over a LAN weekend, so the unit shifts rather
// than printing an unreadable hour count.
function formatPlayed(seconds) {
    if (!seconds) return "\u2014";
    const hours = Math.floor(seconds / 3600);
    if (hours < 1) return `${Math.floor(seconds / 60)}m`;
    if (hours < 24) {
        const minutes = Math.floor((seconds % 3600) / 60);
        return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
    }
    return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}


// Hides nav entries the current visitor cannot open, so a spectator is not
// offered two pages that will only bounce them to a login form.
//
// Presentation only. The server decides access; this just stops the UI
// advertising doors that are locked.
async function trimNavForRole() {
    const needs = { "/analytics": ["viewer", "operator"], "/roster": ["operator"] };
    let role = null;
    try {
        role = (await (await fetch("/api/whoami")).json()).role;
    } catch (e) {
        // Offline or blocked: leave the nav alone rather than hiding things
        // from someone who may well be entitled to them.
        return;
    }
    document.querySelectorAll(".header-right .nav-link[href]").forEach(link => {
        const allowed = needs[new URL(link.href, location.origin).pathname];
        if (allowed && !allowed.includes(role)) link.hidden = true;
    });
    const signOut = document.getElementById("signOut");
    if (signOut && role) signOut.hidden = false;
}

// Live updates arrive continuously (a websocket message per game event).
// Rebuilding a list with container.innerHTML = html destroys and recreates
// every element on every update, which is what caused the focus-loss and
// hover-flicker bugs this replaced: any DOM node the browser is tracking
// state for (focus, :hover, an in-progress CSS transition) gets thrown
// away and rebuilt from scratch even when that specific player's data
// didn't change.
//
// morphdom (vendored in static/vendor/, loaded before this file) instead
// diffs the new HTML against the live DOM and patches only what actually
// changed, reusing existing nodes wherever possible. This only works
// correctly across reorders (sorting, insertions) if each item has a
// stable, unique id — that's what playerElementId is for; give every
// card/row an id built from it, e.g. id="${playerElementId('card', name)}".
function playerElementId(prefix, name) {
    return `${prefix}-${encodeURIComponent(name)}`;
}

// Morphs container's children to match `html`, without touching the
// container element itself. Building a real element of the same tag
// (rather than parsing the string as a generic fragment) matters for
// containers like <tbody> — parsing "<tr>...</tr>" outside of a table
// context gets silently mangled by the HTML parser's foster-parenting
// rules, whereas assigning it to an actual <tbody>'s innerHTML parses it
// correctly.
function morphChildren(container, html) {
    const scratch = document.createElement(container.tagName);
    scratch.innerHTML = html;
    morphdom(container, scratch, { childrenOnly: true });
}
