"""
eUparjan Website Monitor
- Runs an HTTP server so Render keeps the container alive
- Self-pings every PING_INTERVAL seconds to prevent free-tier spin-down
- Persists state to disk so restarts don't cause missed updates
- Checks the target site every CHECK_INTERVAL seconds
- Sends all alerts to Telegram with retries
"""

import requests
import time
import urllib3
import hashlib
import re
import os
import json
import logging
import sys
import threading
from bs4 import BeautifulSoup
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TARGET_URL     = os.environ.get("TARGET_URL", "https://mpeuparjan.mp.gov.in/mpeuparjan25/Home.aspx")
BOT_TOKEN      = os.environ["BOT_TOKEN"]
CHAT_ID        = os.environ["CHAT_ID"]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))   # check every 60s
PING_INTERVAL  = int(os.environ.get("PING_INTERVAL_SECONDS",  "540"))  # self-ping every 9 min (< 10 min spin-down)
PORT           = int(os.environ.get("PORT", "8080"))
STATE_FILE     = "/tmp/euparjan_state.json"   # persists across soft restarts

RENDER_URL     = os.environ.get("RENDER_EXTERNAL_URL", "")  # auto-set by Render

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str, retries: int = 5) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(
                url,
                json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=15,
            )
            if r.status_code == 200:
                log.info("✉️  Telegram sent.")
                return True
            log.warning("Telegram HTTP %d (attempt %d): %s", r.status_code, attempt, r.text[:200])
        except requests.RequestException as e:
            log.warning("Telegram network error (attempt %d): %s", attempt, e)
        time.sleep(min(2 ** attempt, 30))   # backoff capped at 30s
    log.error("❌ Telegram failed after %d attempts.", retries)
    return False

# ─── STATE PERSISTENCE ─────────────────────────────────────────────────────────
def _make_serialisable(state: dict) -> dict:
    """Convert sets → lists so JSON can serialise."""
    return {k: (sorted(v) if isinstance(v, set) else v) for k, v in state.items()}

def _restore_sets(state: dict) -> dict:
    """Convert lists back → sets after JSON load."""
    set_keys = {"slots", "crops"}
    return {k: (set(v) if k in set_keys else v) for k, v in state.items()}

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_make_serialisable(state), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Could not save state: %s", e)

def load_state() -> dict | None:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        state = _restore_sets(raw)
        log.info("📂 Loaded persisted state (hash=%s).", state.get("hash", "?"))
        return state
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("Could not load state: %s", e)
        return None

# ─── SCRAPING ──────────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "hi-IN,hi;q=0.9,en-US;q=0.8",
})

def fetch_page() -> tuple[str, BeautifulSoup]:
    r = SESSION.get(TARGET_URL, timeout=20, verify=False)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    return r.text, soup

def page_hash(html: str) -> str:
    return hashlib.md5(html.encode()).hexdigest()

def extract_notice(soup: BeautifulSoup) -> str:
    text = soup.get_text("\n")
    if "आवश्यक सूचना" in text:
        idx = text.find("आवश्यक सूचना")
        return text[idx: idx + 800].strip()
    return ""

def extract_slots(soup: BeautifulSoup) -> list[str]:
    slots = []
    for ul in soup.find_all("ul"):
        for li in ul.find_all("li", recursive=False):
            a = li.find("a")
            if a and "किसान स्लॉट बुकिंग" in a.get_text(strip=True):
                slots.append(a.get_text(strip=True))
    return slots

def extract_crops(slot_list: list[str]) -> set[str]:
    crops: set[str] = set()
    for slot in slot_list:
        m = re.search(r'\((.*?)\)', slot)
        if m:
            for item in m.group(1).split(","):
                c = item.strip()
                if c:
                    crops.add(c)
    return crops

def get_state() -> dict:
    html, soup = fetch_page()
    slot_list  = extract_slots(soup)
    full_text  = soup.get_text(" ")
    return {
        "hash":         page_hash(html),
        "notice":       extract_notice(soup),
        "slots":        set(slot_list),
        "crops":        extract_crops(slot_list),
        "gehu_present": "गेहूं" in full_text,
        "checked_at":   datetime.now().isoformat(),
    }

