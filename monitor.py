"""
Gulf conflict alerts for monitor-the-situation.com -> ntfy push notifications.

Countries: Saudi Arabia, UAE, Qatar, Bahrain, Kuwait, Iran, Yemen (not Oman).
Conflict-related events only.
  * S3-S5: instant notification (one per event, with a map image).
  * S1-S2: collected into one digest per 8-hour shift
           (06:00, 14:00, 22:00 Riyadh time). No digest if nothing happened.

All notifications use NORMAL priority (ntfy default, 3) so they never break
through iPhone Focus modes. Severity is shown only in text/emoji.
"""

import io
import json
import os
import re
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
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
FEED_FILE = os.environ.get("FEED_FILE")            # local JSON for offline tests
NOW_OVERRIDE = os.environ.get("NOW")               # ISO time, for offline tests
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
USER_AGENT = "personal-gulf-alert-monitor/2.0 (polls every 5 min)"
STATE_VERSION = 2

INSTANT_MIN_SEVERITY = 3
DIGEST_HOURS_UTC = (3, 11, 19)   # = 06:00, 14:00, 22:00 Riyadh (UTC+3, no DST)
RIYADH = timezone(timedelta(hours=3))
MAX_PER_RUN = 8
FAIL_ALERT_AFTER = 6
FORGET_AFTER_DAYS = 14
NTFY_MAX_MESSAGE = 3800          # ntfy limit is 4096 bytes

SEVERITY_EMOJI = {1: "🟢", 2: "🟡", 3: "🟠", 4: "🔴", 5: "🟣"}
SEVERITY_RGB = {1: (46, 204, 113), 2: (241, 196, 15), 3: (230, 126, 34),
                4: (231, 76, 60), 5: (155, 89, 182)}

