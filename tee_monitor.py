"""
Horseshoe Bay Golf Club tee-time monitor.

Logs into ClubHouse Online, follows the SSO bridge to MemberFirst Tee Times,
scrapes the tee sheet for the next N days, finds fully-open tee times in
your preferred window, and texts you when new openings appear.

Run modes:
    python tee_monitor.py                # single check, send alerts for new openings
    python tee_monitor.py --dry-run      # check + log, do NOT send any texts
    python tee_monitor.py --debug        # verbose logging, save raw HTML for inspection
    python tee_monitor.py --reset-state  # wipe the seen-openings cache (re-alert everything)
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.ini"
STATE_PATH = SCRIPT_DIR / "state.json"
LOG_PATH = SCRIPT_DIR / "tee_monitor.log"
DEBUG_DIR = SCRIPT_DIR / "debug"

CLUBHOUSE_HOST = "https://horseshoebaygolfclubwi.clubhouseonline-e3.com"
LOGIN_URL = f"{CLUBHOUSE_HOST}/login.aspx"
MEMBER_CENTRAL = f"{CLUBHOUSE_HOST}/Member_Central"
TEESHEET_BASE = "https://horseshoebaygolfclub.mfteetimes.com/teetimes.php"
TEESHEET_AJAX = ("https://horseshoebaygolfclub.mfteetimes.com/igolf/includes_front/spt/ajax/teesheetviews/getTeeSheetGrid")
DEFAULT_COURSE_ID = 4

SMS_GATEWAYS = {
    "att": "mms.att.net",
    "verizon": "vtext.com",
    "tmobile": "tmomail.net",
    "sprint": "messaging.sprintpcs.com",
    "uscellular": "email.uscc.net",
    "boost": "smsmyboostmobile.com",
    "cricket": "sms.cricketwireless.net",
    "metro": "mymetropcs.com",
    "googlefi": "msg.fi.google.com",
}


@dataclass(frozen=True)
class TeeTime:
    date: str
    time: str
    course: str
    players: int
    raw: str = ""

    def key(self) -> str:
        return f"{self.date}|{self.time}|{self.course}|{self.players}"

    def display(self) -> str:
        d = datetime.strptime(self.date, "%Y-%m-%d")
        h, m = self.time.split(":")
        h_i = int(h)
        suffix = "AM" if h_i < 12 else "PM"
        h_12 = h_i if 1 <= h_i <= 12 else (h_i - 12 if h_i > 12 else 12)
        return f"{d.strftime('%a %m/%d')} {h_12}:{m} {suffix} ({self.players} open)"


@dataclass
class Config:
    username: str
    password: str
    phone: str
    carrier: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    smtp_from: str
    window_start: int
    window_end: int
    days_ahead: int
    course_id: int
    weekdays_only: bool
    weekends_only: bool
    notify_method: str = "sms"
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    pushover_token: str = ""
    pushover_user: str = ""

    @property
    def sms_address(self) -> str:
        gateway = SMS_GATEWAYS.get(self.carrier.lower())
        if not gateway:
            raise SystemExit(f"Unknown carrier '{self.carrier}'")
        digits = re.sub(r"\D", "", self.phone)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise SystemExit(f"Phone number '{self.phone}' is not 10 digits.")
        return f"{digits}@{gateway}"


def load_config(path: Path) -> Config:
    if not path.exists():
        raise SystemExit(f"Missing config file: {path}")
    cp = configparser.ConfigParser()
    cp.read(path)
    try:
        return Config(
            username=cp.get("clubhouse", "username"),
            password=cp.get("clubhouse", "password"),
            phone=cp.get("notify", "phone"),
            carrier=cp.get("notify", "carrier"),
            smtp_host=cp.get("smtp", "host"),
            smtp_port=cp.getint("smtp", "port"),
            smtp_user=cp.get("smtp", "user"),
            smtp_pass=cp.get("smtp", "password"),
            smtp_from=cp.get("smtp", "from_addr"),
            window_start=cp.getint("filter", "window_start_hour"),
            window_end=cp.getint("filter", "window_end_hour"),
            days_ahead=cp.getint("filter", "days_ahead", fallback=14),
            course_id=cp.getint("filter", "course_id", fallback=DEFAULT_COURSE_ID),
            weekdays_only=cp.getboolean("filter", "weekdays_only", fallback=False),
            weekends_only=cp.getboolean("filter", "weekends_only", fallback=False),
            notify_method=cp.get("notify", "method", fallback="sms").lower(),
            ntfy_topic=cp.get("notify", "ntfy_topic", fallback=""),
            ntfy_server=cp.get("notify", "ntfy_server", fallback="https://ntfy.sh").rstrip("/"),
            pushover_token=cp.get("notify", "pushover_token", fallback=""),
            pushover_user=cp.get("notify", "pushover_user", fallback=""),
        )
    except (configparser.NoSectionError, configparser.NoOptionError) as e:
        raise SystemExit(f"Config file is missing a value: {e}")


def login(session, cfg):
    logging.info("Fetching login page...")
    r = session.get(LOGIN_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def field(name):
        el = soup.find("input", {"name": name})
        return el.get("value", "") if el else ""

    payload = {
        "__VIEWSTATE": field("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": field("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": field("__EVENTVALIDATION"),
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
    }
    user_input = soup.find("input", {"type": "text"}) or soup.find(
        "input", {"name": re.compile(r"UserName", re.I)})
    pass_input = soup.find("input", {"type": "password"})
    login_btn = soup.find("input", {"type": "submit"}) or soup.find(
        "input", {"name": re.compile(r"LoginButton|Submit", re.I)})

    if not (user_input and pass_input):
        save_debug("login_page.html", r.text)
        raise SystemExit("Couldn't locate username/password fields.")

    payload[user_input["name"]] = cfg.username
    payload[pass_input["name"]] = cfg.password
    if login_btn and login_btn.get("name"):
        payload[login_btn["name"]] = login_btn.get("value", "Log In")

    logging.info("Submitting login form...")
    r2 = session.post(LOGIN_URL, data=payload, timeout=30, allow_redirects=True)
    r2.raise_for_status()

    if 'type="password"' in r2.text.lower() and "logout" not in r2.text.lower():
        save_debug("login_response.html", r2.text)
        raise SystemExit("Login appears to have failed.")
    logging.info("Login successful.")

    # SSO BRIDGE: walk through the Member Central page to find a launcher
    # link to mfteetimes (or a clubhouseonline page that redirects to it).
    try:
        mc = session.get(MEMBER_CENTRAL, timeout=30)
        save_debug("member_central.html", mc.text)
        candidates = _find_tee_time_links(mc.text)
    except requests.RequestException as e:
        logging.warning("Could not load Member_Central: %s", e)
        candidates = []

    logging.info("Tee-time candidate links from Member_Central:")
    for c in candidates[:10]:
        logging.info("  %s", c)

    visited_any = False
    for url in candidates[:5]:
        logging.info("Visiting bridge candidate: %s", url)
        try:
            br = session.get(url, timeout=30, allow_redirects=True)
            logging.info("  -> final: %s status=%s len=%d cookies=%s",
                         br.url, br.status_code, len(br.text),
                         list(session.cookies.keys()))
            save_debug(f"bridge_{re.sub(r'[^a-zA-Z0-9]+','_',url)[-60:]}.html",
                       br.text)
            visited_any = True
        except requests.RequestException as e:
            logging.warning("  failed: %s", e)

    if not visited_any:
        logging.warning(
            "Found no usable tee-time launcher on Member_Central. "
            "Inspect debug/member_central.html and tell me what link you click "
            "to reach the tee sheet in your browser.")



def _find_tee_time_links(html):
    """
    Return candidate launcher URLs in priority order:
      1. Direct mfteetimes.com URLs
      2. ClubHouse Online /Golf/... or /Tee... paths that look like launchers
         (Reserve/Sheet/Book/Schedule), excluding policy/info pages
    """
    if not html:
        return []
    out = []
    # 1. Absolute mfteetimes.com URLs anywhere in the HTML
    for m in re.findall(r'https?://[^\s"\'<>]+mfteetimes\.com[^\s"\'<>]*', html, re.I):
        if m not in out:
            out.append(m)
    # 2. href= paths that mention "tee" and a time/sheet/book word
    paths = re.findall(r'href="(/[^"]*[Tt]ee[^"]*)"', html)
    def score(path):
        s = 0
        low = path.lower()
        for good in ("reserve", "book", "sheet", "schedule",
                     "tee_times", "teetimes", "tee-times", "tee_time"):
            if good in low:
                s += 3
        for bad in ("polic", "rule", "info", "faq", "about", "help",
                    "instruction", "guideline", "etiquette"):
            if bad in low:
                s -= 5
        return s
    paths = sorted(set(paths), key=lambda p: -score(p))
    for p in paths:
        if score(p) <= 0:
            continue
        full = CLUBHOUSE_HOST + p
        if full not in out:
            out.append(full)
    return out


def fetch_teesheet(session, day, course_id):
    """
    Hit the MFTeeTimes JSON AJAX endpoint and return the rendered HTML grid.
    The endpoint returns {"success": bool, "data": "<html...>"}; the HTML
    inside `data` is what gets injected into #teesheetCards on the page.
    """
    params = {
        "course": str(course_id),
        "dateof": day.isoformat(),
        "time": "",
        "ui_id": "#teesheetGrid",
    }
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://horseshoebaygolfclub.mfteetimes.com/teetimes.php",
    }
    logging.debug("AJAX GET %s %s", TEESHEET_AJAX, params)
    r = session.get(TEESHEET_AJAX, params=params, headers=headers,
                    timeout=30, allow_redirects=True)
    if r.status_code != 200:
        logging.warning("AJAX %s -> %s, body[:200]=%r",
                        day, r.status_code, r.text[:200])
        return ""
    try:
        j = r.json()
    except ValueError:
        logging.warning("AJAX %s: response not JSON, body[:200]=%r",
                        day, r.text[:200])
        return ""
    if not j.get("success"):
        logging.warning("AJAX %s: success=False, keys=%s, msg=%s",
                        day, list(j.keys()), j.get("msg"))
        return ""
    html = j.get("data", "") or ""
    if not html:
        logging.warning("AJAX %s: empty data field", day)
    return html


TIME_PATTERNS = [
    re.compile(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)\b", re.I),
    re.compile(r"\b(\d{1,2}):(\d{2})\b"),
]


def parse_teesheet(html, day):
    """
    Parse the AJAX-rendered teesheet HTML for MemberFirst.

    Each tee-time slot is a <div class="tt card ..."> with data-* attributes:
      - data-timeof="HH:MM:SS"
      - data-slotsavailable="N"   (0 = full, N>0 = some seats open)
      - data-maxslots="M"         (usually 4)
    A slot is fully blank (no names attached) when slotsavailable == maxslots.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.tt.card[data-timeof]")
    openings = []
    seen_keys = set()
    total = 0
    full_open = 0
    partial = 0
    booked = 0
    for c in cards:
        total += 1
        timeof = c.get("data-timeof", "")
        try:
            avail = int(c.get("data-slotsavailable", "0"))
            maxs  = int(c.get("data-maxslots", "0"))
        except ValueError:
            continue
        if avail <= 0:
            booked += 1
            continue
        if maxs and avail < maxs:
            partial += 1
            continue   # user wants fully-blank only
        full_open += 1
        m = re.match(r"^(\d{1,2}):(\d{2})", timeof)
        if not m:
            continue
        hh, mm = int(m.group(1)), int(m.group(2))
        time_24 = f"{hh:02d}:{mm:02d}"
        tt = TeeTime(
            date=day.isoformat(),
            time=time_24,
            course=str(c.get("data-course", DEFAULT_COURSE_ID)),
            players=avail,
            raw=f"slotsavailable={avail}/{maxs} timeof={timeof}",
        )
        if tt.key() in seen_keys:
            continue
        seen_keys.add(tt.key())
        openings.append(tt)

    logging.info("  %s: cards=%d, fully_open=%d, partial=%d, booked=%d",
                 day, total, full_open, partial, booked)
    return openings


