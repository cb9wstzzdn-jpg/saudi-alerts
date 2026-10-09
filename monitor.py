"""
Saudi Arabia alerts for monitor-the-situation.com -> ntfy push notifications.

Runs every few minutes (GitHub Actions). Reads the site's public events feed,
picks out events located in Saudi Arabia, and sends one ntfy notification per
new event with a generated map image attached.

All notifications use NORMAL priority (ntfy default, 3) so they never break
through iPhone Focus modes. Severity is shown only in the text and emoji.
"""

import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

FEED_URL = "https://monitor-the-situation.com/api/events"
EVENT_URL = "https://monitor-the-situation.com/middle-east/event-{short_id}"
REGION_URL = "https://monitor-the-situation.com/middle-east"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"  # print instead of sending
FEED_FILE = os.environ.get("FEED_FILE")  # optional local JSON file, for offline testing
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
USER_AGENT = "personal-saudi-alert-monitor/1.0 (polls every 5 min)"

MAX_PER_RUN = 8              # more than this at once -> send a summary instead
FAIL_ALERT_AFTER = 6         # consecutive failed checks (~30 min) before warning you
FORGET_AFTER_DAYS = 14       # drop old ids from the state file

SEVERITY_EMOJI = {1: "🟢", 2: "🟡", 3: "🟠", 4: "🔴", 5: "🟣"}
SEVERITY_RGB = {1: (46, 204, 113), 2: (241, 196, 15), 3: (230, 126, 34),
                4: (231, 76, 60), 5: (155, 89, 182)}

# Simplified Saudi Arabia border (lng, lat). Only used when the feed's country
# field is missing/"Unknown", and to draw the map image.
SAUDI_POLYGON = [
    (34.95, 29.36), (36.07, 29.19), (36.50, 29.50), (37.98, 30.00), (37.00, 31.50),
    (39.20, 32.15), (40.40, 31.90), (42.00, 31.10), (44.70, 29.20), (46.55, 29.10),
    (47.40, 28.50), (48.40, 28.55), (48.80, 27.60), (49.60, 26.90), (50.10, 26.20),
    (50.20, 25.60), (50.80, 24.75), (51.60, 24.25), (52.60, 22.94), (55.20, 22.70),
    (55.67, 22.00), (55.00, 20.00), (52.00, 19.00), (49.10, 18.60), (48.20, 18.20),
    (47.00, 16.95), (46.30, 17.25), (44.50, 17.40), (43.40, 17.50), (43.20, 16.70),
    (42.80, 16.40), (42.60, 16.80), (41.70, 17.90), (40.90, 19.50), (39.60, 20.90),
    (39.10, 21.70), (38.50, 23.60), (37.50, 24.60), (36.60, 25.80), (35.60, 27.30),
    (34.70, 28.10),
]
CITIES = [("Riyadh", 46.72, 24.71), ("Jeddah", 39.19, 21.49), ("Mecca", 39.83, 21.42),
          ("Medina", 39.61, 24.47), ("Dammam", 50.10, 26.43), ("Tabuk", 36.57, 28.38),
          ("Abha", 42.51, 18.22), ("Hail", 41.69, 27.52)]


# ---------------------------------------------------------------- helpers

def log(*a):
    print(*a, flush=True)


def point_in_polygon(lng, lat, poly=SAUDI_POLYGON):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def is_saudi(ev):
    country = (ev.get("country") or "").strip().lower()
    if "saudi" in country:
        return True
    if country in ("", "unknown", "none", "null"):
        try:
            return point_in_polygon(float(ev["lng"]), float(ev["lat"]))
        except (KeyError, TypeError, ValueError):
            return False
    return False


def confidence_label(conf):
    # Bands from the site's methodology page.
    try:
        c = int(conf)
    except (TypeError, ValueError):
        return "❓ Unknown", None
    if c < 31:
        return "❓ Unconfirmed", c
    if c <= 55:
        return "🔸 Developing", c
    return "✅ Verified", c


def severity(ev):
    try:
        return max(1, min(5, int(ev.get("severity") or 1)))
    except (TypeError, ValueError):
        return 1


def event_link(ev):
    return EVENT_URL.format(short_id=str(ev["id"]).replace("-", "")[:8])


def build_text(ev, upgraded=False, test=False):
    sev = severity(ev)
    label, conf = confidence_label(ev.get("confidence"))
    conf_txt = f"{label} ({conf})" if conf is not None else label
    title = f"{SEVERITY_EMOJI[sev]} S{sev} · {conf_txt}"
    if upgraded:
        title = "⬆️ " + title
    if test:
        title = "[TEST] " + title
    loc = ev.get("location_name") or "Saudi Arabia"
    cat = (ev.get("category") or "").capitalize()
    message = f"{ev.get('title', 'Untitled event')}\n📍 {loc}" + (f" · {cat}" if cat else "")
    return title, message


# ---------------------------------------------------------------- image