# ------------------------------------------------------------ countries
# Simplified borders (lng, lat): used to draw maps, and to place events whose
# country field is missing/"Unknown". "watch": False = drawn for context only.
COUNTRIES = {
    "SA": dict(name="Saudi Arabia", flag="🇸🇦", watch=True,
               aliases=["saudi arabia", "saudi", "ksa", "kingdom of saudi arabia"],
               poly=[(34.95, 29.36), (36.07, 29.19), (36.5, 29.5), (37.98, 30.0), (37.0, 31.5),
                     (39.2, 32.15), (40.4, 31.9), (42.0, 31.1), (44.7, 29.2), (46.55, 29.1),
                     (47.4, 28.5), (48.4, 28.55), (48.8, 27.6), (49.6, 26.9), (50.1, 26.2),
                     (50.2, 25.6), (50.8, 24.75), (51.6, 24.25), (52.6, 22.94), (55.2, 22.7),
                     (55.67, 22.0), (55.0, 20.0), (52.0, 19.0), (49.1, 18.6), (48.2, 18.2),
                     (47.0, 16.95), (46.3, 17.25), (44.5, 17.4), (43.4, 17.5), (43.2, 16.7),
                     (42.8, 16.4), (42.6, 16.8), (41.7, 17.9), (40.9, 19.5), (39.6, 20.9),
                     (39.1, 21.7), (38.5, 23.6), (37.5, 24.6), (36.6, 25.8), (35.6, 27.3),
                     (34.7, 28.1)],
               cities=[("Riyadh", 46.72, 24.71), ("Jeddah", 39.19, 21.49), ("Mecca", 39.83, 21.42),
                       ("Medina", 39.61, 24.47), ("Dammam", 50.10, 26.43), ("Tabuk", 36.57, 28.38),
                       ("Abha", 42.51, 18.22), ("Abqaiq", 49.67, 25.93)]),
    "AE": dict(name="UAE", flag="🇦🇪", watch=True,
               aliases=["united arab emirates", "uae", "u.a.e.", "emirates"],
               poly=[(51.6, 24.25), (52.6, 24.2), (53.9, 24.1), (54.6, 24.6), (55.4, 25.4),
                     (56.05, 26.05), (56.35, 25.1), (56.0, 24.4), (55.8, 24.0), (55.5, 23.5),
                     (55.2, 22.7), (52.6, 22.94)],
               cities=[("Abu Dhabi", 54.37, 24.45), ("Dubai", 55.27, 25.20), ("Fujairah", 56.33, 25.12),
                       ("Ras Al Khaimah", 55.94, 25.79), ("Al Ain", 55.74, 24.21)]),
    "QA": dict(name="Qatar", flag="🇶🇦", watch=True, aliases=["qatar", "state of qatar"],
               poly=[(50.75, 24.75), (50.85, 25.6), (51.05, 26.1), (51.3, 26.15), (51.6, 25.8),
                     (51.6, 25.2), (51.5, 24.6), (51.2, 24.55)],
               cities=[("Doha", 51.53, 25.29), ("Al Udeid", 51.32, 25.12), ("Ras Laffan", 51.54, 25.90)]),
    "BH": dict(name="Bahrain", flag="🇧🇭", watch=True, aliases=["bahrain", "kingdom of bahrain"],
               poly=[(50.35, 25.78), (50.40, 26.15), (50.47, 26.27), (50.65, 26.27), (50.66, 26.05),
                     (50.60, 25.85), (50.50, 25.78)],
               cities=[("Manama", 50.58, 26.22), ("Riffa", 50.55, 26.13)]),
    "KW": dict(name="Kuwait", flag="🇰🇼", watch=True, aliases=["kuwait", "state of kuwait"],
               poly=[(46.55, 29.1), (47.7, 30.1), (48.0, 29.95), (48.15, 29.5), (47.95, 29.35),
                     (48.4, 28.55), (47.7, 28.53), (47.4, 28.5)],
               cities=[("Kuwait City", 47.98, 29.37), ("Al Ahmadi", 48.08, 29.08)]),
    "IR": dict(name="Iran", flag="🇮🇷", watch=True,
               aliases=["iran", "islamic republic of iran", "iran (islamic republic of)"],
               poly=[(44.0, 39.4), (44.8, 39.7), (46.5, 38.9), (47.9, 39.6), (48.4, 38.8), (49.0, 38.4),
                     (50.0, 37.4), (51.5, 36.8), (53.9, 37.0), (54.8, 37.4), (56.2, 38.0), (57.4, 37.9),
                     (59.2, 37.4), (60.4, 36.6), (61.2, 36.6), (61.1, 35.6), (60.7, 34.3), (60.9, 33.5),
                     (60.6, 32.9), (60.9, 31.5), (61.7, 31.4), (61.8, 30.8), (60.9, 29.8), (61.4, 29.3),
                     (62.5, 28.3), (62.8, 27.2), (63.2, 27.0), (62.8, 26.6), (61.6, 25.2), (59.6, 25.4),
                     (58.0, 25.7), (57.3, 25.9), (56.8, 27.1), (56.2, 27.2), (55.0, 26.7), (53.5, 26.8),
                     (52.5, 27.4), (51.4, 27.9), (50.8, 28.8), (50.1, 30.1), (49.0, 30.5), (48.5, 29.9),
                     (48.0, 30.5), (48.0, 30.9), (47.7, 31.0), (47.7, 31.4), (47.0, 31.8), (46.2, 32.6),
                     (45.4, 33.2), (45.5, 34.0), (45.6, 34.6), (45.0, 35.2), (46.1, 35.8), (45.4, 36.0),
                     (44.8, 37.2), (44.2, 37.9), (44.3, 38.3)],
               cities=[("Tehran", 51.39, 35.69), ("Isfahan", 51.67, 32.65), ("Bandar Abbas", 56.27, 27.18),
                       ("Bushehr", 50.84, 28.92), ("Tabriz", 46.29, 38.08), ("Mashhad", 59.60, 36.30),
                       ("Shiraz", 52.53, 29.59), ("Ahvaz", 48.67, 31.32)]),
    "YE": dict(name="Yemen", flag="🇾🇪", watch=True, aliases=["yemen", "republic of yemen"],
               poly=[(42.8, 16.4), (43.2, 16.7), (43.4, 17.5), (44.5, 17.4), (46.3, 17.25), (47.0, 16.95),
                     (48.2, 18.2), (49.1, 18.6), (52.0, 19.0), (53.1, 16.65), (52.2, 15.6), (49.5, 14.6),
                     (48.0, 14.0), (45.6, 13.3), (45.0, 12.8), (43.5, 12.65), (43.2, 13.3), (42.8, 14.8),
                     (42.7, 15.7)],
               cities=[("Sanaa", 44.21, 15.35), ("Aden", 45.03, 12.80), ("Hodeidah", 42.95, 14.80),
                       ("Marib", 45.32, 15.46), ("Taiz", 44.02, 13.58), ("Saada", 43.76, 16.94)]),
    "OM": dict(name="Oman", flag="🇴🇲", watch=False, aliases=["oman"],
               poly=[(55.67, 22.0), (55.2, 22.7), (55.5, 23.5), (55.8, 24.0), (56.0, 24.4), (56.4, 24.9),
                     (57.2, 23.9), (58.6, 23.6), (59.8, 22.5), (59.3, 21.4), (58.5, 20.4), (57.8, 20.2),
                     (57.7, 19.0), (56.6, 18.6), (55.4, 17.8), (53.1, 16.65), (52.0, 19.0), (55.0, 20.0)],
               cities=[]),
}
WATCHED = [k for k, c in COUNTRIES.items() if c["watch"]]

