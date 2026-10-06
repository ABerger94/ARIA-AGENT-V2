"""web.py — web, comms, media, github, mcp, skills, scheduler tasks, autonomy,
vision, hardware, bridge, routines, personas, and misc tools.

Every external-service / hardware import is a guarded top-level import
(see below). Unavailable services return honest bracket messages — never
raise on import, never fake success.

Import rule: NO import statement inside any function or indented block.
The system-toolkit state web.py needs (scheduler, media keys, health)
lives in leaf modules (sched.py, mediakeys.py, health.py) — no system <->
web circular import.

NOTE: the five approval-control tools (approve, deny, list_pending_approvals,
get_approval_mode, set_approval_mode) are registered by ToolRegistry itself
as bound methods, so their state stays with the gate. They are NOT
re-registered here.
"""
from __future__ import annotations

import base64
import html as html_lib
import itertools
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from aria.tools.registry import (
    preflight,
    ToolFailed,
    ToolRepairable,
    LoopGuardTripped,
)
from aria.tools.toolkits.files import (
    _workspace,
    disk_usage as _du,
    find_file as _ff,
    read_file as _rf,
)
from aria.tools.toolkits.sched import sched_add, sched_cancel, sched_list
from aria.tools.toolkits.mediakeys import media_key
from aria.tools.toolkits.health import system_health_audit
from aria.tools.sandbox import run_python
from aria.tools.mcp_bridge import (
    set_registry as set_bridge_registry,
    mcp_setup_status,
    mcp_connect_status,
    mcp_disconnect_status,
    mcp_list_servers_status,
    mcp_remove_server_status,
)
from aria.vision.capture import grab_screen, grab_webcam
from aria.vision.pipeline import describe_native
from aria.core.optimport import optional_module as _optional_module
from aria.tools.hardware_proto import (
    BAUD as _BODY_BAUD,
    HardwareState as _HardwareState,
    clamp_servo as _clamp_servo,
    clamp_wheels as _clamp_wheels,
    encode_drive as _encode_drive,
    encode_servo as _encode_servo,
    encode_stop as _encode_stop,
    port_matches as _port_matches,
)

import smtplib
import imaplib
import email as email_lib
import webbrowser
from email.message import EmailMessage

# ---------------------------------------------------------------------------
# Guarded optional deps — all at column 0, each with a HAS_* flag.
# No import statement inside any function or indented block.
# ---------------------------------------------------------------------------

_DDGS_mod = _optional_module("duckduckgo_search")
DDGS = getattr(_DDGS_mod, "DDGS", None) if _DDGS_mod is not None else None
HAS_DDGS = DDGS is not None

_memory_store = _optional_module("aria.memory.store")
HAS_MEMORY_STORE = _memory_store is not None

serial = _optional_module("serial")
HAS_SERIAL = serial is not None

_list_ports_mod = _optional_module("serial.tools.list_ports")
HAS_LIST_PORTS = _list_ports_mod is not None

_face_track_mod = _optional_module("aria.vision.face_track")
HAS_FACE_TRACK = _face_track_mod is not None

_REGISTRY_REF = None  # set in register()

# ---------------------------------------------------------------------------
# Local keys file (runtime data only — never credentials in code)
# ---------------------------------------------------------------------------

_KEYS_FILE = os.path.expanduser("~/workspace/aria-v2/aria_keys.json")