def make_image(ev):
    """Return PNG bytes: Saudi map with a pin + severity/confidence/title card."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), (16, 21, 27))
    d = ImageDraw.Draw(img)

    def font(size, bold=False):
        names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
        for n in names:
            for base in ("/usr/share/fonts/truetype/dejavu/", ""):
                try:
                    return ImageFont.truetype(base + n, size)
                except OSError:
                    pass
        return ImageFont.load_default(size=size)

    # --- map panel (left)
    mx0, my0, mx1, my1 = 30, 30, 640, 600
    d.rounded_rectangle((mx0, my0, mx1, my1), 18, fill=(22, 30, 39))
    lng0, lng1, lat0, lat1 = 33.5, 57.0, 15.0, 33.0

    def xy(lng, lat):
        x = mx0 + 25 + (lng - lng0) / (lng1 - lng0) * (mx1 - mx0 - 50)
        y = my1 - 25 - (lat - lat0) / (lat1 - lat0) * (my1 - my0 - 50)
        return x, y

    for g in range(36, 57, 4):
        d.line([xy(g, lat0), xy(g, lat1)], fill=(30, 40, 52))
    for g in range(16, 33, 4):
        d.line([xy(lng0, g), xy(lng1, g)], fill=(30, 40, 52))
    d.polygon([xy(*p) for p in SAUDI_POLYGON], fill=(38, 52, 66), outline=(90, 120, 145))
    small = font(15)
    d.text(xy(37.3, 20.2), "Red Sea", font=small, fill=(70, 110, 140))
    d.text(xy(51.0, 27.6), "Persian Gulf", font=small, fill=(70, 110, 140))
    for name, lng, lat in CITIES:
        x, y = xy(lng, lat)
        d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(150, 165, 180))
        if name == "Jeddah":  # sits right next to Mecca; put its label on the left
            d.text((x - 6, y - 9), name, font=small, fill=(150, 165, 180), anchor="ra")
        else:
            d.text((x + 6, y - 9), name, font=small, fill=(150, 165, 180))

    sev = severity(ev)
    col = SEVERITY_RGB[sev]
    try:
        px, py = xy(float(ev["lng"]), float(ev["lat"]))
        px = max(mx0 + 12, min(mx1 - 12, px))
        py = max(my0 + 12, min(my1 - 12, py))
        for r, a in ((34, 60), (22, 120)):
            ring = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse((px - r, py - r, px + r, py + r), fill=col + (a,))
            img.paste(ring, (0, 0), ring)
        d = ImageDraw.Draw(img)
        d.ellipse((px - 10, py - 10, px + 10, py + 10), fill=col, outline=(255, 255, 255), width=3)
    except (KeyError, TypeError, ValueError):
        pass

    # --- info card (right)
    x = 670
    d.rounded_rectangle((x, 40, x + 120, 100), 12, fill=col)
    d.text((x + 60, 70), f"S{sev}", font=font(36, True), fill=(16, 21, 27), anchor="mm")
    label, conf = confidence_label(ev.get("confidence"))
    label = label.split(" ", 1)[1]  # drop emoji (font has none)
    conf_col = {"Verified": (46, 204, 113), "Developing": (241, 196, 15)}.get(label, (160, 170, 180))
    ctext = f"{label} {conf}" if conf is not None else label
    cw = d.textlength(ctext, font=font(26, True)) + 36
    d.rounded_rectangle((x + 135, 40, x + 135 + cw, 100), 12, outline=conf_col, width=3)
    d.text((x + 135 + cw / 2, 70), ctext, font=font(26, True), fill=conf_col, anchor="mm")

    # wrapped title
    tf = font(34, True)
    words, lines, cur = str(ev.get("title", "")).split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=tf) <= W - x - 40:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > 5:
        lines = lines[:5]
        lines[-1] = lines[-1].rstrip(".") + "…"
    y = 135
    for ln in lines:
        d.text((x, y), ln, font=tf, fill=(235, 240, 245))
        y += 44

    meta = font(22)
    y += 16
    loc = ev.get("location_name") or "Saudi Arabia"
    d.text((x, y), loc, font=meta, fill=(180, 190, 200)); y += 32
    cat = (ev.get("category") or "").capitalize()
    when = ev.get("created_at") or ""
    d.text((x, y), " · ".join(p for p in (cat, f"{when} UTC" if when else "") if p),
           font=meta, fill=(130, 140, 150))
    d.text((x, 575), "monitor-the-situation.com · automated, may be inaccurate",
           font=font(16), fill=(90, 100, 110))

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- ntfy

def ntfy_send(title, message, click=None, image=None):
    """Send at default priority. With image -> attachment upload; else JSON."""
    if DRY_RUN or not NTFY_TOPIC:
        log(f"[dry-run] {title} | {message!r} | {click} | image={len(image) if image else 0}B")
        return True
    try:
        if image:
            params = {"title": title, "message": message, "filename": "event.png"}
            if click:
                params["click"] = click
            url = f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, data=image, method="PUT",
                                         headers={"User-Agent": USER_AGENT})
        else:
            body = {"topic": NTFY_TOPIC, "title": title, "message": message}
            if click:
                body["click"] = click
            req = urllib.request.Request(NTFY_SERVER, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        log("ntfy send failed:", e)
        if image:  # retry without the picture so you still get the alert
            return ntfy_send(title, message, click, None)
        return False


def notify_event(ev, upgraded=False, test=False):
    title, message = build_text(ev, upgraded, test)
    img = None
    try:
        img = make_image(ev)
    except Exception as e:  # noqa: BLE001
        log("image failed, sending text only:", e)
    return ntfy_send(title, message, event_link(ev), img)


# ---------------------------------------------------------------- state / feed

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"initialized": False, "events": {}, "fail_count": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")


def fetch_feed():
    if FEED_FILE:
        with open(FEED_FILE) as f:
            data = json.load(f)
    else:
        req = urllib.request.Request(FEED_URL, headers={"User-Agent": USER_AGENT,
                                                        "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    if isinstance(data, dict):
        data = data.get("events") or data.get("data") or []
    if not isinstance(data, list):
        raise ValueError("unexpected feed format")
    return data


# ---------------------------------------------------------------- main

def main():
    if not NTFY_TOPIC and not DRY_RUN:
        log("NTFY_TOPIC secret is not set."); sys.exit(1)

    state = load_state()
    now = datetime.now(timezone.utc)
    state["last_check_day"] = now.strftime("%Y-%m-%d")  # keeps the repo 'active'

    try:
        events = fetch_feed()
    except Exception as e:  # noqa: BLE001
        state["fail_count"] = state.get("fail_count", 0) + 1
        log(f"Could not read feed ({state['fail_count']} in a row):", e)
        if state["fail_count"] == FAIL_ALERT_AFTER:
            ntfy_send("⚙️ Saudi alerts: feed unreachable",
                      "The monitor hasn't been able to read the site for ~30 min. "
                      "It keeps trying and will tell you when it's back.", REGION_URL)
        save_state(state)
        if TEST_MODE:
            ntfy_send("[TEST] ⚙️ Saudi alerts", f"Test ran but the feed was unreachable: {e}")
        return

    if state.get("fail_count", 0) >= FAIL_ALERT_AFTER:
        ntfy_send("⚙️ Saudi alerts: back online", "The monitor can read the site again.", REGION_URL)
    state["fail_count"] = 0

    saudi = [e for e in events if e.get("id") and is_saudi(e)]
    saudi.sort(key=lambda e: e.get("created_at") or "")
    log(f"Feed: {len(events)} events, {len(saudi)} in Saudi Arabia")
    seen = state.setdefault("events", {})
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if TEST_MODE:
        sample = saudi[-1] if saudi else {
            "id": "00000000-test", "title": "Test notification — no Saudi events in the feed right now",
            "severity": 3, "confidence": 60, "lat": 24.71, "lng": 46.72,
            "location_name": "Riyadh, Saudi Arabia", "category": "test"}
        ok = notify_event(sample, test=True)
        log("Test notification sent" if ok else "Test notification FAILED")

    if not state.get("initialized"):
        # First run: remember what's already there instead of sending a flood.
        for e in saudi:
            seen[e["id"]] = {"sev": severity(e), "t": stamp}
        state["initialized"] = True
        ntfy_send("✅ Saudi alerts are running",
                  f"Watching monitor-the-situation.com every ~5 min. {len(saudi)} current "
                  "Saudi events were skipped; you'll get alerts for new ones from now on.",
                  REGION_URL)
    else:
        new, upgrades = [], []
        for e in saudi:
            prev = seen.get(e["id"])
            if prev is None:
                new.append(e)
            elif severity(e) > prev.get("sev", 0):
                upgrades.append(e)
        todo = [(e, False) for e in new] + [(e, True) for e in upgrades]
        if len(todo) > MAX_PER_RUN:
            extra = todo[:-MAX_PER_RUN]
            todo = todo[-MAX_PER_RUN:]
            ntfy_send(f"Saudi alerts: +{len(extra)} more events",
                      f"{len(extra)} older new events were bundled to avoid spamming you.",
                      REGION_URL)
        for e, upgraded in todo:
            if notify_event(e, upgraded=upgraded):
                log("sent:", e.get("title"))
            time.sleep(1)
        for e in saudi:
            if e["id"] not in seen or severity(e) > seen[e["id"]].get("sev", 0):
                seen[e["id"]] = {"sev": severity(e), "t": stamp}

    cutoff = (now - timedelta(days=FORGET_AFTER_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    current = {e["id"] for e in saudi}
    for k in [k for k, v in seen.items() if v.get("t", "") < cutoff and k not in current]:
        del seen[k]
    save_state(state)


if __name__ == "__main__":
    main()