# Words that make a non-"conflict" event count as conflict/security related.
SECURITY_RE = re.compile(
    r"\b(missiles?|drones?|air ?strikes?|strikes?|struck|attack(?:s|ed)?|explosions?|blasts?|"
    r"intercept(?:s|ed|ion|ions)?|air defen[cs]e|rockets?|shelling|militants?|militias?|"
    r"terror\w*|bomb(?:s|ing|ings)?|gunfire|gunmen|clash(?:es)?|assassinat\w*|hijack\w*|"
    r"irgc|houthis?|ansarallah|hezbollah|military|troops|navy|naval|warships?|sabotage|"
    r"seiz(?:e|ed|ure)|shoot ?down|shot down|ballistic|uav|ukmto|armed)\b", re.I)
NON_CONFLICT_CATEGORIES = {"disaster", "weather"}


# ------------------------------------------------------------ helpers

def log(*a):
    print(*a, flush=True)


def now_utc():
    if NOW_OVERRIDE:
        return datetime.fromisoformat(NOW_OVERRIDE.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def point_in_polygon(lng, lat, poly):
    inside, j = False, len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def country_code(ev):
    """Return a code from COUNTRIES (watched or not), or None."""
    raw = (ev.get("country") or "").strip().lower()
    if raw and raw not in ("unknown", "none", "null", "n/a"):
        for code, c in COUNTRIES.items():
            if raw in c["aliases"] or any(a == raw for a in c["aliases"]):
                return code
        for code, c in COUNTRIES.items():  # e.g. "Riyadh, Saudi Arabia"
            if any(re.search(r"\b" + re.escape(a) + r"\b", raw) for a in c["aliases"] if len(a) > 3):
                return code
        return None
    loc = (ev.get("location_name") or "").lower()
    for code, c in COUNTRIES.items():
        if any(re.search(r"\b" + re.escape(a) + r"\b", loc) for a in c["aliases"] if len(a) > 3):
            return code
    try:
        lng, lat = float(ev["lng"]), float(ev["lat"])
    except (KeyError, TypeError, ValueError):
        return None
    for code in ("BH", "QA", "KW", "AE", "YE", "IR", "SA", "OM"):  # small ones first
        if point_in_polygon(lng, lat, COUNTRIES[code]["poly"]):
            return code
    return None


def is_conflict(ev):
    cat = (ev.get("category") or "").strip().lower()
    if cat == "conflict":
        return True
    if cat in NON_CONFLICT_CATEGORIES:
        return False
    text = " ".join(str(ev.get(k) or "") for k in ("subtype", "title", "summary"))
    return bool(SECURITY_RE.search(text.replace("_", " ")))


def severity(ev):
    try:
        return max(1, min(5, int(ev.get("severity") or 1)))
    except (TypeError, ValueError):
        return 1


def confidence_label(conf):
    try:
        c = int(conf)
    except (TypeError, ValueError):
        return "❓ Unknown", None
    if c < 31:
        return "❓ Unconfirmed", c
    if c <= 55:
        return "🔸 Developing", c
    return "✅ Verified", c


def conf_text(ev):
    label, c = confidence_label(ev.get("confidence"))
    return f"{label} ({c})" if c is not None else label


def event_link(ev):
    return EVENT_URL.format(short_id=str(ev["id"]).replace("-", "")[:8])


def cinfo(ev):
    return COUNTRIES.get(ev.get("_cc") or country_code(ev), COUNTRIES["SA"])


def instant_text(ev, upgraded=False, test=False):
    c, sev = cinfo(ev), severity(ev)
    title = f"{c['flag']} {c['name']} · {SEVERITY_EMOJI[sev]} S{sev} · {conf_text(ev)}"
    if upgraded:
        title += " · ⬆️ upgraded"
    if test:
        title = "[TEST] " + title
    loc = ev.get("location_name") or c["name"]
    cat = (ev.get("category") or "").capitalize()
    message = f"{ev.get('title', 'Untitled event')}\n📍 {loc}" + (f" · {cat}" if cat else "")
    return title, message


# ------------------------------------------------------------ image

def _font(size, bold=False):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for base in ("/usr/share/fonts/truetype/dejavu/", ""):
        try:
            return ImageFont.truetype(base + name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def draw_flag(d, code, x, y, w=96, h=60):
    """Simplified flags drawn with shapes (no emoji font needed)."""
    G, W_, R, K = (0, 122, 61), (255, 255, 255), (206, 17, 38), (0, 0, 0)
    box = (x, y, x + w, y + h)
    if code == "SA":
        d.rectangle(box, fill=(0, 108, 53))
        d.line((x + w * .2, y + h * .72, x + w * .8, y + h * .72), fill=W_, width=3)
        for i in range(4):
            d.line((x + w * (.22 + i * .15), y + h * .3, x + w * (.32 + i * .15), y + h * .3), fill=W_, width=3)
    elif code == "AE":
        d.rectangle(box, fill=W_)
        d.rectangle((x, y, x + w, y + h / 3), fill=(0, 115, 47))
        d.rectangle((x, y + 2 * h / 3, x + w, y + h), fill=K)
        d.rectangle((x, y, x + w * .27, y + h), fill=(255, 0, 0))
    elif code in ("QA", "BH"):
        col = (138, 21, 56) if code == "QA" else (206, 17, 38)
        teeth = 9 if code == "QA" else 5
        d.rectangle(box, fill=col)
        bx = x + w * .3
        pts = [(x, y), (bx, y)]
        step = h / teeth
        for i in range(teeth):
            pts += [(bx + w * .1, y + step * (i + .5)), (bx, y + step * (i + 1))]
        pts += [(x, y + h)]
        d.polygon(pts, fill=W_)
    elif code == "KW":
        d.rectangle(box, fill=W_)
        d.rectangle((x, y, x + w, y + h / 3), fill=(0, 122, 61))
        d.rectangle((x, y + 2 * h / 3, x + w, y + h), fill=R)
        d.polygon([(x, y), (x + w * .25, y + h / 3), (x + w * .25, y + 2 * h / 3), (x, y + h)], fill=K)
    elif code == "IR":
        d.rectangle(box, fill=W_)
        d.rectangle((x, y, x + w, y + h / 3), fill=(35, 159, 64))
        d.rectangle((x, y + 2 * h / 3, x + w, y + h), fill=(218, 0, 0))
        d.ellipse((x + w / 2 - 7, y + h / 2 - 7, x + w / 2 + 7, y + h / 2 + 7), outline=(218, 0, 0), width=3)
    elif code == "YE":
        d.rectangle(box, fill=W_)
        d.rectangle((x, y, x + w, y + h / 3), fill=R)
        d.rectangle((x, y + 2 * h / 3, x + w, y + h), fill=K)
    else:
        d.rectangle(box, fill=(80, 90, 100))
    d.rectangle(box, outline=(200, 205, 210), width=1)


def draw_map(img, rect, bbox, pins, focus=None, flag=None):
    """bbox=(lng0,lng1,lat0,lat1). pins=[(lng,lat,sev)]. focus = country code to highlight."""
    from PIL import Image, ImageDraw
    import math
    d = ImageDraw.Draw(img)
    mx0, my0, mx1, my1 = rect
    d.rounded_rectangle(rect, 18, fill=(18, 28, 40))
    lng0, lng1, lat0, lat1 = bbox
    k = math.cos(math.radians((lat0 + lat1) / 2))
    pw, ph = mx1 - mx0 - 40, my1 - my0 - 40
    scale = min(pw / ((lng1 - lng0) * k), ph / (lat1 - lat0))
    cx, cy = (lng0 + lng1) / 2, (lat0 + lat1) / 2
    ox, oy = (mx0 + mx1) / 2, (my0 + my1) / 2

    def xy(lng, lat):
        return ox + (lng - cx) * k * scale, oy - (lat - cy) * scale

    small = _font(15)
    clip = Image.new("RGB", img.size)
    layer = img.copy()
    ld = ImageDraw.Draw(layer)
    for code, c in COUNTRIES.items():
        fill = (52, 72, 92) if code == focus else (32, 42, 54)
        outline = (140, 175, 205) if code == focus else (70, 85, 100)
        ld.polygon([xy(*p) for p in c["poly"]], fill=fill, outline=outline)
    for code, c in COUNTRIES.items():
        if code == focus:
            for name, lng, lat in c["cities"]:
                if lng0 <= lng <= lng1 and lat0 <= lat <= lat1:
                    x, y = xy(lng, lat)
                    ld.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(150, 165, 180))
                    ld.text((x + 6, y - 9), name, font=small, fill=(150, 165, 180))
        else:
            xs = [p[0] for p in c["poly"]]
            ys = [p[1] for p in c["poly"]]
            lx, ly = sum(xs) / len(xs), sum(ys) / len(ys)
            if lng0 + 1 < lx < lng1 - 1 and lat0 + .5 < ly < lat1 - .5:
                ld.text(xy(lx, ly), c["name"], font=small, fill=(95, 110, 125), anchor="mm")
    for lng, lat, sev in pins:
        px, py = xy(lng, lat)
        col = SEVERITY_RGB[sev]
        for r, a in ((26, 60), (16, 120)):
            ring = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse((px - r, py - r, px + r, py + r), fill=col + (a,))
            layer.paste(ring, (0, 0), ring)
        ld = ImageDraw.Draw(layer)
        ld.ellipse((px - 9, py - 9, px + 9, py + 9), fill=col, outline=(255, 255, 255), width=3)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(rect, 18, fill=255)
    img.paste(layer, (0, 0), mask)
    if flag:
        d = ImageDraw.Draw(img)
        draw_flag(d, flag, mx0 + 18, my0 + 18, 72, 45)
    del clip


def country_bbox(code, lng=None, lat=None):
    xs = [p[0] for p in COUNTRIES[code]["poly"]]
    ys = [p[1] for p in COUNTRIES[code]["poly"]]
    if lng is not None:
        xs.append(lng); ys.append(lat)
    lng0, lng1, lat0, lat1 = min(xs), max(xs), min(ys), max(ys)
    span = max(lng1 - lng0, lat1 - lat0, 3.0) * 1.25
    cx, cy = (lng0 + lng1) / 2, (lat0 + lat1) / 2
    return (cx - span / 2, cx + span / 2, cy - span / 2, cy + span / 2)


def wrap(d, text, font, width, max_lines):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= width:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".") + "…"
    return lines