# legacy helpers kept for unit tests (older test fixtures still rely on them)
_ALLOWED_WORDS = re.compile(
    r"\b("
    r"AM|PM|"
    r"open|available|book|reserve|reserved|sign\s*up|tee\s*time|"
    r"hole|holes|men|women|junior|guest|guests|player|players|"
    r"mon|tue|tues|wed|thu|thur|fri|sat|sun|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|"
    r"october|november|december|"
    r"course|hbgc|north|south|east|west|cypress|apple"
    r")\b", re.I)


def _has_name(text):
    residue = text
    for pat in TIME_PATTERNS:
        residue = pat.sub(" ", residue)
    residue = _ALLOWED_WORDS.sub(" ", residue)
    residue = re.sub(r"[^A-Za-z]+", " ", residue)
    return any(len(w) >= 2 for w in residue.split())


def _extract_time(text):
    for pat in TIME_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        hour = int(m.group(1))
        minute = int(m.group(2))
        ampm = m.group(3).upper() if m.lastindex == 3 else None
        if ampm == "PM" and hour < 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        if 0 <= hour < 24 and 0 <= minute < 60:
            return f"{hour:02d}:{minute:02d}"
    return None


def in_window(tt, cfg):
    hour = int(tt.time.split(":")[0])
    if not (cfg.window_start <= hour < cfg.window_end):
        return False
    weekday = datetime.strptime(tt.date, "%Y-%m-%d").weekday()
    if cfg.weekends_only and weekday < 5:
        return False
    if cfg.weekdays_only and weekday >= 5:
        return False
    return True