# ─── DIFF & NOTIFY ─────────────────────────────────────────────────────────────
def check_and_notify(prev: dict, curr: dict) -> int:
    """Compare states, fire Telegram messages. Returns number of alerts sent."""
    ts      = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    alerts  = 0

    # 1. Raw page change
    if curr["hash"] != prev["hash"]:
        send_telegram(f"⚠️ <b>वेबसाइट में बदलाव हुआ है</b>\n🕐 {ts}")
        alerts += 1

    # 2. Notice updated
    if curr["notice"] and curr["notice"] != prev["notice"]:
        send_telegram(
            f"📢 <b>आवश्यक सूचना अपडेट:</b>\n\n"
            f"{curr['notice'][:3000]}\n\n🕐 {ts}"
        )
        alerts += 1

    # 3. New slots added
    for s in sorted(curr["slots"] - prev["slots"]):
        send_telegram(f"🚨 <b>नया स्लॉट जोड़ा गया:</b>\n\n{s}\n\n🕐 {ts}")
        alerts += 1

    # 4. Slots removed (might mean booking closed — still worth knowing)
    for s in sorted(prev["slots"] - curr["slots"]):
        send_telegram(f"🔕 <b>स्लॉट हटाया गया:</b>\n\n{s}\n\n🕐 {ts}")
        alerts += 1

    # 5. New crops
    for crop in sorted(curr["crops"] - prev["crops"]):
        send_telegram(f"🌾 <b>नया crop जोड़ा गया:</b> {crop}\n🕐 {ts}")
        alerts += 1

    # 6. गेहूं appeared anywhere on the page
    if curr["gehu_present"] and not prev["gehu_present"]:
        send_telegram(
            f"🚨🌾 <b>ALERT: गेहूं वेबसाइट पर पाया गया!</b>\n"
            f"तुरंत स्लॉट बुक करें!\n🕐 {ts}"
        )
        alerts += 1

    return alerts

# ─── HEALTH-CHECK + STATUS SERVER ─────────────────────────────────────────────
# A tiny HTTP server on $PORT.
# GET /         → 200 OK (Render health check)
# GET /status   → JSON snapshot of last known state
_last_state: dict = {}

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            body = json.dumps(_make_serialisable(_last_state), ensure_ascii=False, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"OK - eUparjan Monitor running"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass  # silence access logs

def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("🌐 Health server on port %d", PORT)

# ─── SELF-PING (prevents free-tier spin-down) ─────────────────────────────────
def self_ping_loop():
    """Ping own health endpoint every PING_INTERVAL seconds."""
    if not RENDER_URL:
        log.warning("RENDER_EXTERNAL_URL not set — self-ping disabled.")
        return
    ping_url = RENDER_URL.rstrip("/") + "/"
    while True:
        time.sleep(PING_INTERVAL)
        try:
            r = requests.get(ping_url, timeout=10)
            log.info("🏓 Self-ping %s → %d", ping_url, r.status_code)
        except Exception as e:
            log.warning("Self-ping failed: %s", e)

def start_self_ping():
    t = threading.Thread(target=self_ping_loop, daemon=True)
    t.start()

# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    global _last_state

    start_health_server()
    start_self_ping()

    log.info("🚀 eUparjan Monitor starting — interval=%ds", CHECK_INTERVAL)

    # ── Load persisted state (survives container restarts) ──
    state = load_state()

    if state is None:
        # Fresh start — capture baseline
        send_telegram(
            "✅ <b>eUparjan Monitor चालू हुआ</b>\n\n"
            f"🌐 <code>{TARGET_URL}</code>\n"
            f"⏱ Check interval: {CHECK_INTERVAL}s\n"
            f"🏓 Self-ping interval: {PING_INTERVAL}s\n"
            f"🕐 {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
        )
        while state is None:
            try:
                state = get_state()
                save_state(state)
                log.info("✅ Initial state captured. hash=%s", state["hash"])
            except Exception as e:
                log.error("Initial fetch failed: %s — retry in 30s", e)
                time.sleep(30)
    else:
        # Resumed — compare immediately so nothing is missed during downtime
        send_telegram(
            "🔄 <b>eUparjan Monitor resumed (restart)</b>\n"
            f"🕐 {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}\n"
            "Checking for missed updates…"
        )
        try:
            fresh = get_state()
            alerts = check_and_notify(state, fresh)
            if alerts == 0:
                send_telegram("✅ Restart check complete — कोई बदलाव नहीं मिला।")
            state = fresh
            save_state(state)
        except Exception as e:
            log.error("Restart diff check failed: %s", e)

    _last_state = state

    # ── Main loop ──────────────────────────────────────────
    consecutive_errors = 0
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            new_state = get_state()
            alerts    = check_and_notify(state, new_state)
            state     = new_state
            _last_state = state
            save_state(state)
            consecutive_errors = 0
            log.info(
                "✔ Check OK | hash=%s | alerts=%d | slots=%d | crops=%s | gehu=%s",
                state["hash"][:8], alerts,
                len(state["slots"]),
                state["crops"] or "none",
                state["gehu_present"],
            )
        except Exception as e:
            consecutive_errors += 1
            log.error("Check error #%d: %s", consecutive_errors, e)
            # Alert on Telegram only every 3 consecutive failures (avoid spam)
            if consecutive_errors % 3 == 1:
                send_telegram(
                    f"❌ <b>Monitor Error (#{consecutive_errors}):</b>\n"
                    f"<code>{str(e)[:400]}</code>\n"
                    f"🕐 {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}"
                )
            # Back off up to 5 minutes on repeated failures
            time.sleep(min(60 * consecutive_errors, 300))

if __name__ == "__main__":
    main()