def make_event_image(ev):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), (16, 21, 27))
    code = ev.get("_cc") or country_code(ev) or "SA"
    try:
        lng, lat = float(ev["lng"]), float(ev["lat"])
        pins = [(lng, lat, severity(ev))]
    except (KeyError, TypeError, ValueError):
        lng = lat = None
        pins = []
    draw_map(img, (30, 30, 640, 600), country_bbox(code, lng, lat), pins, focus=code, flag=code)
    d = ImageDraw.Draw(img)
    x, sev = 670, severity(ev)
    col = SEVERITY_RGB[sev]
    draw_flag(d, code, x, 40, 64, 40)
    d.text((x + 80, 60), COUNTRIES[code]["name"], font=_font(30, True), fill=(235, 240, 245), anchor="lm")
    d.rounded_rectangle((x, 100, x + 110, 155), 12, fill=col)
    d.text((x + 55, 127), f"S{sev}", font=_font(32, True), fill=(16, 21, 27), anchor="mm")
    label, c = confidence_label(ev.get("confidence"))
    label = label.split(" ", 1)[1]
    ccol = {"Verified": (46, 204, 113), "Developing": (241, 196, 15)}.get(label, (160, 170, 180))
    ct = f"{label} {c}" if c is not None else label
    f26 = _font(24, True)
    cw = d.textlength(ct, font=f26) + 32
    d.rounded_rectangle((x + 125, 100, x + 125 + cw, 155), 12, outline=ccol, width=3)
    d.text((x + 125 + cw / 2, 127), ct, font=f26, fill=ccol, anchor="mm")
    tf = _font(32, True)
    y = 180
    for ln in wrap(d, ev.get("title", ""), tf, W - x - 40, 5):
        d.text((x, y), ln, font=tf, fill=(235, 240, 245)); y += 42
    y += 14
    d.text((x, y), ev.get("location_name") or COUNTRIES[code]["name"], font=_font(22), fill=(180, 190, 200)); y += 32
    when = ev.get("created_at") or ""
    cat = (ev.get("category") or "").capitalize()
    d.text((x, y), " · ".join(p for p in (cat, f"{when} UTC" if when else "") if p), font=_font(20), fill=(130, 140, 150))
    d.text((x, 578), "monitor-the-situation.com · automated, may be inaccurate", font=_font(16), fill=(90, 100, 110))
    buf = io.BytesIO(); img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def make_digest_image(items, shift_label):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), (16, 21, 27))
    pins = []
    for it in items:
        try:
            pins.append((float(it["lng"]), float(it["lat"]), severity(it)))
        except (KeyError, TypeError, ValueError):
            pass
    draw_map(img, (30, 30, 640, 600), (40.5, 63.5, 11.5, 40.5), pins, focus=None)
    d = ImageDraw.Draw(img)
    x = 670
    d.text((x, 45), "Low-severity digest", font=_font(34, True), fill=(235, 240, 245))
    d.text((x, 92), f"{len(items)} event(s) · {shift_label}", font=_font(22), fill=(160, 170, 180))
    y = 140
    f = _font(20)
    for it in items[:9]:
        code = it.get("_cc") or country_code(it) or "SA"
        draw_flag(d, code, x, y + 2, 30, 19)
        sev = severity(it)
        d.rounded_rectangle((x + 40, y, x + 84, y + 24), 6, fill=SEVERITY_RGB[sev])
        d.text((x + 62, y + 12), f"S{sev}", font=_font(16, True), fill=(16, 21, 27), anchor="mm")
        t = wrap(d, it.get("title", ""), f, W - x - 140, 1)
        d.text((x + 94, y + 1), t[0] if t else "", font=f, fill=(220, 225, 230))
        y += 46
    if len(items) > 9:
        d.text((x, y), f"+{len(items) - 9} more", font=f, fill=(160, 170, 180))
    d.text((x, 578), "monitor-the-situation.com · automated, may be inaccurate", font=_font(16), fill=(90, 100, 110))
    buf = io.BytesIO(); img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------ ntfy