def load_state():
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text())
        today = date.today().isoformat()
        return {k for k in data.get("seen", []) if k.split("|", 1)[0] >= today}
    except Exception as e:
        logging.warning("Could not read state file (%s); starting fresh.", e)
        return set()


def save_state(seen):
    STATE_PATH.write_text(json.dumps({"seen": sorted(seen)}, indent=2))


def send_pushover(cfg, body, title="Tee time open"):
    if not cfg.pushover_token or not cfg.pushover_user:
        raise SystemExit("notify.method=pushover but pushover_token/user not set in config.ini")
    # pushover_user may be a comma-separated list of user keys -- send once per recipient
    users = [u.strip() for u in cfg.pushover_user.split(",") if u.strip()]
    if not users:
        raise SystemExit("pushover_user is empty after parsing")
    sent_ok = 0
    failures = []
    for u in users:
        payload = {
            "token": cfg.pushover_token,
            "user": u,
            "title": title,
            "message": body,
            "priority": 1,   # high priority -- bypass quiet hours
        }
        logging.info("Posting alert to Pushover (user=%s...)", u[:6])
        try:
            r = requests.post("https://api.pushover.net/1/messages.json",
                              data=payload, timeout=30)
            r.raise_for_status()
            try:
                j = r.json()
                if j.get("status") != 1:
                    failures.append(f"{u[:6]}...: {j}")
                    continue
            except ValueError:
                pass
            sent_ok += 1
            logging.info("  -> %s", r.status_code)
        except requests.RequestException as e:
            failures.append(f"{u[:6]}...: {e}")
    if failures:
        logging.warning("Pushover send failed for %d recipient(s): %s",
                        len(failures), failures)
    if sent_ok == 0:
        raise SystemExit("All Pushover recipients failed")
    logging.info("Pushover: %d/%d recipients OK", sent_ok, len(users))


