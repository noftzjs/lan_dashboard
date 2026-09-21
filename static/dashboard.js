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
    twitch: `<svg viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="#9146FF"/><rect x="8" y="6" width="2.6" height="10" fill="#fff"/><rect x="13.4" y="6" width="2.6" height="10" fill="#fff"/></svg>`,
    youtube: `<svg viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="#FF0000"/><path d="M9.5 7.5l7 4.5-7 4.5v-9z" fill="#fff"/></svg>`,
    kick: `<svg viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="#53FC18"/><text x="12" y="16.5" font-size="12" font-weight="800" text-anchor="middle" fill="#000">K</text></svg>`,
    other: `<svg viewBox="0 0 24 24"><rect width="24" height="24" rx="6" style="fill:var(--border-color)"/><path d="M9.5 7.5l7 4.5-7 4.5v-9z" fill="#fff"/></svg>`,
};

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
const TAGS_MAX_COUNT = 10;

// Persists a stream link / tag edit. tagsCsv is the raw comma-separated
// text as typed; splitting/trimming happens here so both pages agree on
// the rules. Returns the server's response body ({status, state}).
async function postRosterMeta(name, streamUrl, tagsCsv) {
    const tags = (tagsCsv ?? "").split(",")
        .map(t => t.trim().slice(0, TAG_MAX_LENGTH))
        .filter(Boolean)
        .slice(0, TAGS_MAX_COUNT);
    const response = await fetch(`${API_BASE}/api/roster/${encodeURIComponent(name)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stream_url: streamUrl ?? "", tags }),
    });
    const body = await response.json();
    if (body.state) {
        playerData[name] = stampReceived(body.state); // Reflect immediately, don't wait on the broadcast
    }
    return body;
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