def _load_keys() -> Dict[str, str]:
    try:
        with open(_KEYS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_keys(keys: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(_KEYS_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_KEYS_FILE), prefix=".keys-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(keys, f, indent=2)
        os.replace(tmp, _KEYS_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _get_key(name: str) -> str:
    v = _load_keys().get(name, "").strip()
    return "" if v in ("", "INSERT") else v


def _set_key(name: str, value: str) -> None:
    keys = _load_keys()
    keys[name] = value
    _save_keys(keys)


# ---------------------------------------------------------------------------
# Web: search + fetch
# ---------------------------------------------------------------------------

def _search_structured(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    if DDGS is None:
        raise RuntimeError("duckduckgo_search not installed")
    out = []
    for r in DDGS().text(query, max_results=max_results) or []:
        out.append({"title": r.get("title", ""), "url": r.get("href", ""),
                    "body": r.get("body", "")})
    return out


def web_search(args: Dict[str, Any]) -> str:
    query = str(args.get("query", "") or "").strip()
    if not query:
        return "[web_search needs a query]"
    try:
        results = _search_structured(query, 3)
    except RuntimeError:
        return "[web search unavailable: duckduckgo_search not installed]"
    except Exception as e:
        return f"[search error: {e}]"
    if not results:
        return "No web results found."
    return "\n".join(f"• {r['title']}: {r['body']}" for r in results)


def _fetch_html(url: str, timeout: int = 12) -> Optional[str]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _html_to_text(html: str) -> str:
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<noscript.*?</noscript>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def fetch_url(args: Dict[str, Any]) -> str:
    url = str(args.get("url", "") or "").strip()
    if not url:
        return "[fetch_url needs a url]"
    html = _fetch_html(url)
    if not html:
        return f"[Could not fetch {url}]"
    return _html_to_text(html)[:8000]


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def _gmail_creds() -> Tuple[str, str]:
    return (_get_key("GMAIL_USER"),
            _get_key("GMAIL_APP_PASSWORD").replace(" ", ""))


def gmail_setup(args: Dict[str, Any]) -> str:
    user = str(args.get("gmail_user", "") or "").strip()
    pw = str(args.get("app_password", "") or "").replace(" ", "").strip()
    if not user or not pw:
        return "[gmail_setup needs both gmail_user and app_password]"
    _set_key("GMAIL_USER", user)
    _set_key("GMAIL_APP_PASSWORD", pw)
    return (f"Gmail saved for {user}. You can now send email with send_email "
            "and read it with read_email.")


def _gmail_not_configured() -> str:
    return ("[gmail not configured — call gmail_setup with your Gmail address "
            "and an app password first]")


def send_email(args: Dict[str, Any]) -> str:
    user, pw = _gmail_creds()
    if not user or not pw:
        return _gmail_not_configured()
    to = str(args.get("to", "") or "").strip()
    subject = str(args.get("subject", "") or "(no subject)")
    body = str(args.get("body", "") or "")
    if not to:
        return "[send_email needs a recipient]"
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        return f"Email sent to {to}: '{subject}'."
    except smtplib.SMTPAuthenticationError:
        return ("[gmail rejected the login — the app password is wrong or revoked; "
                "generate a fresh one and call gmail_setup again]")
    except Exception as e:
        return f"[could not send email to {to}: {e}]"


def _imap_open():
    user, pw = _gmail_creds()
    if not user or not pw:
        return None, _gmail_not_configured()
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        m.login(user, pw)
        return m, None
    except Exception:
        return None, ("[gmail rejected the login — the app password is wrong or "
                      "revoked; call gmail_setup again]")


def read_email(args: Dict[str, Any]) -> str:
    m, err = _imap_open()
    if err:
        return err
    query = str(args.get("query", "") or "")
    uid = str(args.get("uid", "") or "")
    unread_only = bool(args.get("unread_only", False))
    try:
        limit = max(1, min(50, int(args.get("limit", 10) or 10)))
    except Exception:
        limit = 10
    try:
        m.select("INBOX", readonly=True)
        if uid:
            typ, data = m.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not data or not data[0]:
                return f"[no message with uid {uid}]"
            raw = data[0][1] if isinstance(data[0], tuple) else data[0]
            msg = email_lib.message_from_bytes(raw)
            body = _imap_text(msg).strip()[:4000]
            return (f"From: {msg.get('From', '?')}\nDate: {msg.get('Date', '?')}\n"
                    f"Subject: {msg.get('Subject', '(no subject)')}\n\n{body or '[no text body]'}")
        if query:
            try:
                typ, data = m.uid("SEARCH", None, "X-GM-RAW", query)
                if typ != "OK":
                    raise Exception("X-GM-RAW failed")
            except Exception:
                typ, data = m.uid("SEARCH", None, "TEXT", query)
        elif unread_only:
            typ, data = m.uid("SEARCH", None, "UNSEEN")
        else:
            typ, data = m.uid("SEARCH", None, "ALL")
        if typ != "OK":
            return "[gmail search failed]"
        uids = (data[0] or b"").split()[-limit:][::-1]
        if not uids:
            return "No matching emails."
        lines = []
        for u in uids:
            u = u.decode()
            typ, data = m.uid("FETCH", u, "(BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)])")
            if typ != "OK" or not data or not data[0]:
                continue
            raw = data[0][1] if isinstance(data[0], tuple) else data[0]
            h = email_lib.message_from_bytes(raw)
            frm = (h.get("From") or "?").replace("\n", " ")[:60]
            subj = (h.get("Subject") or "(no subject)").replace("\n", " ")[:80]
            dt = (h.get("Date") or "?")[:31]
            lines.append(f"[uid={u}] {dt} | {frm} | \"{subj}\"")
        return f"{len(lines)} email(s), newest first:\n" + "\n".join(lines)
    except Exception as e:
        return f"[could not read gmail: {e}]"
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _imap_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8",
                                          errors="replace")
                except Exception:
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return str(msg.get_payload())


_IMPORTANT_RE = re.compile(
    r"urgent|invoice|bill|payment|security|action required|overdue|court|lawsuit|fraud",
    re.I)
_NOISE_RE = re.compile(
    r"newsletter|unsubscribe|promo|sale|deal|marketing|webinar|digest", re.I)


def triage_email(args: Dict[str, Any]) -> str:
    m, err = _imap_open()
    if err:
        return err
    try:
        limit = max(1, min(50, int(args.get("limit", 20) or 20)))
    except Exception:
        limit = 20
    try:
        m.select("INBOX", readonly=True)
        typ, data = m.uid("SEARCH", None, "ALL")
        if typ != "OK":
            return "[gmail search failed]"
        uids = (data[0] or b"").split()[-limit:][::-1]
        buckets = {"IMPORTANT": [], "FYI": [], "NOISE": []}
        for u in uids:
            u = u.decode()
            typ, data = m.uid("FETCH", u, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if typ != "OK" or not data or not data[0]:
                continue
            raw = data[0][1] if isinstance(data[0], tuple) else data[0]
            h = email_lib.message_from_bytes(raw)
            subj = h.get("Subject") or "(no subject)"
            frm = h.get("From") or "?"
            blob = f"{subj} {frm}"
            if _IMPORTANT_RE.search(blob):
                buckets["IMPORTANT"].append((frm, subj))
            elif _NOISE_RE.search(blob):
                buckets["NOISE"].append((frm, subj))
            else:
                buckets["FYI"].append((frm, subj))
        out = ["Inbox triage:"]
        for label in ("IMPORTANT", "FYI", "NOISE"):
            items = buckets[label]
            out.append(f"\n{label} ({len(items)}):")
            out.extend(f"  • {f[:50]} — {s[:70]}" for f, s in items[:15])
        return "\n".join(out)
    except Exception as e:
        return f"[triage failed: {e}]"
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Calendar (live iCal feed)
# ---------------------------------------------------------------------------

def calendar_setup(args: Dict[str, Any]) -> str:
    url = str(args.get("ical_url", "") or "").strip()
    if not url.startswith(("http://", "https://")) or "ics" not in url.lower():
        return ("[that doesn't look like an iCal URL — in Google Calendar go to "
                "Settings > your calendar > 'Secret address in iCal format' and pass it here]")
    _set_key("ICAL_URL", url)
    return "Calendar connected. I'll read your live schedule from now on."


def _ical_unescape(s: str) -> str:
    return (s.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";"))


def _ical_parse_dt(value: str, params: str) -> Optional[datetime]:
    """Parse an ICS date/time into a naive-local datetime.

    Covers Google Calendar's secret-feed forms: UTC (...Z), floating local,
    TZID=..., and date-only (all-day)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        if value.endswith("Z"):
            aware = datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            return aware.astimezone().replace(tzinfo=None)
        m = re.match(r"(\d{8})T(\d{6})$", value)
        if m:
            dt = datetime.strptime(m.group(1) + "T" + m.group(2), "%Y%m%dT%H%M%S")
            tzid = re.search(r"TZID=([^;:]+)", params or "")
            if tzid:
                try:
                    zone = ZoneInfo(tzid.group(1))
                    return dt.replace(tzinfo=zone).astimezone().replace(tzinfo=None)
                except Exception:
                    pass
            return dt
        if re.match(r"^\d{8}$", value):  # date-only: all-day
            return datetime.strptime(value, "%Y%m%d")
    except ValueError:
        pass
    return None


def _parse_ical_events(ics: str) -> List[Dict[str, Any]]:
    # Unfold continuation lines, then scan VEVENT blocks keeping params.
    lines = []
    for raw in (ics or "").splitlines():
        line = raw.rstrip("\r\n")
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    events, in_event, props = [], False, {}
    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event, props = True, {}
        elif line == "END:VEVENT":
            if in_event and "DTSTART" in props:
                params, raw = props["DTSTART"]
                start = _ical_parse_dt(raw, params)
                if start:
                    summary = _ical_unescape(props.get("SUMMARY", ("", ""))[1]).strip() or "(no title)"
                    loc = _ical_unescape(props.get("LOCATION", ("", ""))[1]).strip()
                    if loc:
                        summary = f"{summary} @ {loc}"
                    all_day = "T" not in raw.strip().upper()
                    events.append({"start": start, "summary": summary,
                                   "all_day": all_day})
            in_event = False
        elif in_event and ":" in line:
            prop, _, val = line.partition(":")
            name = prop.split(";")[0].strip().upper()
            if name in ("DTSTART", "DTEND", "SUMMARY", "LOCATION"):
                props[name] = (prop[len(name):], val)
    return events


def check_calendar(args: Dict[str, Any]) -> str:
    url = _get_key("ICAL_URL")
    if not url:
        return ("[no calendar connected yet — call calendar_setup with your "
                "Google Calendar secret iCal URL]")
    try:
        days = max(1, min(14, int(args.get("days", 1) or 1)))
    except Exception:
        days = 1
    ics = _fetch_html(url, timeout=15)
    if not ics:
        return "[could not fetch your calendar]"
    now = datetime.now()
    end = now + timedelta(days=days)
    events = [e for e in _parse_ical_events(ics) if now - timedelta(hours=12) <= e["start"] <= end]
    events.sort(key=lambda e: e["start"])
    if not events:
        return f"Nothing on your calendar for the next {days} day(s)."

    def _ft(dt):
        h = dt.hour % 12 or 12
        return f"{h}:{dt.minute:02d} {'PM' if dt.hour >= 12 else 'AM'}"

    lines = []
    for e in events:
        s = e["start"]
        day = f"{s.strftime('%a %b')} {s.day}"
        tm = f"{day} (all day)" if e.get("all_day") else f"{day}, {_ft(s)}"
        lines.append(f"- {tm}: {e['summary']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MTG (Scryfall — free, no key)
# ---------------------------------------------------------------------------

def _scryfall(url: str) -> Tuple[Optional[dict], Optional[str]]:
    req = urllib.request.Request(
        url, headers={"User-Agent": "ARIA-Agent/2.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "not found"
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)[:120]


def mtg_card(args: Dict[str, Any]) -> str:
    name = str(args.get("card_name", "") or "").strip()
    if not name:
        return "[mtg_card needs a card_name]"
    c, err = _scryfall("https://api.scryfall.com/cards/named?fuzzy=" +
                       urllib.parse.quote(name))
    if c is None or c.get("object") == "error":
        return f"[card not found: '{name}']"
    usd = (c.get("prices") or {}).get("usd")
    lines = [f"{c.get('name', '?')} {c.get('mana_cost', '')}".strip(),
             c.get("type_line", ""),
             (c.get("oracle_text") or "[no oracle text]")[:600]]
    if usd:
        lines.append(f"Market: ~${usd}")
    return "\n".join(lines)


def mtg_advice(args: Dict[str, Any]) -> str:
    deck = str(args.get("deck", "") or "").strip()
    name = str(args.get("card_name", "") or "").strip()
    if not name or not deck:
        return "[mtg_advice needs both deck and card_name]"
    c, err = _scryfall("https://api.scryfall.com/cards/named?fuzzy=" +
                       urllib.parse.quote(name))
    if c is None or "name" not in c:
        return f"[couldn't fetch '{name}': {err or 'not found'}]"
    return (
        f"Card: {c.get('name')}\n"
        f"Type: {c.get('type_line', '')}\n"
        f"Mana: {c.get('mana_cost', '')}\n"
        f"Oracle: {(c.get('oracle_text') or '')[:300]}\n"
        f"EDHREC: #{c.get('edhrec_rank', 'n/a')}\n"
        f"Deck context: {deck}"
    )


# ---------------------------------------------------------------------------
# Price watches (sqlite, ported from v1)
# ---------------------------------------------------------------------------

def _price_db():
    os.makedirs(_workspace(), exist_ok=True)
    conn = sqlite3.connect(os.path.join(_workspace(), "price_watches.db"))
    conn.execute("""CREATE TABLE IF NOT EXISTS price_watches(
        id INTEGER PRIMARY KEY, url TEXT, target REAL, label TEXT,
        last_price REAL, alerted INTEGER DEFAULT 0, created TEXT)""")
    return conn


def watch_price(args: Dict[str, Any]) -> str:
    url = str(args.get("url", "") or "").strip()
    label = str(args.get("label", "") or "item").strip()
    try:
        target = float(str(args.get("target_price", "")).replace("$", "").strip())
    except ValueError:
        return "[target_price must be a number, e.g. 40]"
    if not url:
        return "[watch_price needs a url]"
    with _price_db() as db:
        cur = db.execute(
            "INSERT INTO price_watches(url,target,label,last_price,created) VALUES(?,?,?,?,?)",
            (url, target, label, None, datetime.now().isoformat()))
        wid = cur.lastrowid
    return (f"Watching '{label}' (#{wid}): I'll alert you when it's at or below "
            f"${target:.2f}. Checks run hourly.")


def list_price_watches(args: Dict[str, Any]) -> str:
    with _price_db() as db:
        rows = db.execute(
            "SELECT id,label,url,target,last_price,alerted FROM price_watches ORDER BY id"
        ).fetchall()
    if not rows:
        return "[no active price watches]"
    return "\n".join(
        f"#{r[0]} {r[1]} — target ${r[3]:.2f}"
        f"{f', last seen ${r[4]:.2f}' if r[4] else ', not checked yet'}"
        f"{' [alerted]' if r[5] else ''}\n  {r[2]}" for r in rows)


def unwatch_price(args: Dict[str, Any]) -> str:
    try:
        wid = int(args.get("watch_id", 0))
    except Exception:
        return "[unwatch_price needs a numeric watch_id]"
    with _price_db() as db:
        cur = db.execute("DELETE FROM price_watches WHERE id=?", (wid,))
    return f"Removed price watch #{wid}." if cur.rowcount else f"[no watch #{wid}]"


_PRICE_RE = re.compile(r"\$\s?([\d,]+\.\d{2})")
_JSONLD_PRICE_RE = re.compile(r'"price"\s*:\s*"?([0-9][0-9,]*\.[0-9]{2})"?')


def extract_price(text: str):
    """Best-effort price extraction. Returns float or None.

    Checks JSON-LD ``"price"`` markup first (far more reliable on modern
    product pages), then falls back to ``$`` amounts.
    """
    if not text:
        return None
    m = _JSONLD_PRICE_RE.search(text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    prices = []
    for m in _PRICE_RE.finditer(text):
        try:
            prices.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
    return min(prices) if prices else None


def check_price_watches(args: Dict[str, Any]) -> str:
    with _price_db() as db:
        rows = db.execute(
            "SELECT id,url,target,label,last_price,alerted FROM price_watches").fetchall()
    if not rows:
        return "[no active price watches]"
    out = []
    for wid, url, target, label, last_price, alerted in rows:
        html = _fetch_html(url)
        if not html:
            out.append(f"#{wid} {label}: [could not fetch page]")
            continue
        seen = extract_price(_html_to_text(html)[:20000])
        if seen is None:
            out.append(f"#{wid} {label}: [no price found on page]")
            continue
        with _price_db() as db:
            db.execute("UPDATE price_watches SET last_price=? WHERE id=?", (seen, wid))
        if seen <= target and not alerted:
            with _price_db() as db:
                db.execute("UPDATE price_watches SET alerted=1 WHERE id=?", (wid,))
            out.append(f"#{wid} {label}: PRICE DROP — ${seen:.2f} (target ${target:.2f})")
        else:
            out.append(f"#{wid} {label}: ${seen:.2f} (target ${target:.2f})")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Inbox (local upload dir)
# ---------------------------------------------------------------------------

def _inbox_dir() -> str:
    d = os.path.join(_workspace(), "inbox")
    os.makedirs(d, exist_ok=True)
    return d


def inbox_list(args: Dict[str, Any]) -> str:
    try:
        entries = []
        for fn in sorted(os.listdir(_inbox_dir()),
                         key=lambda f: os.path.getmtime(os.path.join(_inbox_dir(), f)),
                         reverse=True):
            full = os.path.join(_inbox_dir(), fn)
            if os.path.isfile(full):
                sz = os.path.getsize(full)
                when = datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M")
                entries.append(f"- {fn} ({sz // 1024} KB, {when})")
    except Exception as e:
        return f"[could not list inbox: {e}]"
    return "\n".join(entries) if entries else "Inbox is empty."


def inbox_read(args: Dict[str, Any]) -> str:
    name = str(args.get("name", "") or "").strip()
    d = _inbox_dir()
    if not name:
        texts = [f for f in os.listdir(d)
                 if os.path.isfile(os.path.join(d, f))
                 and os.path.splitext(f)[1].lower() in (".txt", ".md", ".csv", ".json", ".log")]
        if not texts:
            return "[no text files in inbox]"
        name = sorted(texts, key=lambda f: os.path.getmtime(os.path.join(d, f)))[-1]
    path = os.path.join(d, os.path.basename(name))
    if not os.path.isfile(path):
        return f"[no file '{name}' in inbox]"
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()[:8000]
    except Exception as e:
        return f"[could not read '{name}': {e}]"


def inbox_describe(args: Dict[str, Any]) -> str:
    name = str(args.get("name", "") or "").strip()
    if not name:
        return "[inbox_describe needs a file name]"
    path = os.path.join(_inbox_dir(), os.path.basename(name))
    if not os.path.isfile(path):
        return f"[no file '{name}' in inbox]"
    if os.path.splitext(path)[1].lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return "[inbox_describe works on images — use inbox_read for text]"
    try:
        with open(path, "rb") as f:
            img = f.read()
        desc = describe_native(img, "Describe this image in detail.")
        return desc or "[vision describe failed]"
    except Exception:
        return "[vision not available]"


# ---------------------------------------------------------------------------
# Spotify / DJ (media keys work headless; app launching opens the desktop app)
# ---------------------------------------------------------------------------

# Ported from v1 aria/spotify.py: curated mood playlists used when the
# request doesn't match a saved one.
_CURATED_MOODS = {
    "chill": ("spotify:playlist:37i9dQZF1DWWQRwui0ExPn", "Lofi Beats"),
    "lofi": ("spotify:playlist:37i9dQZF1DWWQRwui0ExPn", "Lofi Beats"),
    "focus": ("spotify:playlist:37i9dQZF1DWZeKCadgRdKQ", "Deep Focus"),
    "ambient": ("spotify:playlist:37i9dQZF1DWZeKCadgRdKQ", "Deep Focus"),
    "study": ("spotify:playlist:37i9dQZF1DWZeKCadgRdKQ", "Deep Focus"),
    "energy": ("spotify:playlist:37i9dQZF1DX76Wlfdnj7AP", "Beast Mode"),
    "workout": ("spotify:playlist:37i9dQZF1DX76Wlfdnj7AP", "Beast Mode"),
    "gym": ("spotify:playlist:37i9dQZF1DX76Wlfdnj7AP", "Beast Mode"),
    "electronic workout": ("spotify:playlist:37i9dQZF1DX76Wlfdnj7AP", "Beast Mode"),
}


def _find_playlist(name: str) -> Tuple[Optional[str], Optional[str]]:
    """Find a saved playlist URI in v2 memory (category='playlist'),
    fuzzy name match. Returns (uri, key)."""
    words = [w for w in re.sub(r"[^a-z0-9\s]", "", (name or "").lower()).split()
             if w not in ("my", "playlist", "playlists")]
    if not words or _memory_store is None:
        return None, None
    rows: List[Dict[str, Any]] = []
    try:
        rows = _memory_store.search_memory_fts(words[0], limit=50) or []
        if not rows and len(words) > 1:
            rows = _memory_store.search_memory_fts(" ".join(words), limit=50) or []
    except Exception:
        return None, None
    for row in rows:
        if str(row.get("category", "")).lower() != "playlist":
            continue
        key = str(row.get("key", ""))
        kl = key.lower()
        if all(w in kl for w in words) or all(w in " ".join(words) for w in kl.split()):
            return str(row.get("value", "")).strip(), key
    return None, None


def _spotify_open_uri(uri: str) -> str:
    """Open a spotify: URI in the desktop app. Honest on failure."""
    try:
        ok = webbrowser.open(uri)
    except Exception as e:
        return f"[could not open Spotify: {e}]"
    if not ok:
        return "[could not open Spotify — is it installed on this machine?]"
    return f"Spotify: opened {uri}."


_SPOTIFY_MEDIA = {
    "play": "play_pause", "pause": "play_pause", "play_pause": "play_pause",
    "resume": "play_pause", "next": "next", "skip": "next",
    "previous": "prev", "prev": "prev", "back": "prev", "stop": "stop",
    "mute": "mute", "volume_up": "volume_up", "vol_up": "volume_up",
    "volume_down": "volume_down", "vol_down": "volume_down",
}


def spotify(args: Dict[str, Any]) -> str:
    action = str(args.get("action", "play_pause") or "play_pause").lower().strip()
    query = str(args.get("query", "") or "").strip()
    if action in _SPOTIFY_MEDIA:
        try:
            return media_key({"action": _SPOTIFY_MEDIA[action]})
        except Exception:
            return "[media key unavailable]"
    if action == "open":
        return _spotify_open_uri("spotify:")
    if action == "play_uri":
        if not query:
            return "[give me a spotify: URI, e.g. spotify:playlist:xxx]"
        return _spotify_open_uri(query)
    if action in ("play", "search"):
        if not query:
            return "[spotify play/search needs a query]"
        if query.startswith("spotify:") or query.startswith("http"):
            return _spotify_open_uri(query)
        saved_uri, saved_name = _find_playlist(query)
        if saved_uri:
            return _spotify_open_uri(saved_uri)
        ql = query.lower()
        for m_key, (m_uri, _m_label) in _CURATED_MOODS.items():
            if m_key in ql:
                return _spotify_open_uri(m_uri)
        return _spotify_open_uri("spotify:search:" + urllib.parse.quote(query))
    return (f"[unknown spotify action '{action}' — use open, play, pause, next, "
            "previous, stop, mute, volume_up, volume_down, search, or play_uri]")


def dj(args: Dict[str, Any]) -> str:
    request = str(args.get("request", "") or "").strip()
    if not request:
        return "[dj needs a request, e.g. 'shuffle my liked songs']"
    q = request.lower()
    uri, label = None, ""
    if "liked songs" in q:
        uri, label = "spotify:collection:tracks", "Liked Songs"
    else:
        name = re.sub(r"\b(play|shuffle|shuffled|some|something|music|me|my|"
                      r"playlist|playlists|on|spotify)\b", "", q).strip()
        uri, label = _find_playlist(name)
    if not uri:
        for m_key, (m_uri, m_label) in _CURATED_MOODS.items():
            if m_key in q:
                uri, label = m_uri, m_label
                break
    if not uri:
        clean = re.sub(r"\b(play|shuffle|shuffled|some|something|music|me|my|"
                       r"playlist|playlists|on|spotify)\b", "", q).strip()
        if clean:
            return _spotify_open_uri("spotify:search:" + urllib.parse.quote(clean))
        return (f"[I don't have a playlist saved for '{request}' — save the "
                "Spotify link with save_memory (category 'playlist') and I'll "
                "remember it for next time]")
    opened = _spotify_open_uri(uri)
    if opened.startswith("["):
        return opened
    try:
        media_key({"action": "play_pause"})
    except Exception:
        pass
    return f"Spotify DJ: playing {label or uri}."


# ---------------------------------------------------------------------------
# GitHub (token from local keys file)
# ---------------------------------------------------------------------------

def github_setup(args: Dict[str, Any]) -> str:
    token = str(args.get("token", "") or "").strip()
    if not token:
        return ("[github_setup needs a token — create one at "
                "github.com/settings/tokens with the 'repo' scope]")
    _set_key("GITHUB_TOKEN", token)
    return "GitHub token saved. github_push_file and github_create_repo are ready."


def _github_not_configured() -> str:
    return ("[github not configured — run github_setup with a personal "
            "access token first]")


def _github_request(path: str, method: str = "GET",
                    payload: Optional[dict] = None) -> Tuple[Optional[dict], Optional[str]]:
    token = _get_key("GITHUB_TOKEN")
    if not token:
        return None, _github_not_configured()
    url = f"https://api.github.com{path}"
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "User-Agent": "ARIA-Agent/2.0",
               "Content-Type": "application/json"}
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"GitHub API error {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as e:
        return None, f"GitHub request failed: {e}"


def github_push_file(args: Dict[str, Any]) -> str:
    repo = str(args.get("repo", "") or "").strip()
    filepath = str(args.get("filepath", "") or "").strip()
    content = str(args.get("content", "") or "")
    message = str(args.get("message", "") or "")
    if not repo or not filepath:
        return "[github_push_file needs repo and filepath]"
    enc_path = urllib.parse.quote(filepath)
    existing, _ = _github_request(f"/repos/{repo}/contents/{enc_path}")
    payload = {"message": message or f"ARIA: update {filepath}",
               "content": base64.b64encode(content.encode("utf-8")).decode()}
    if existing and "sha" in existing:
        payload["sha"] = existing["sha"]
    _result, err = _github_request(f"/repos/{repo}/contents/{enc_path}", "PUT", payload)
    if err:
        return f"Push failed: {err}"
    return f"Pushed '{filepath}' to {repo}."


def github_create_repo(args: Dict[str, Any]) -> str:
    name = str(args.get("name", "") or "").strip()
    if not name:
        return "[github_create_repo needs a name]"
    description = str(args.get("description", "") or "")
    private = bool(args.get("private", True))
    result, err = _github_request("/user/repos", "POST",
                                 {"name": name, "description": description,
                                  "private": private, "auto_init": True})
    if err:
        return f"Repo creation failed: {err}"
    return f"Repository '{name}' created: {(result or {}).get('html_url', '')}"


# ---------------------------------------------------------------------------
# MCP (real bridge — aria/tools/mcp_bridge.py). Server tools register
# dynamically as mcp_<server>__<tool> on connect and unload on disconnect.
# ---------------------------------------------------------------------------

def mcp_setup(args: Dict[str, Any]) -> str:
    return mcp_setup_status(
        name=str(args.get("name", "") or ""),
        transport=str(args.get("transport", "") or ""),
        command=str(args.get("command", "") or ""),
        args=args.get("args", ""),
        url=str(args.get("url", "") or ""),
        env=args.get("env", ""),
        headers=args.get("headers", ""),
    )


def mcp_connect(args: Dict[str, Any]) -> str:
    return mcp_connect_status(name=str(args.get("name", "") or ""))


def mcp_disconnect(args: Dict[str, Any]) -> str:
    return mcp_disconnect_status(name=str(args.get("name", "") or ""))


def mcp_list_servers(args: Dict[str, Any]) -> str:
    return mcp_list_servers_status()


def mcp_remove_server(args: Dict[str, Any]) -> str:
    return mcp_remove_server_status(name=str(args.get("name", "") or ""))


# ---------------------------------------------------------------------------
# Bounded workflow skills (ported from v1 skills.py)
# ---------------------------------------------------------------------------

_SKILL_BUDGET = 12


class _Budget:
    def __init__(self, n: int):
        self.n = n

    def spend(self, what: str) -> None:
        self.n -= 1
        if self.n < 0:
            raise RuntimeError(f"skill budget exceeded at: {what}")


def _skill_deep_research(objective: str, b: _Budget) -> str:
    b.spend("search:1")
    results = _search_structured(objective, 5)
    if not results:
        return f"Deep research on '{objective}': no web results found."
    cites, bodies = [], []
    for r in results[:3]:
        b.spend(f"fetch:{r['url'][:40]}")
        html = _fetch_html(r["url"])
        text = _html_to_text(html)[:2500] if html else r["body"]
        cites.append(f"[{len(cites) + 1}] {r['title']} — {r['url']}")
        bodies.append(f"[{len(cites)}] {text[:1200]}")
    return (f"Research: {objective}\n\n" + "\n\n".join(bodies) +
            "\n\nSources:\n" + "\n".join(cites))


def _skill_system_check(objective: str, b: _Budget) -> str:
    b.spend("health")
    health = system_health_audit({})
    b.spend("disk")
    usage = _du({"directory": _workspace(), "top_n": 10})
    return f"System check ({objective or 'full'}):\n{health}\n\nWorkspace disk:\n{usage}"


def _skill_file_sweep(objective: str, b: _Budget) -> str:
    words = [w for w in re.findall(r"[a-zA-Z]{3,}", objective.lower())][:3]
    found = []
    for w in words:
        b.spend(f"find:{w}")
        out = _ff({"name": w})
        if not out.startswith("[No files"):
            found.extend(out.splitlines()[1:4])
    seen, digests = [], []
    for path in found:
        if path in seen or len(digests) >= 5:
            continue
        seen.append(path)
        b.spend(f"read:{os.path.basename(path)[:30]}")
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                digests.append(f"== {path} ==\n{f.read(2000)}")
        except OSError:
            continue
    if not digests:
        return f"File sweep on '{objective}': no matching files found."
    return f"File sweep on '{objective}':\n\n" + "\n\n".join(digests)


def _skill_git_audit(objective: str, b: _Budget) -> str:
    root = _workspace()
    b.spend("git")
    try:
        def _git(*a):
            return subprocess.run(["git", "-C", root, *a], capture_output=True,
                                  text=True, timeout=15).stdout.strip()
        branch = _git("branch", "--show-current") or "(detached)"
        status = _git("status", "--short") or "(clean)"
        log = _git("log", "--oneline", "-10") or "(no commits)"
        return (f"Git audit of workspace:\nbranch: {branch}\nstatus:\n{status}\n"
                f"recent commits:\n{log}")
    except Exception as e:
        return f"[git audit failed: {e}]"


_SKILLS = {
    "deep_research": ("Web research with a cited summary.", _skill_deep_research),
    "system_check": ("Laptop health report.", _skill_system_check),
    "file_sweep": ("Find and digest workspace files matching the objective.", _skill_file_sweep),
    "git_audit": ("Git branch, status, and recent commits report.", _skill_git_audit),
}


def run_skill(args: Dict[str, Any]) -> str:
    name = str(args.get("skill_name", "") or "").strip()
    objective = str(args.get("objective", "") or "").strip()
    if name not in _SKILLS:
        avail = ", ".join(f"{k} ({v[0]})" for k, v in _SKILLS.items())
        return f"[unknown skill '{name}' — available: {avail}]"
    if not objective:
        return "[run_skill needs an objective]"
    try:
        return _SKILLS[name][1](objective, _Budget(_SKILL_BUDGET))
    except RuntimeError as e:
        return f"[skill '{name}' stopped: {e}]"
    except Exception as e:
        return f"[skill '{name}' failed: {e}]"


# ---------------------------------------------------------------------------
# run_python_code (sandbox) + self-heal diagnosis
# ---------------------------------------------------------------------------

def run_python_code(args: Dict[str, Any]) -> str:
    code = str(args.get("code", "") or "")
    if not code.strip():
        return "[run_python_code needs code]"
    res = run_python(code, timeout=30)
    if res.ok:
        return res.output if res.output.strip() else "[no output]"
    return f"[python failed]\n{res.output}"


def _diagnose(error_text: str) -> Dict[str, str]:
    """Error categorization ported from v1 self_healing.diagnose_error."""
    err = str(error_text or "")
    m = re.search(r"(?:No module named|ModuleNotFoundError: No module named) ['\"]([^'\"]+)['\"]", err)
    if m:
        return {"category": "MISSING_PACKAGE", "heal": True,
                "diagnosis": f"Missing Python package: '{m.group(1).split('.')[0]}'",
                "action": f"pip install {m.group(1).split('.')[0]}"}
    m = re.search(r"(?:No such file or directory|FileNotFoundError): ['\"]([^'\"]+)['\"]", err)
    if m:
        return {"category": "PATH_NOT_FOUND", "heal": True,
                "diagnosis": f"Missing file or directory: '{m.group(1)}'",
                "action": f"Create the parent directory for '{m.group(1)}'"}
    if "unicodeescape" in err or "truncated \\U" in err or "truncated \\u" in err:
        return {"category": "UNICODE_ESCAPE_SYNTAX", "heal": True,
                "diagnosis": "Windows path backslashes broke string parsing",
                "action": "Rewrite backslashes as forward slashes"}
    if "SyntaxError" in err:
        return {"category": "SYNTAX_ERROR", "heal": False,
                "diagnosis": "Python syntax error in executed code",
                "action": "Review the syntax and regenerate"}
    if any(k in err.lower() for k in ("timed out", "timeout", "503", "502", "connection reset")):
        return {"category": "NETWORK_TRANSIENT", "heal": True,
                "diagnosis": "Transient network timeout or gateway failure",
                "action": "Back off and retry the request"}
    if "WinError 32" in err or "PermissionError" in err:
        return {"category": "PERMISSION_LOCKED", "heal": True,
                "diagnosis": "File is locked by another process",
                "action": "Briefly delay and retry access"}
    return {"category": "GENERIC_ERROR", "heal": False,
            "diagnosis": f"Execution error: {err[:200]}",
            "action": "Manual inspection"}


def self_heal_diagnose(args: Dict[str, Any]) -> str:
    error_text = str(args.get("error_text", "") or "")
    if not error_text:
        return ("Self-healing engine: operational. "
                "[no incident store wired yet — pass error_text to diagnose an error]")
    d = _diagnose(error_text)
    return (f"Diagnosis:\n- Category: {d['category']}\n- {d['diagnosis']}\n"
            f"- Auto-heal available: {d['heal']}\n- Recommended: {d['action']}")


# ---------------------------------------------------------------------------
# Vision (aria.vision imports guarded at top of this module)
# ---------------------------------------------------------------------------

def describe_camera(args: Dict[str, Any]) -> str:
    img = grab_webcam()
    if not img:
        return "[camera unavailable]"
    out = describe_native(img, str(args.get("question", "") or "Describe what you see."))
    return out or "[vision describe failed]"


def read_screen(args: Dict[str, Any]) -> str:
    img = grab_screen()
    if not img:
        return "[screen capture unavailable]"
    out = describe_native(img, str(args.get("question", "") or "Read all visible text."))
    return out or "[vision describe failed]"


def take_photo(args: Dict[str, Any]) -> str:
    img = grab_webcam()
    if not img:
        return "[camera unavailable]"
    name = re.sub(r"[^\w\-.]", "-", str(args.get("name", "") or
                                        datetime.now().strftime("photo-%Y%m%d-%H%M%S"))).strip("-")
    if not name.lower().endswith(".jpg"):
        name += ".jpg"
    photo_dir = os.path.join(_workspace(), "photos")
    try:
        os.makedirs(photo_dir, exist_ok=True)
        path = os.path.join(photo_dir, name)
        with open(path, "wb") as f:
            f.write(img)
        return f"Photo saved: {path}"
    except Exception as e:
        return f"[photo save failed: {e}]"


# ---------------------------------------------------------------------------
# Scheduler task tools (shared store lives in toolkits/sched.py, the leaf
# both this module and system.py import — no circular import)
# ---------------------------------------------------------------------------

def set_recurring_task(args: Dict[str, Any]) -> str:
    try:
        interval = max(60, int(args.get("interval_seconds", 3600) or 3600))
    except Exception:
        interval = 3600
    prompt = str(args.get("prompt", "") or "").strip()
    if not prompt:
        return "[set_recurring_task needs a prompt]"
    tid = sched_add("recurring", prompt, interval_s=interval)
    return f"Recurring task set (#{tid}): '{prompt}' every {interval} seconds."


def list_scheduled_tasks(args: Dict[str, Any]) -> str:
    rows = sched_list()
    if not rows:
        return "No scheduled tasks."
    lines = []
    for r in rows:
        nxt = datetime.fromtimestamp(r.get("next_run", 0)).strftime("%H:%M:%S")
        lines.append(f"#{r['id']} [{r['kind']}] next: {nxt} | {r['payload'][:80]}")
    return "\n".join(lines)


def cancel_scheduled_task(args: Dict[str, Any]) -> str:
    try:
        tid = int(args.get("task_id", 0))
    except Exception:
        return "[cancel_scheduled_task needs a numeric task_id]"
    return f"Cancelled task #{tid}." if sched_cancel(tid) else f"[no task #{tid} found]"


# ---------------------------------------------------------------------------
# Autonomous goals (lightweight in-process)
# ---------------------------------------------------------------------------

_GOALS: Dict[int, Dict[str, Any]] = {}
_GOAL_SEQ = itertools.count(1)
_GOAL_LOCK = threading.Lock()


def manage_autonomous_goal(args: Dict[str, Any]) -> str:
    action = str(args.get("action", "list") or "list").lower().strip()
    if action == "create":
        title = str(args.get("title", "") or "").strip()
        if not title:
            return "[cannot create a goal without a title]"
        try:
            interval = int(args.get("interval_s", 0) or 0)
            priority = int(args.get("priority", 5) or 5)
        except Exception:
            interval, priority = 0, 5
        gid = next(_GOAL_SEQ)
        with _GOAL_LOCK:
            _GOALS[gid] = {"id": gid, "title": title,
                           "description": str(args.get("description", "") or title),
                           "interval_s": interval, "priority": priority,
                           "status": "active", "created": datetime.now().isoformat()}
        return f"Autonomous goal #{gid} '{title}' created (priority {priority}, interval {interval}s)."
    if action == "list":
        with _GOAL_LOCK:
            goals = list(_GOALS.values())
        if not goals:
            return "No autonomous goals registered."
        lines = []
        for g in goals:
            recur = f" (recur {g['interval_s']}s)" if g["interval_s"] else ""
            lines.append(
                f"- #{g['id']} [{g['status']}] (prio {g['priority']}) "
                f"'{g['title']}'{recur}: {g['description'][:80]}")
        return "\n".join(lines)
    if action in ("cancel", "delete"):
        try:
            gid = int(args.get("goal_id", 0))
        except Exception:
            return "[provide a goal_id to cancel]"
        with _GOAL_LOCK:
            g = _GOALS.pop(gid, None)
        return f"Goal #{gid} cancelled." if g else f"[goal #{gid} not found]"
    if action in ("complete", "done"):
        try:
            gid = int(args.get("goal_id", 0))
        except Exception:
            return "[provide a goal_id to complete]"
        with _GOAL_LOCK:
            g = _GOALS.get(gid)
            if g:
                g["status"] = "complete"
        return f"Goal #{gid} completed." if g else f"[goal #{gid} not found]"
    return f"[unknown action '{action}' — use create, list, cancel, or complete]"


# ---------------------------------------------------------------------------
# Robot hardware (pyserial, guarded; real firmware wire protocol)
#
# Firmware (arduino/aria_body/aria_body.ino) speaks, at 115200 baud:
#   P<pan>T<tilt>   e.g. b"P90T45\n"    pan 0..180, tilt 0..90
#   W<left>,<right> e.g. b"W50,-50\n"   each -100..100
#   S               e.g. b"S\n"          stop (also re-centers the head)
# One persistent connection is held open (per-command open/close resets
# most Arduinos via DTR). See aria/tools/hardware_proto.py for the
# canonical encoders, clamps, and port-matching rules (v1 parity).
# ---------------------------------------------------------------------------

_BODY_STATE = _HardwareState()
_SERIAL_CONN = None  # persistent connection; opened once, held open
_FACE_THREAD = None  # face-tracking daemon thread, started on demand


def _serial_ensure():
    """Return (conn, err). Opens and holds one persistent connection."""
    global _SERIAL_CONN
    if serial is None:
        return None, "[robot hardware not connected: pyserial not installed]"
    if _SERIAL_CONN is not None:
        try:
            if _SERIAL_CONN.is_open:
                return _SERIAL_CONN, None
        except Exception:
            pass
        _SERIAL_CONN = None
    try:
        url = _get_key("ARIA_BODY_SERIAL_URL")
        if url:
            # Virtual body (v1 parity): socket://, loop://, etc.
            conn = serial.serial_for_url(url, _BODY_BAUD, timeout=2)
        else:
            explicit = _get_key("ARIA_SERIAL_PORT")
            if explicit:
                conn = serial.Serial(explicit, _BODY_BAUD, timeout=2)
            else:
                conn = None
                if HAS_LIST_PORTS:
                    for port in _list_ports_mod.comports():
                        if _port_matches(getattr(port, "description", "") or ""):
                            conn = serial.Serial(port.device, _BODY_BAUD,
                                                 timeout=2)
                            break
                if conn is None:
                    return None, ("[robot hardware not connected: no "
                                  "Arduino/CH340/USB Serial port found]")
        _SERIAL_CONN = conn
        _BODY_STATE.connected = True
        _BODY_STATE.serial_available = True
        return conn, None
    except Exception as e:
        _BODY_STATE.connected = False
        return None, f"[robot hardware not connected: {e}]"


def _serial_write(data: bytes) -> Optional[str]:
    """Write bytes to the body. None on success, error string otherwise."""
    global _SERIAL_CONN
    conn, err = _serial_ensure()
    if err:
        return err
    try:
        conn.write(data)
        conn.flush()
        return None
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        _SERIAL_CONN = None
        _BODY_STATE.connected = False
        return f"[body command failed: {e}]"


def _auto_stop_wheels() -> None:
    _serial_write(_encode_stop())
    _BODY_STATE.note_stop()


def move_head_servos(args: Dict[str, Any]) -> str:
    try:
        pan = int(args.get("pan", 90))
        tilt = int(args.get("tilt", 45))
    except Exception:
        return "[move_head_servos needs numeric pan/tilt]"
    pan, tilt = _clamp_servo(pan, tilt)
    err = _serial_write(_encode_servo(pan, tilt))
    if err:
        return err
    _BODY_STATE.note_servo(pan, tilt)
    return f"Head servos → pan {pan}, tilt {tilt}."


def drive_wheels(args: Dict[str, Any]) -> str:
    try:
        left = int(args.get("left", 0))
        right = int(args.get("right", 0))
        seconds = max(0.0, float(args.get("seconds", 0) or 0))
    except Exception:
        return "[drive_wheels needs numeric left/right/seconds]"
    left, right = _clamp_wheels(left, right)
    err = _serial_write(_encode_drive(left, right))
    if err:
        return err
    _BODY_STATE.note_drive(left, right)
    if seconds > 0:
        # The firmware has no seconds parameter — auto-stop is client-side
        # (v1 tool_drive parity). Without this the body drives forever.
        secs = max(0.1, min(30.0, seconds))
        t = threading.Timer(secs, _auto_stop_wheels)
        t.daemon = True
        t.start()
        return f"Driving ({left}, {right}) for {secs:g}s — auto-stop armed."
    return f"Driving ({left}, {right})."


def body_stop(args: Dict[str, Any]) -> str:
    err = _serial_write(_encode_stop())
    if err:
        return err
    _BODY_STATE.note_stop()
    return "Body stopped; head re-centered."


def body_status(args: Dict[str, Any]) -> str:
    """Report robot-body connection state (v1 get_hardware_status parity)."""
    s = _BODY_STATE.snapshot()
    wheels = s.get("wheels", {})
    lines = [
        f"connected: {'yes' if s.get('connected') else 'no'}",
        f"head: pan {s.get('pan')}, tilt {s.get('tilt')}",
        f"wheels: left {wheels.get('left')}, right {wheels.get('right')}",
        f"pyserial: {'installed' if HAS_SERIAL else 'not installed'}",
    ]
    return "Body status — " + "; ".join(lines) + "."


def _face_move_head(pan: int, tilt: int) -> None:
    """face_track callback: servo bytes to the body. Never raises."""
    try:
        _serial_write(_encode_servo(*_clamp_servo(pan, tilt)))
    except Exception:
        pass


def face_tracking(args: Dict[str, Any]) -> str:
    global _FACE_THREAD
    on = args.get("on", True)
    on = bool(on) if isinstance(on, bool) else str(on).lower() in ("1", "true", "on", "yes")
    if not on:
        if _face_track_mod is not None:
            _face_track_mod.set_face_tracking(False)
        return "Face tracking off."
    if serial is None:
        return "[robot hardware not connected: pyserial not installed]"
    if not HAS_FACE_TRACK:
        return "[face_tracking unavailable: vision/face_track not available]"
    conn, err = _serial_ensure()
    if err:
        return err
    _face_track_mod.set_face_tracking(True)
    if _FACE_THREAD is None or not _FACE_THREAD.is_alive():
        _FACE_THREAD = _face_track_mod.start_face_track_thread(
            move_head_fn=_face_move_head)
    return "Face tracking on — the neck servos will follow faces."


# ---------------------------------------------------------------------------
# Bridge token / commands panel / toolkits
# ---------------------------------------------------------------------------

def bridge_token(args: Dict[str, Any]) -> str:
    tok = _get_key("BRIDGE_TOKEN")
    if not tok:
        return "[no bridge token configured]"
    return "Your bridge token is set. Enter it on the phone bridge login page."


# Provider key env-var names, in chain order (aria/core/config.py parity).
_PROVIDER_KEY_ENV = {
    "ollama_cloud": "OLLAMA_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _mask_key(k: str) -> str:
    k = (k or "").strip()
    if len(k) <= 8:
        return "****"
    return k[:4] + "..." + k[-2:]


def provider_keys(args: Dict[str, Any]) -> str:
    """Manage provider API keys: masked status, or add one.

    v1's gemini_keys tool, generalized to the whole failover chain.
    Keys are stored in ~/workspace/aria-v2/aria_keys.json (gitignored,
    never leaves the machine). Status output is masked.
    """
    a = (args or {})
    action = str(a.get("action", "status")).lower().strip()
    if action == "status":
        lines = []
        for provider, env_name in _PROVIDER_KEY_ENV.items():
            k = _get_key(env_name)
            state = f"set ({_mask_key(k)})" if k else "not set"
            lines.append(f"  {provider}: {state}")
        return "Provider API keys:\n" + "\n".join(lines)
    if action == "add":
        provider = str(a.get("provider", "")).lower().strip()
        key = str(a.get("key", "")).strip()
        if provider not in _PROVIDER_KEY_ENV:
            return ("Unknown provider '%s'. Choose one of: %s."
                    % (provider, ", ".join(_PROVIDER_KEY_ENV)))
        if len(key) < 12:
            return "That key looks too short (need 12+ characters). Not added."
        _set_key(_PROVIDER_KEY_ENV[provider], key)
        return (f"Added {provider} key ({_mask_key(key)}). "
                "It joins the failover chain on the next turn.")
    return f"Unknown action '{action}'. Use 'status' or 'add'."


def _grouped_commands() -> str:
    groups = [
        ("Memory", ["save_memory", "search_memory", "forget_memory", "journal_write"]),
        ("Web", ["web_search", "fetch_url", "run_skill"]),
        ("Laptop", ["open_app_or_url", "launch_app", "gui_click", "gui_type",
                    "list_windows", "focus_window", "minimize_window", "close_window",
                    "window_snap", "clipboard_read", "clipboard_write", "media_key",
                    "volume", "run_python_code", "write_file", "read_file",
                    "list_workspace", "find_file", "file_organize",
                    "file_find_advanced", "file_duplicates", "disk_usage"]),
        ("Seeing", ["take_screenshot", "read_screen", "take_photo", "describe_camera",
                    "watch_screen", "list_screen_watches"]),
        ("Time", ["get_time", "set_timer", "set_reminder", "set_recurring_task",
                  "list_scheduled_tasks", "cancel_scheduled_task", "morning_briefing",
                  "break_reminders", "calendar_setup", "check_calendar"]),
        ("Autonomy", ["manage_autonomous_goal", "manage_background_job",
                      "system_health_audit", "self_heal_diagnose"]),
        ("Music", ["spotify", "dj"]),
        ("Notes", ["take_note", "read_notes"]),
        ("MTG", ["mtg_card", "mtg_advice"]),
        ("Prices", ["watch_price", "list_price_watches", "unwatch_price",
                    "check_price_watches"]),
        ("Face & body", ["face_tracking", "move_head_servos", "drive_wheels", "body_stop",
                      "body_status"]),
        ("Phone & keys", ["bridge_token"]),
        ("GitHub", ["github_setup", "github_push_file", "github_create_repo"]),
        ("Email", ["gmail_setup", "send_email", "read_email", "triage_email"]),
        ("MCP", ["mcp_setup", "mcp_connect", "mcp_disconnect", "mcp_list_servers",
                 "mcp_remove_server"]),
        ("Routines", ["routine_record_start", "routine_record_stop", "run_routine",
                      "list_routines", "describe_routine", "trust_routine"]),
        ("Safety", ["approve", "deny", "list_pending_approvals", "set_approval_mode",
                    "get_approval_mode"]),
        ("Persona", ["set_persona", "list_personas", "get_persona"]),
    ]
    reg = _REGISTRY_REF
    schemas = getattr(reg, "schemas", {}) if reg else {}
    out = ["Commands:"]
    for label, names in groups:
        rows = []
        for n in names:
            d = (schemas.get(n) or {}).get("description", "")
            rows.append(f"  • {n}" + (f" — {d}" if d else ""))
        out.append(f"\n{label}:\n" + "\n".join(rows))
    return "\n".join(out)


def show_commands(args: Dict[str, Any]) -> str:
    return _grouped_commands()


def hide_commands(args: Dict[str, Any]) -> str:
    return "Commands panel hidden."


_TOOLKIT_CATALOG = {
    "core": ("everyday tools: web search, open apps/URLs, run Python code, click/type, "
             "screenshots, screen reading, clipboard, files, memory save/search, skills",
             ["web_search", "open_app_or_url", "run_python_code", "gui_click", "gui_type",
              "save_memory", "search_memory", "take_screenshot", "read_screen", "fetch_url",
              "clipboard_read", "clipboard_write", "find_file", "read_file", "write_file",
              "list_workspace", "list_files", "run_skill"]),
    "files": ("file commander: organize a folder, advanced find, duplicate detection, disk usage",
              ["file_organize", "file_find_advanced", "file_duplicates", "disk_usage"]),
    "system": ("time, timers, reminders, volume, window control, background jobs, health",
               ["get_time", "set_timer", "set_reminder", "volume", "media_key", "list_windows",
                "focus_window", "minimize_window", "close_window", "window_snap", "launch_app",
                "gui_click", "gui_type", "take_screenshot", "clipboard_read", "clipboard_write",
                "system_health_audit", "manage_background_job", "break_reminders",
                "morning_briefing", "watch_screen", "unwatch_screen", "list_screen_watches"]),
    "memory": ("notes, journal, forgetting memories", ["forget_memory", "journal_write",
                                                       "take_note", "read_notes"]),
    "comms": ("Gmail: store credentials, send and read email, triage the inbox",
              ["gmail_setup", "send_email", "read_email", "triage_email"]),
    "github": ("save a token, push files, and create GitHub repos",
              ["github_setup", "github_push_file", "github_create_repo"]),
    "mcp": ("Model Context Protocol servers", ["mcp_setup", "mcp_connect", "mcp_disconnect",
                                               "mcp_list_servers", "mcp_remove_server"]),
    "mtg": ("Magic card lookup, Commander deck advice, price watches",
            ["mtg_card", "mtg_advice", "watch_price", "list_price_watches",
             "unwatch_price", "check_price_watches"]),
    "scheduler": ("reminders, spoken timers, recurring tasks, break nudges, live calendar",
                  ["set_reminder", "set_recurring_task", "list_scheduled_tasks",
                   "cancel_scheduled_task", "set_timer", "break_reminders",
                   "calendar_setup", "check_calendar"]),
    "spotify": ("music: Spotify control, DJ mode, media keys", ["spotify", "dj", "media_key"]),
    "vision": ("webcam photos, camera descriptions, face tracking, neck servos",
               ["take_photo", "describe_camera", "face_tracking", "move_head_servos",
                "drive_wheels", "body_stop", "body_status"]),
    "windows": ("list, focus, minimize, close, snap windows; launch apps",
                ["list_windows", "focus_window", "minimize_window", "close_window",
                 "window_snap", "launch_app"]),
    "routines": ("record and replay multi-step routines",
                 ["routine_record_start", "routine_record_stop", "run_routine",
                  "list_routines", "delete_routine", "describe_routine",
                  "trust_routine", "untrust_routine"]),
    "autonomy": ("autonomous goals, background jobs, health audit, self-healing",
                 ["manage_autonomous_goal", "manage_background_job",
                  "system_health_audit", "self_heal_diagnose"]),
    "admin": ("bridge token, command guide, approval controls, personas",
              ["bridge_token", "show_commands", "hide_commands", "approve", "deny",
               "list_pending_approvals", "set_approval_mode", "get_approval_mode",
               "set_persona", "list_personas", "get_persona", "reload_user_tools"]),
}

_LOADED_TOOLKITS = {"core"}


def load_toolkit(args: Dict[str, Any]) -> str:
    name = str(args.get("toolkit", "") or "").strip().lower()
    if name not in _TOOLKIT_CATALOG:
        avail = ", ".join(sorted(_TOOLKIT_CATALOG))
        return f"[unknown toolkit '{name}' — available: {avail}]"
    _LOADED_TOOLKITS.add(name)
    summary, tools = _TOOLKIT_CATALOG[name]
    return (f"Toolkit '{name}' loaded — {summary}. Tools: {', '.join(tools)}. "
            "Use them on the next turn.")


# ---------------------------------------------------------------------------
# Routines (record / replay named multi-step automations)
# ---------------------------------------------------------------------------

def _routines_path() -> str:
    return os.path.join(_workspace(), "routines.json")


def _load_routines() -> Dict[str, Any]:
    try:
        with open(_routines_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_routines(routines: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=_workspace(), prefix=".routines-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(routines, f, indent=2)
        os.replace(tmp, _routines_path())
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def routine_record_start(args: Dict[str, Any]) -> str:
    name = str(args.get("name", "") or "").strip()
    if not name:
        return "[routine_record_start needs a name]"
    if _REGISTRY_REF is None:
        return "[routines unavailable: no registry]"
    _REGISTRY_REF.start_recording(name)
    return (f"Recording routine '{name}' — every tool you call now is captured. "
            "Call routine_record_stop when done.")


def routine_record_stop(args: Dict[str, Any]) -> str:
    if _REGISTRY_REF is None:
        return "[routines unavailable: no registry]"
    name, steps = _REGISTRY_REF.stop_recording()
    if not name:
        return "[no routine is being recorded]"
    routines = _load_routines()
    routines[name] = {"steps": steps, "trusted": False,
                      "created": datetime.now().isoformat()}
    _save_routines(routines)
    return f"Routine '{name}' saved with {len(steps)} step(s)."


def _substitute(value: str, params: Dict[str, Any]) -> str:
    for k, v in (params or {}).items():
        value = value.replace("{{" + str(k) + "}}", str(v))
    return value


def run_routine(args: Dict[str, Any]) -> str:
    name = str(args.get("name", "") or "").strip()
    routines = _load_routines()
    r = routines.get(name)
    if not r:
        return f"[no routine named '{name}']"
    steps = r.get("steps", [])
    if _REGISTRY_REF is None:
        return "[routines unavailable: no registry]"
    unknown = preflight(steps, _REGISTRY_REF)
    if unknown:
        return (f"[routine '{name}' references unknown tools: {', '.join(unknown)} — "
                "fix or delete the routine]")
    try:
        params = json.loads(str(args.get("params_json", "") or "{}"))
        if not isinstance(params, dict):
            params = {}
    except Exception:
        params = {}
    outs = []
    for i, step in enumerate(steps[:50]):
        tool = step.get("tool", "")
        sargs = {k: (_substitute(v, params) if isinstance(v, str) else v)
                 for k, v in (step.get("args") or {}).items()}
        try:
            out = _REGISTRY_REF.dispatch(tool, sargs)
        except (ToolRepairable, ToolFailed, LoopGuardTripped) as e:
            return f"[routine '{name}' stopped at step {i + 1} ({tool}): {e}]"
        except KeyError as e:
            return f"[routine '{name}' stopped at step {i + 1} ({tool}): {e}]"
        outs.append(f"[{tool}] {str(out)[:400]}")
    return f"Routine '{name}' done ({len(outs)} steps):\n" + "\n".join(outs)


def list_routines(args: Dict[str, Any]) -> str:
    routines = _load_routines()
    if not routines:
        return "No saved routines."
    return "\n".join(
        f"- {n} ({len(r.get('steps', []))} steps)"
        f"{' [trusted]' if r.get('trusted') else ''}" for n, r in routines.items())


def delete_routine(args: Dict[str, Any]) -> str:
    name = str(args.get("name", "") or "").strip()
    routines = _load_routines()
    if name not in routines:
        return f"[no routine named '{name}']"
    del routines[name]
    _save_routines(routines)
    return f"Routine '{name}' deleted."


def describe_routine(args: Dict[str, Any]) -> str:
    name = str(args.get("name", "") or "").strip()
    r = _load_routines().get(name)
    if not r:
        return f"[no routine named '{name}']"
    lines = [f"Routine '{name}' ({len(r.get('steps', []))} steps):"]
    for i, s in enumerate(r.get("steps", []), 1):
        lines.append(f"  {i}. {s.get('tool')}({s.get('args')})")
    return "\n".join(lines)


def trust_routine(args: Dict[str, Any]) -> str:
    name = str(args.get("name", "") or "").strip()
    routines = _load_routines()
    if name not in routines:
        return f"[no routine named '{name}']"
    routines[name]["trusted"] = True
    _save_routines(routines)
    return f"Routine '{name}' trusted: its steps replay without per-step approval."


def untrust_routine(args: Dict[str, Any]) -> str:
    name = str(args.get("name", "") or "").strip()
    routines = _load_routines()
    if name not in routines:
        return f"[no routine named '{name}']"
    routines[name]["trusted"] = False
    _save_routines(routines)
    return f"Routine '{name}' untrusted."


# ---------------------------------------------------------------------------
# Personas + user tools
# ---------------------------------------------------------------------------

_PERSONAS = {
    "concise": "Terse, no flair. Short sentences. No small talk.",
    "coach": "Direct, pushy, accountability-first. Calls out excuses.",
}
_ACTIVE_PERSONA = "none"


def set_persona(args: Dict[str, Any]) -> str:
    global _ACTIVE_PERSONA
    name = str(args.get("name", "") or "").strip().lower()
    if name == "none":
        _ACTIVE_PERSONA = "none"
        return "Persona cleared."
    if name not in _PERSONAS:
        return f"[unknown persona '{name}' — available: {', '.join(_PERSONAS)}]"
    _ACTIVE_PERSONA = name
    return f"Persona set to '{name}': {_PERSONAS[name]}"


def list_personas(args: Dict[str, Any]) -> str:
    return "Personas: " + ", ".join(f"{k} ({v})" for k, v in _PERSONAS.items())


def get_persona(args: Dict[str, Any]) -> str:
    return f"Active persona: {_ACTIVE_PERSONA}."


def reload_user_tools(args: Dict[str, Any]) -> str:
    d = os.path.join(_workspace(), "user_tools")
    os.makedirs(d, exist_ok=True)
    mods = sorted(f for f in os.listdir(d) if f.endswith(".py") and not f.startswith("_"))
    if not mods:
        return "[no user tools found in workspace/user_tools/]"
    return "User tools found (not auto-loaded in v2):\n" + "\n".join(f"- {m}" for m in mods)


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------

def _schema(name: str, description: str, properties: Dict[str, Any] = None,
            required: List[str] = None) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object",
                           "properties": properties or {},
                           "required": required or []}}


_S = lambda t, d="": {"type": t, **({"description": d} if d else {})}

SCHEMAS = [
    _schema("fetch_url", "Fetch a web page and return its readable text (scripts/styles stripped).",
            {"url": _S("string")}, ["url"]),
    _schema("web_search", "Searches the live web for facts, docs, news, or answers.",
            {"query": _S("string")}, ["query"]),
    _schema("gmail_setup", "Saves your Gmail address and app password. Call after the user gives you both.",
            {"gmail_user": _S("string"), "app_password": _S("string")}, ["gmail_user", "app_password"]),
    _schema("send_email", "Sends an email through your Gmail. Needs gmail_setup first.",
            {"to": _S("string"), "subject": _S("string"), "body": _S("string")},
            ["to", "subject", "body"]),
    _schema("read_email", "Reads Gmail over IMAP (read-only, never marks read). Without uid: newest matching summaries with uids. With uid: full body.",
            {"query": _S("string"), "limit": _S("integer"), "unread_only": _S("boolean"), "uid": _S("string")}),
    _schema("triage_email", "Triages the Gmail inbox: classifies recent messages into IMPORTANT / FYI / NOISE. Needs gmail_setup first.",
            {"limit": _S("integer")}),
    _schema("calendar_setup", "Saves the Google Calendar secret iCal URL so ARIA can read the live schedule.",
            {"ical_url": _S("string")}, ["ical_url"]),
    _schema("check_calendar", "Reads upcoming events from the live iCal calendar feed (needs calendar_setup first). days: 1-14.",
            {"days": _S("integer")}),
    _schema("mtg_card", "Look up a Magic: The Gathering card on Scryfall — rules text, type, mana cost, market price.",
            {"card_name": _S("string")}, ["card_name"]),
    _schema("mtg_advice", "Commander deck advice: fetches the card from Scryfall with EDHREC rank, in the context of the named deck.",
            {"deck": _S("string"), "card_name": _S("string")}, ["deck", "card_name"]),
    _schema("watch_price", "Watch a product URL and alert when its price drops at or below target_price.",
            {"url": _S("string"), "target_price": _S("string"), "label": _S("string")},
            ["url", "target_price"]),
    _schema("unwatch_price", "Remove a price watch by its #id.",
            {"watch_id": _S("integer")}, ["watch_id"]),
    _schema("list_price_watches", "List active price watches."),
    _schema("check_price_watches", "Runs one price-watch check pass now (alerts on drops at/below target)."),
    _schema("inbox_list", "Lists files uploaded to the inbox."),
    _schema("inbox_read", "Reads a text file from the inbox. Blank name reads the latest text file.",
            {"name": _S("string")}),
    _schema("inbox_describe", "Describes an image in the inbox with vision.",
            {"name": _S("string")}, ["name"]),
    _schema("spotify", "Controls Spotify: media keys (play_pause, next, previous, stop, mute, volume_up/down); open/search/play/play_uri open the desktop app.",
            {"action": _S("string"), "query": _S("string")}, ["action"]),
    _schema("dj", "DJ mode: plays a Spotify playlist from a natural request. Needs Spotify connected.",
            {"request": _S("string")}, ["request"]),
    _schema("github_setup", "Saves your GitHub personal access token (repo scope) for github_push_file / github_create_repo.",
            {"token": _S("string")}, ["token"]),
    _schema("github_create_repo", "Creates a new GitHub repository under your account.",
            {"name": _S("string"), "description": _S("string"), "private": _S("boolean")}, ["name"]),
    _schema("github_push_file", "Creates/updates a file in a GitHub repo.",
            {"repo": _S("string"), "filepath": _S("string"), "content": _S("string"), "message": _S("string")},
            ["repo", "filepath"]),
    _schema("mcp_setup", "Adds an MCP server ARIA can use. transport: stdio (command), sse, or http (url); omit to infer from command/url.",
            {"name": _S("string"), "transport": _S("string"), "command": _S("string"),
             "args": _S("string"), "url": _S("string"), "env": _S("string"),
             "headers": _S("string")}, ["name"]),
    _schema("mcp_connect", "Connects to MCP server(s) and loads their tools. Omit name for all.",
            {"name": _S("string")}),
    _schema("mcp_disconnect", "Disconnects an MCP server (or all).",
            {"name": _S("string")}),
    _schema("mcp_list_servers", "Lists configured MCP servers with connection status."),
    _schema("mcp_remove_server", "Removes an MCP server configuration entirely.",
            {"name": _S("string")}, ["name"]),
    _schema("run_skill", "Run a bounded workflow skill: deep_research, system_check, file_sweep, git_audit.",
            {"skill_name": _S("string"), "objective": _S("string")}, ["skill_name", "objective"]),
    _schema("run_python_code", "Executes Python code in a sandboxed subprocess (timeout + truncation).",
            {"code": _S("string")}, ["code"]),
    _schema("self_heal_diagnose", "Diagnoses an operational error: category, auto-heal availability, recommended action.",
            {"error_text": _S("string"), "context": _S("string")}),
    _schema("describe_camera", "Looks through the webcam right now and describes what it sees.",
            {"question": _S("string")}),
    _schema("read_screen", "Captures the screen and reads it with vision — answers a question about what is shown.",
            {"question": _S("string")}),
    _schema("take_photo", "Saves a webcam photo to the workspace photos folder and returns its path.",
            {"name": _S("string")}),
    _schema("set_recurring_task", "Runs a prompt every interval_seconds (min 60), autonomously.",
            {"interval_seconds": _S("integer"), "prompt": _S("string")},
            ["interval_seconds", "prompt"]),
    _schema("list_scheduled_tasks", "Lists all scheduled reminders and recurring tasks."),
    _schema("cancel_scheduled_task", "Cancels a scheduled task by its #id.",
            {"task_id": _S("integer")}, ["task_id"]),
    _schema("manage_autonomous_goal", "Manages persistent autonomous background goals. Actions: create, list, cancel, complete.",
            {"action": _S("string"), "title": _S("string"), "description": _S("string"),
             "goal_id": _S("integer"), "interval_s": _S("integer"), "priority": _S("integer")},
            ["action"]),
    _schema("move_head_servos", "Rotates physical robot neck servos (Pan 0-180, Tilt 0-90).",
            {"pan": _S("integer"), "tilt": _S("integer")}),
    _schema("drive_wheels", "Drives the robot body wheels. left/right -100..100; seconds > 0 auto-stops.",
            {"left": _S("integer"), "right": _S("integer"), "seconds": _S("number")}),
    _schema("body_stop", "Stops the robot body wheels and centers the head."),
    _schema("body_status", "Reports robot-body connection state: link, head position, wheels."),
    _schema("face_tracking", "Turn camera face-tracking on/off.",
            {"on": _S("boolean")}),
    _schema("bridge_token", "Shows the phone-bridge login token status."),
    _schema("provider_keys", "Manage provider API keys: 'status' shows masked key state per provider; 'add' stores a key.",
            {"action": _S("string"), "provider": _S("string"), "key": _S("string")}),
    _schema("show_commands", "Shows the on-screen commands reference: every tool, grouped, with descriptions."),
    _schema("hide_commands", "Hides the on-screen commands reference panel."),
    _schema("load_toolkit", "Unlock a toolkit's tools. Call with a toolkit name, then use its tools on the next turn.",
            {"toolkit": _S("string")}, ["toolkit"]),
    _schema("routine_record_start", "Starts recording a named routine: every tool called afterwards is captured as a replayable step.",
            {"name": _S("string")}, ["name"]),
    _schema("routine_record_stop", "Stops routine recording and saves the routine."),
    _schema("run_routine", "Replays a saved routine by name. params_json supplies {{param}} values.",
            {"name": _S("string"), "params_json": _S("string")}, ["name"]),
    _schema("list_routines", "Lists saved routines with step counts and trust status."),
    _schema("delete_routine", "Deletes a saved routine.",
            {"name": _S("string")}, ["name"]),
    _schema("describe_routine", "Shows the steps of a saved routine in plain language.",
            {"name": _S("string")}, ["name"]),
    _schema("trust_routine", "Marks a routine trusted: its steps replay without per-step approval.",
            {"name": _S("string")}, ["name"]),
    _schema("untrust_routine", "Removes trusted status from a routine.",
            {"name": _S("string")}, ["name"]),
    _schema("set_persona", "Switches persona overlay: concise, coach, or none to clear.",
            {"name": _S("string")}, ["name"]),
    _schema("list_personas", "Lists available persona overlays."),
    _schema("get_persona", "Reports the currently active persona overlay."),
    _schema("reload_user_tools", "Re-scans the user-tools folder and reports custom tools found."),
]

_FUNCS = [
    fetch_url, web_search, gmail_setup, send_email, read_email, triage_email,
    calendar_setup, check_calendar, mtg_card, mtg_advice, watch_price,
    unwatch_price, list_price_watches, check_price_watches, inbox_list,
    inbox_read, inbox_describe, spotify, dj, github_setup, github_create_repo,
    github_push_file, mcp_setup, mcp_connect, mcp_disconnect,
    mcp_list_servers, mcp_remove_server, run_skill, run_python_code,
    self_heal_diagnose, describe_camera, read_screen, take_photo,
    set_recurring_task, list_scheduled_tasks, cancel_scheduled_task,
    manage_autonomous_goal, move_head_servos, drive_wheels, body_stop,
    body_status, face_tracking, bridge_token, provider_keys, show_commands, hide_commands, load_toolkit,
    routine_record_start, routine_record_stop, run_routine,
    list_routines, delete_routine, describe_routine, trust_routine,
    untrust_routine, set_persona, list_personas, get_persona,
    reload_user_tools,
]

assert len(SCHEMAS) == len(_FUNCS), f"schema/func mismatch: {len(SCHEMAS)} vs {len(_FUNCS)}"


def register(registry) -> None:
    global _REGISTRY_REF
    _REGISTRY_REF = registry
    set_bridge_registry(registry)  # dynamic mcp_<server>__<tool> registration
    seen = set()
    for schema, func in zip(SCHEMAS, _FUNCS):
        name = schema["name"]
        assert name not in seen, f"duplicate registration: {name}"
        seen.add(name)
        registry.register(name, func, schema)