def send_ntfy(cfg, body, title="Tee time open"):
    if not cfg.ntfy_topic:
        raise SystemExit("notify.method=ntfy but ntfy_topic is empty in config.ini")
    url = f"{cfg.ntfy_server}/{cfg.ntfy_topic}"
    headers = {
        "Title": title,
        "Priority": "max",
        "Tags": "golf,calendar",
    }
    logging.info("Posting alert to %s", url)
    r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=30)
    r.raise_for_status()
    logging.info("ntfy response: %s", r.status_code)


def send_sms(cfg, body):
    msg = EmailMessage()
    msg["Subject"] = ""
    msg["From"] = cfg.smtp_from
    msg["To"] = cfg.sms_address
    msg.set_content(body)
    logging.info("Sending alert to %s via %s", cfg.sms_address, cfg.smtp_host)
    if cfg.smtp_port == 465:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.login(cfg.smtp_user, cfg.smtp_pass)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(cfg.smtp_user, cfg.smtp_pass)
            s.send_message(msg)


def save_debug(name, content):
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / name).write_text(content, encoding="utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser(description="Horseshoe Bay tee-time monitor")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--reset-state", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])

    cfg = load_config(CONFIG_PATH)

    if args.reset_state and STATE_PATH.exists():
        STATE_PATH.unlink()
        logging.info("State reset.")

    seen = load_state()

    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36")
    })

    try:
        login(session, cfg)
    except requests.RequestException as e:
        logging.error("Login failed: %s", e)
        return 2

    new_openings = []
    today = date.today()
    for offset in range(cfg.days_ahead):
        day = today + timedelta(days=offset)
        try:
            html = fetch_teesheet(session, day, cfg.course_id)
        except requests.RequestException as e:
            logging.warning("Could not fetch %s: %s", day, e)
            continue

        if args.debug:
            save_debug(f"teesheet_{day.isoformat()}.html", html)

        for tt in parse_teesheet(html, day):
            if not in_window(tt, cfg):
                continue
            logging.debug("Found in window: %s [%s]", tt.display(), tt.raw)
            if tt.key() not in seen:
                new_openings.append(tt)
                seen.add(tt.key())

    if not new_openings:
        logging.info("No new openings in window %d:00-%d:00.",
                     cfg.window_start, cfg.window_end)
        save_state(seen)
        return 0

    body = "New tee times open:\n" + "\n".join(t.display() for t in new_openings[:8])
    if len(new_openings) > 8:
        body += f"\n(+{len(new_openings) - 8} more)"
    logging.info("Alert body:\n%s", body)

    if args.dry_run:
        logging.info("Dry run -- not sending SMS.")
    else:
        try:
            if cfg.notify_method == "pushover":
                send_pushover(cfg, body)
            elif cfg.notify_method == "ntfy":
                send_ntfy(cfg, body)
            else:
                send_sms(cfg, body)
        except Exception as e:
            logging.error("Notification send failed: %s", e)
            return 3

    save_state(seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