def ntfy_send(title, message, click=None, image=None):
    """Always default priority (3): never Time Sensitive / Critical on iOS."""
    if len(message.encode()) > NTFY_MAX_MESSAGE:
        message = message.encode()[:NTFY_MAX_MESSAGE].decode(errors="ignore") + "…"
    if DRY_RUN or not NTFY_TOPIC:
        log(f"[dry-run] {title} | {message!r} | {click} | image={len(image) if image else 0}B")
        return True
    try:
        if image:
            params = {"title": title, "message": message, "filename": "alert.png"}
            if click:
                params["click"] = click
            url = f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, data=image, method="PUT", headers={"User-Agent": USER_AGENT})
        else:
            body = {"topic": NTFY_TOPIC, "title": title, "message": message}
            if click:
                body["click"] = click
            req = urllib.request.Request(NTFY_SERVER, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        log("ntfy send failed:", e)
        if image:
            return ntfy_send(title, message, click, None)
        return False


def notify_instant(ev, upgraded=False, test=False):
    title, message = instant_text(ev, upgraded, test)
    img = None
    try:
        img = make_event_image(ev)
    except Exception as e:  # noqa: BLE001
        log("image failed, sending text only:", e)
    return ntfy_send(title, message, event_link(ev), img)


def notify_digest(items, shift_label, test=False):
    flags = "".join(dict.fromkeys(cinfo(i)["flag"] for i in items))
    title = f"{flags} Digest · {len(items)} low-severity event{'s' if len(items) != 1 else ''} · {shift_label}"
    if test:
        title = "[TEST] " + title
    lines = []
    for it in sorted(items, key=lambda i: i.get("created_at") or ""):
        c, sev = cinfo(it), severity(it)
        lines.append(f"{c['flag']} {c['name']} · {SEVERITY_EMOJI[sev]} S{sev} · {conf_text(it)}\n   {it.get('title', '')}")
    img = None
    try:
        img = make_digest_image(items, shift_label)
    except Exception as e:  # noqa: BLE001
        log("digest image failed:", e)
    return ntfy_send(title, "\n".join(lines), REGION_URL, img)


# ------------------------------------------------------------ state / feed

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def fetch_feed():
    if FEED_FILE:
        with open(FEED_FILE) as f:
            data = json.load(f)
    else:
        req = urllib.request.Request(FEED_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    if isinstance(data, dict):
        data = data.get("events") or data.get("data") or []
    if not isinstance(data, list):
        raise ValueError("unexpected feed format")
    return data


def current_slot(now):
    """Most recent digest time (UTC) at or before now, as (slot_id, Riyadh label)."""
    cands = []
    for day in (now.date() - timedelta(days=1), now.date()):
        for h in DIGEST_HOURS_UTC:
            t = datetime(day.year, day.month, day.day, h, tzinfo=timezone.utc)
            if t <= now:
                cands.append(t)
    t = max(cands)
    return t.strftime("%Y-%m-%dT%H"), t.astimezone(RIYADH).strftime("%H:%M Riyadh")


def snapshot(ev):
    keep = ("id", "title", "severity", "confidence", "lat", "lng", "location_name", "category", "created_at", "_cc")
    return {k: ev.get(k) for k in keep}


# ------------------------------------------------------------ main

def main():
    if not NTFY_TOPIC and not DRY_RUN:
        log("NTFY_TOPIC secret is not set."); sys.exit(1)

    state = load_state()
    now = now_utc()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state["last_check_day"] = now.strftime("%Y-%m-%d")   # keeps the repo 'active'
    slot_id, slot_label = current_slot(now)

    try:
        events = fetch_feed()
    except Exception as e:  # noqa: BLE001
        state["fail_count"] = state.get("fail_count", 0) + 1
        log(f"Could not read feed ({state['fail_count']} in a row):", e)
        if state["fail_count"] == FAIL_ALERT_AFTER:
            ntfy_send("⚙️ Gulf alerts: feed unreachable",
                      "The monitor hasn't been able to read the site for ~30 min. "
                      "It keeps trying and will tell you when it's back.", REGION_URL)
        if TEST_MODE:
            ntfy_send("[TEST] ⚙️ Gulf alerts", f"Test ran but the feed was unreachable: {e}")
        save_state(state)
        return

    if state.get("fail_count", 0) >= FAIL_ALERT_AFTER:
        ntfy_send("⚙️ Gulf alerts: back online", "The monitor can read the site again.", REGION_URL)
    state["fail_count"] = 0

    matched, by_country, in_area = [], {}, 0
    for e in events:
        if not e.get("id"):
            continue
        code = country_code(e)
        if code not in WATCHED:
            continue
        in_area += 1
        if not is_conflict(e):
            continue
        e["_cc"] = code
        matched.append(e)
        by_country[code] = by_country.get(code, 0) + 1
    matched.sort(key=lambda e: e.get("created_at") or "")
    hi = [e for e in matched if severity(e) >= INSTANT_MIN_SEVERITY]
    log(f"Feed: {len(events)} events; {in_area} in watched countries; {len(matched)} conflict-related "
        f"({len(hi)} S3+, {len(matched) - len(hi)} S1-S2); by country: "
        + ", ".join(f"{COUNTRIES[c]['name']} {n}" for c, n in sorted(by_country.items())))

    seen = state.setdefault("events", {})
    queue = state.setdefault("digest_queue", [])

    if TEST_MODE:
        sample = hi[-1] if hi else {
            "id": "00000000-test", "title": "Sample instant alert (no S3+ events in the feed right now)",
            "severity": 4, "confidence": 60, "lat": 25.2, "lng": 55.27, "_cc": "AE",
            "location_name": "Dubai, UAE", "category": "conflict"}
        log("Test instant:", "sent" if notify_instant(sample, test=True) else "FAILED", "-", sample.get("title"))
        lows = [e for e in matched if severity(e) < INSTANT_MIN_SEVERITY][-6:]
        if not lows:
            lows = [{"id": "t1", "title": "Sample low-severity item", "severity": 2, "confidence": 40,
                     "lat": 15.35, "lng": 44.21, "_cc": "YE", "location_name": "Sanaa, Yemen"},
                    {"id": "t2", "title": "Sample low-severity item", "severity": 1, "confidence": 25,
                     "lat": 26.22, "lng": 50.58, "_cc": "BH", "location_name": "Manama, Bahrain"}]
        log("Test digest:", "sent" if notify_digest(lows, slot_label, test=True) else "FAILED",
            f"- {len(lows)} item(s)")

    if state.get("version") != STATE_VERSION:
        # First run of this version: remember everything already in the feed (no flood).
        for e in matched:
            seen[e["id"]] = {"sev": severity(e), "t": stamp}
        state.update(version=STATE_VERSION, initialized=True, last_digest_slot=slot_id)
        state["digest_queue"] = []
        names = ", ".join(COUNTRIES[c]["flag"] + " " + COUNTRIES[c]["name"] for c in WATCHED)
        ntfy_send("✅ Gulf alerts updated",
                  f"Now watching conflict events in {names}.\nS3–S5: instant. S1–S2: digest at "
                  f"06:00, 14:00 and 22:00 Riyadh time (skipped if empty).\n"
                  f"{len(matched)} events already in the feed were skipped.", REGION_URL)
        save_state(state)
        return

    # --- instant alerts and digest queue
    instant = []
    queued_ids = {q["id"] for q in queue}
    for e in matched:
        sev, prev = severity(e), seen.get(e["id"])
        if prev is None:
            if sev >= INSTANT_MIN_SEVERITY:
                instant.append((e, False))
            elif e["id"] not in queued_ids:
                queue.append(snapshot(e)); queued_ids.add(e["id"])
        elif sev >= INSTANT_MIN_SEVERITY and sev > prev.get("sev", 0):
            instant.append((e, True))
        if e["id"] in queued_ids:   # refresh queued item / drop it if it became instant-level
            if sev >= INSTANT_MIN_SEVERITY:
                queue[:] = [q for q in queue if q["id"] != e["id"]]
                queued_ids.discard(e["id"])
            else:
                for q in queue:
                    if q["id"] == e["id"]:
                        q.update(snapshot(e))
        if prev is None or sev > prev.get("sev", 0):
            seen[e["id"]] = {"sev": sev, "t": stamp}

    if len(instant) > MAX_PER_RUN:
        extra, instant = instant[:-MAX_PER_RUN], instant[-MAX_PER_RUN:]
        flags = "".join(dict.fromkeys(cinfo(e)["flag"] for e, _ in extra))
        ntfy_send(f"{flags} +{len(extra)} more S3+ events",
                  "\n".join(f"{cinfo(e)['flag']} S{severity(e)} {e.get('title', '')}" for e, _ in extra),
                  REGION_URL)
    for e, upgraded in instant:
        if notify_instant(e, upgraded=upgraded):
            log("instant sent:", e.get("title"))
        time.sleep(1)

    if state.get("last_digest_slot") != slot_id:
        if queue:
            if notify_digest(queue, slot_label):
                log(f"digest sent: {len(queue)} item(s) ({slot_label})")
                state["digest_queue"] = []
                state["last_digest_slot"] = slot_id
        else:
            log(f"digest skipped (nothing to report) ({slot_label})")
            state["last_digest_slot"] = slot_id

    cutoff = (now - timedelta(days=FORGET_AFTER_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    current = {e["id"] for e in matched}
    for k in [k for k, v in seen.items() if v.get("t", "") < cutoff and k not in current]:
        del seen[k]
    save_state(state)


if __name__ == "__main__":
    main()
