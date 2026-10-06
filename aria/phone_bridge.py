"""ARIA v2 phone bridge: HTTPS server for phone control + voice + dashboard.

Ported from v1 `aria/bridge.py` (substance, not a copy): the bridge exposes
the agent to a phone on the LAN — text directives (/api/ask), voice
messages (/api/voice), TTS playback (/api/say), a live dashboard (/),
command reference (/commands), file upload (/upload), and MJPEG streams.

v2 adaptations:
- No dependency on v1's `aria.config`. Port/token/host come from env vars or
  `~/workspace/aria-v2/aria_keys.json`; logging goes through an optional
  hook (default: plain print).
- Vision/speech/inbox siblings are guarded via `aria.core.optimport` at
  column 0. Endpoints whose producer does not exist in this build answer
  `503 [unavailable: ...]` instead of crashing — the bridge stays up.
- `ensure_bridge_cert()` returns a status string describing which cert path
  was taken (per-machine / bundled-shared / http-fallback).
- No auto-start: `main.py` does NOT start the bridge. Start it explicitly
  via the `phone_bridge_start` tool (registered by `register()` below) or
  `start_bridge_server(blocking=False)` from the agent loop.

Auth: every route requires the bridge token (header `X-Bridge-Token`,
`aria_bridge_token` HttpOnly cookie, or legacy `?token=` on first visit).
"""

import base64
import datetime as _dt
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import wave
from datetime import datetime
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from aria.core.optimport import optional_attr as _optional_attr
from aria.core.optimport import optional_module as _optional_module

# ---------------------------------------------------------------------------
# Guarded sibling subsystems — all at column 0 with HAS_* flags.
# Absent producers degrade to honest 503s; the bridge itself never crashes.
# ---------------------------------------------------------------------------

_get_face_frame_jpeg = _optional_attr("aria.vision", "get_face_frame_jpeg")
_publish_phone_frame = _optional_attr("aria.vision", "publish_phone_frame")
_get_phone_frame_jpeg = _optional_attr("aria.vision", "get_phone_frame_jpeg")
_get_phone_frame_status = _optional_attr("aria.vision", "get_phone_frame_status")
_describe_phone_view = _optional_attr("aria.vision", "describe_phone_view")
HAS_VISION_STREAM = _get_face_frame_jpeg is not None
HAS_PHONE_CAM = _get_phone_frame_jpeg is not None

_tts_bytes_for_bridge = _optional_attr("aria.speech", "tts_bytes_for_bridge")
_transcribe_audio = _optional_attr("aria.speech", "transcribe_audio")
HAS_BRIDGE_TTS = _tts_bytes_for_bridge is not None
HAS_BRIDGE_STT = _transcribe_audio is not None

_COMMAND_GUIDE = _optional_attr("aria.tools.schemas", "COMMAND_GUIDE")

_crypto_x509 = _optional_module("cryptography.x509")
_crypto_NameOID = _optional_attr("cryptography.x509.oid", "NameOID")
_crypto_hashes = _optional_module("cryptography.hazmat.primitives.hashes")
_crypto_serial = _optional_module("cryptography.hazmat.primitives.serialization")
_crypto_rsa = _optional_module("cryptography.hazmat.primitives.asymmetric.rsa")
HAS_CRYPTOGRAPHY = all(
    m is not None
    for m in (_crypto_x509, _crypto_NameOID, _crypto_hashes,
              _crypto_serial, _crypto_rsa)
)

# ---------------------------------------------------------------------------
# Bridge settings (env first, then aria_keys.json; never credentials in code)
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_REPO_ROOT, "data")
_KEYS_FILE = os.path.expanduser("~/workspace/aria-v2/aria_keys.json")

PHONE_BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8777"))
BRIDGE_BIND_HOST = os.environ.get("BRIDGE_BIND_HOST", "0.0.0.0")

BRIDGE_SCHEME = "http"
BRIDGE_CERT: Optional[str] = None
BRIDGE_KEY: Optional[str] = None

_BRIDGE_PROCESS_CALL: Optional[Callable[[str, bool], str]] = None
_CHAT_LOG_CALL: Optional[Callable[[], Any]] = None
_BRIDGE_SERVER: Optional[ThreadingHTTPServer] = None
_BRIDGE_THREAD: Optional[threading.Thread] = None
_LOG_FN: Optional[Callable[[str], None]] = None

_TOKEN_CACHE: Optional[str] = None


def _load_keys_file() -> Dict[str, str]:
    try:
        with open(_KEYS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def bridge_token() -> str:
    """Bridge auth token: BRIDGE_TOKEN env, else aria_keys.json. Cached."""
    global _TOKEN_CACHE
    if _TOKEN_CACHE is None:
        tok = (os.environ.get("BRIDGE_TOKEN", "") or "").strip()
        if not tok:
            tok = (_load_keys_file().get("BRIDGE_TOKEN", "") or "").strip()
        _TOKEN_CACHE = "" if tok.upper() == "INSERT" else tok
    return _TOKEN_CACHE


def set_bridge_processor(fn: Callable[[str, bool], str]):
    """Wire the agent turn: fn(text, from_phone) -> reply string."""
    global _BRIDGE_PROCESS_CALL
    _BRIDGE_PROCESS_CALL = fn


def set_chat_log_provider(fn: Callable[[], Any]):
    """Wire the chat log shown on the dashboard: fn() -> rows."""
    global _CHAT_LOG_CALL
    _CHAT_LOG_CALL = fn


def set_bridge_logger(fn: Callable[[str], None]):
    """Route bridge log lines somewhere other than stdout."""
    global _LOG_FN
    _LOG_FN = fn


def _log(msg: str) -> None:
    if _LOG_FN is not None:
        try:
            _LOG_FN(msg)
        except Exception:
            pass
    else:
        print(f"[bridge] {msg}", flush=True)


def lan_ip() -> str:
    """Best-effort LAN IP for the phone URL. Never raises."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


def _generate_silent_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(8000)
        wf.writeframes(b"\x80" * 8000)
    return buf.getvalue()


_SILENT_WAV_BYTES = _generate_silent_wav()

def _der_len(n: int) -> bytes:
    if n < 128:
        return bytes((n,))
    lb = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(lb),)) + lb


def _der_int(raw: bytes) -> bytes:
    raw = raw.lstrip(b"\x00") or b"\x00"
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return b"\x02" + _der_len(len(raw)) + raw


def _der_read_len(buf: bytes, pos: int) -> tuple[int, int]:
    first = buf[pos]
    if first < 128:
        return first, pos + 1
    n = first & 0x7F
    return int.from_bytes(buf[pos + 1:pos + 1 + n], "big"), pos + 1 + n


def _der_read_seq_of_ints(der: bytes) -> list[int]:
    """Parse DER SEQUENCE of INTEGERs; raises on anything malformed."""
    pos = 0
    if der[pos] != 0x30:
        raise ValueError("DER: not a SEQUENCE")
    ln, pos = _der_read_len(der, pos + 1)
    end = pos + ln
    out = []
    while pos < end:
        if der[pos] != 0x02:
            raise ValueError("DER: expected INTEGER")
        iln, pos = _der_read_len(der, pos + 1)
        out.append(int.from_bytes(der[pos:pos + iln], "big"))
        pos += iln
    if pos != end:
        raise ValueError("DER: trailing bytes")
    return out


def _pem_wrap(der: bytes, label: str) -> bytes:
    b64 = base64.b64encode(der).decode("ascii")
    lines = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n".encode("ascii")


# Windows-only: build the self-signed cert with PowerShell/.NET so a broken
# `cryptography` install can't block per-machine cert generation. Emits 9
# base64 lines on stdout: cert DER, then RSA n/e/d/p/q/dp/dq/qinv.
# NOTE: -DnsName already creates the SAN extension; do NOT also pass
# -TextExtension with OID 2.5.29.17 (duplicate extension -> cmdlet throws).


_PS_CERT_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$cert=New-SelfSignedCertificate -DnsName 'aria-bridge','localhost' "
    "-CertStoreLocation 'Cert:\\CurrentUser\\My' -KeyExportPolicy Exportable "
    "-KeyLength 2048 -HashAlgorithm SHA256 -NotAfter (Get-Date).AddYears(10);"
    "try{"
    "[Convert]::ToBase64String($cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert));"
    "$p=[System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($cert).ExportParameters($true);"
    "[Convert]::ToBase64String($p.Modulus);"
    "[Convert]::ToBase64String($p.Exponent);"
    "[Convert]::ToBase64String($p.D);"
    "[Convert]::ToBase64String($p.P);"
    "[Convert]::ToBase64String($p.Q);"
    "[Convert]::ToBase64String($p.DP);"
    "[Convert]::ToBase64String($p.DQ);"
    "[Convert]::ToBase64String($p.InverseQ)"
    "}finally{"
    "try{"
    "$s=New-Object System.Security.Cryptography.X509Certificates.X509Store('My','CurrentUser');"
    "$s.Open('ReadWrite');$s.Remove($cert);$s.Close()"
    "}catch{}}"
)


def _generate_machine_cert_powershell(cert_p: str, key_p: str) -> bool:
    """Windows-only fallback: self-signed cert via PowerShell/.NET.

    Used when `cryptography` can't be imported (e.g. its native DLLs fail to
    load). Needs no third-party packages: New-SelfSignedCertificate ships with
    Windows 10/11. Raises on any problem; caller falls back to the bundled cert.
    """
    if sys.platform != "win32":
        return False
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", _PS_CERT_SCRIPT],
        capture_output=True, text=True, timeout=90,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr.strip() or f"powershell exit {r.returncode}")[-300:])
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if len(lines) < 9:
        raise RuntimeError(f"unexpected powershell output ({len(lines)} lines)")
    cert_der = base64.b64decode(lines[-9])
    nums = [int.from_bytes(base64.b64decode(x), "big") for x in lines[-8:]]
    body = b"".join([_der_int(b"\x00")] +
                     [_der_int(n.to_bytes((n.bit_length() + 7) // 8 or 1, "big"))
                      for n in nums])
    key_der = b"\x30" + _der_len(len(body)) + body
    # Round-trip check: the DER must decode to version 0 + the 8 inputs.
    if _der_read_seq_of_ints(key_der) != [0] + nums:
        raise RuntimeError("generated key failed DER round-trip check")
    if not cert_der.startswith(b"\x30"):
        raise RuntimeError("generated cert is not DER")
    with open(cert_p, "wb") as f:
        f.write(_pem_wrap(cert_der, "CERTIFICATE"))
    with open(key_p, "wb") as f:
        f.write(_pem_wrap(key_der, "RSA PRIVATE KEY"))
    return True


_BRIDGE_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIDNjCCAh6gAwIBAgIUEVjzN4XTbazT0YrhRlehUUQhkfYwDQYJKoZIhvcNAQEL
BQAwFjEUMBIGA1UEAwwLYXJpYS1icmlkZ2UwHhcNMjYwOTI2MTQyNjU0WhcNMzYw
OTIzMTQyNjU0WjAWMRQwEgYDVQQDDAthcmlhLWJyaWRnZTCCASIwDQYJKoZIhvcN
AQEBBQADggEPADCCAQoCggEBAKOKT7xoLl+vSM3wd0q4cFH9p5aS5/Y7VyRm7N86
Ur8LHzkIXkR+DzXN6CwqziljLe2Ak2DP8fecaO3Yp9AlGHgK8E9zM4RLHU9FYGhX
wF/Jqr7UxYeapPOg0p/tANPhFGOr1wuC9o2WmYdVKhRPKTfv/mLN0O/yOS+6a7F4
wgMR8LUg2t7g+A/P8tixGXXFl4bxxs+Ff1MxYGl5oy0ZXvGzAN08XCFoiFJ6z1Bs
SwkbuR7k15+mr/W1dt0uQ5le/m/hs9AW2DMNDag6T2hZy/42X6pXmEoH4lrnjOk0
ow7lI074/RU1LNARApkTKt2HblJrV3b6iEZ5NN1eUVh/CnkCAwEAAaN8MHowHQYD
VR0OBBYEFJUGhX33gWoxlZ9guh+wwgnl5JTzMB8GA1UdIwQYMBaAFJUGhX33gWox
lZ9guh+wwgnl5JTzMA8GA1UdEwEB/wQFMAMBAf8wJwYDVR0RBCAwHoILYXJpYS1i
cmlkZ2WCCWxvY2FsaG9zdIcEfwAAATANBgkqhkiG9w0BAQsFAAOCAQEAbFocytna
OgBNGqpq9ZQbwj03DlxmGalRlH1qAnGZac+zb3oGFkNxNBlCXjOTen4Gnfik93nO
4U0ucjS1VHYaIj1ydt8CB7SDxDPlmschvoNUA4QcsR7NctA3oPnnr5Mc2OIJwnbH
Pjc8cx+e/26A0KWuO9QWy3StU5FjNVHgbSelGH53jwPn6tcQufHaKLRbFMM59mvu
kIpDTE+OvlADfd1lm4o5Xfqf59hk5SKjfFtXZAmTshpoOQCwpNOyhxMUP292I3+i
ZsdcP2cpAXZufOWa7ILyhiHfTNvm8rbWMcOl6XlANiL1RQJlq0gtg+YN3NLO5l5A
AnshztnTBNrCdw==
-----END CERTIFICATE-----"""


_BRIDGE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCjik+8aC5fr0jN
8HdKuHBR/aeWkuf2O1ckZuzfOlK/Cx85CF5Efg81zegsKs4pYy3tgJNgz/H3nGjt
2KfQJRh4CvBPczOESx1PRWBoV8Bfyaq+1MWHmqTzoNKf7QDT4RRjq9cLgvaNlpmH
VSoUTyk37/5izdDv8jkvumuxeMIDEfC1INre4PgPz/LYsRl1xZeG8cbPhX9TMWBp
eaMtGV7xswDdPFwhaIhSes9QbEsJG7ke5Nefpq/1tXbdLkOZXv5v4bPQFtgzDQ2o
Ok9oWcv2Nl+qV5hKByJa54zpNKMMySNO+P0VNSzQEQKZFSrdh25Sa1d2+ohGeTTd
XlFYfwp5AgMBAAECggEAbqZ6mU0RkO6bX6b9N+k65xTz1u361g10x8E8k8y+1g9L
4X4d9Z3569i8l6s55A8A0s13i5aD+s0wA7+8Yw/e92v0y1m0k6b9k4h51j7l0+g0
a1u6b5y1p3m7+8y0A1k5y0y3m8w1z7z6k4h9k4b8w0v0y2j1A9g1y4k8w0g3u9k=
-----END PRIVATE KEY-----"""


def _generate_machine_cert(cert_p: str, key_p: str) -> bool:
    """Generate a unique self-signed cert for THIS machine.

    Preferred path is `cryptography`; on Windows, if that is unavailable or
    broken, falls back to PowerShell/.NET so no third-party package is needed.
    Returns True on success, False otherwise (caller falls back to bundled).
    """
    try:
        if not HAS_CRYPTOGRAPHY:
            raise RuntimeError("cryptography package not available")

        key = _crypto_rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = _crypto_x509.Name(
            [_crypto_x509.NameAttribute(_crypto_NameOID.COMMON_NAME, "aria-bridge")])
        cert = (
            _crypto_x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(_crypto_x509.random_serial_number())
            .not_valid_before(_dt.datetime.now(_dt.timezone.utc))
            .not_valid_after(_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=3650))
            .add_extension(
                _crypto_x509.SubjectAlternativeName([
                    _crypto_x509.DNSName("aria-bridge"),
                    _crypto_x509.DNSName("localhost"),
                    _crypto_x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]),
                critical=False,
            )
            .sign(key, _crypto_hashes.SHA256())
        )
        with open(key_p, "wb") as f:
            f.write(key.private_bytes(
                _crypto_serial.Encoding.PEM,
                _crypto_serial.PrivateFormat.TraditionalOpenSSL,
                _crypto_serial.NoEncryption()))
        with open(cert_p, "wb") as f:
            f.write(cert.public_bytes(_crypto_serial.Encoding.PEM))
        return True
    except Exception as e:
        crypto_err = e
    # Windows fallback: PowerShell/.NET needs no third-party packages.
    if sys.platform == "win32":
        try:
            if _generate_machine_cert_powershell(cert_p, key_p):
                return True
        except Exception as e2:
            msg = str(e2)[:300]
            _log("Windows native cert fallback failed:")
            for i in range(0, len(msg), 60):
                _log("> " + msg[i:i + 60])
    hint = (" - installed but its native libraries failed to load. Fix: reinstall "
            "with the SAME python that runs ARIA "
            "(python -m pip install --force-reinstall --no-cache-dir cryptography)"
            if "dll" in str(crypto_err).lower() else "")
    _log(f"per-machine cert generation failed ({crypto_err}){hint}")
    return False


def ensure_bridge_cert() -> str:
    """Pick the TLS identity for the bridge. Returns a status string.

    1. Per-machine cert under data/ (unique, never committed).
    2. Bundled shared cert (weaker — identical on every deployment).
    3. Plain HTTP fallback when nothing could be written.
    """
    global BRIDGE_SCHEME, BRIDGE_CERT, BRIDGE_KEY
    # 1. Prefer a per-machine cert in the data dir (unique, gitignored).
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        mcert = os.path.join(_DATA_DIR, "aria_bridge_machine.pem")
        mkey = os.path.join(_DATA_DIR, "aria_bridge_machine_key.pem")
        if not (os.path.exists(mcert) and os.path.exists(mkey)):
            if _generate_machine_cert(mcert, mkey):
                _log("generated unique per-machine HTTPS cert.")
        if os.path.exists(mcert) and os.path.exists(mkey):
            BRIDGE_SCHEME, BRIDGE_CERT, BRIDGE_KEY = "https", mcert, mkey
            return "https (per-machine cert)"
    except Exception as e:
        _log(f"per-machine cert path failed: {e}")
    # 2. Fall back to the bundled cert (shared across deployments — weaker).
    _log("WARNING - using bundled shared cert; install 'cryptography' "
         "for a unique per-machine certificate.")
    dirs = [_REPO_ROOT, tempfile.gettempdir()]
    for d in dirs:
        base = os.path.join(d, "aria_bridge_bundled")
        cert_p, key_p = base + ".pem", base + "_key.pem"
        try:
            cur = open(cert_p).read().strip() if os.path.exists(cert_p) else ""
            if cur != _BRIDGE_CERT_PEM.strip() or not os.path.exists(key_p):
                with open(cert_p, "w", encoding="utf-8") as f:
                    f.write(_BRIDGE_CERT_PEM.strip() + "\n")
                with open(key_p, "w", encoding="utf-8") as f:
                    f.write(_BRIDGE_KEY_PEM.strip() + "\n")
                _log(f"wrote bundled HTTPS cert ({d})")
            BRIDGE_SCHEME, BRIDGE_CERT, BRIDGE_KEY = "https", cert_p, key_p
            return "https (bundled shared cert)"
        except Exception:
            pass
    BRIDGE_SCHEME = "http"
    _log("[unavailable: no TLS cert] cert write failed - HTTP fallback.")
    return "http (no cert available)"


def get_bridge_url() -> str:
    """Public URL for the phone, e.g. https://192.168.1.5:8777."""
    return f"{BRIDGE_SCHEME}://{lan_ip()}:{PHONE_BRIDGE_PORT}"

BRIDGE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<title>A.R.I.A. Bridge</title>
<style>
:root{--acc:#ff5fa2;--bg:#05070a;--card:#0d1117;--line:#1c232c;--txt:#eef4fa;--dim:#8ba2b5}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;margin:0;padding:14px;min-height:100vh}
#app{max-width:560px;margin:0 auto;display:flex;flex-direction:column;gap:12px}
.topbar{display:flex;justify-content:space-between;align-items:center}
.wordmark{font-size:13px;letter-spacing:4px;color:var(--dim);font-weight:600}
.topbtns{display:flex;gap:8px;align-items:center}
.icon-btn{background:transparent;color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:7px 11px;font-size:16px;cursor:pointer}
#flipcam{font-size:12px;color:var(--dim)}
#stage{position:relative;display:flex;justify-content:center}
#face{width:min(94vw,520px);max-width:100%;border-radius:20px;border:1px solid var(--line);image-rendering:pixelated;background:#000;box-shadow:0 0 60px rgba(255,95,162,.12)}
#eyes{width:min(94vw,520px);max-width:100%;border-radius:20px;border:1px solid var(--line);background:#000}
#caption{position:absolute;left:12px;right:12px;bottom:10px;font-size:13px;line-height:1.4;color:var(--txt);background:rgba(5,7,10,.78);border:1px solid var(--line);border-radius:10px;padding:8px 10px;display:none;backdrop-filter:blur(6px)}
#caption.on{display:block}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px}
#settings{display:flex;flex-direction:column;gap:8px}
#log{max-height:38vh;overflow-y:auto;font-size:14px;display:flex;flex-direction:column;gap:6px}
.you{color:var(--acc)}.aria{color:#28f078}
.modes{display:flex;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:4px;gap:4px}
.mode-btn{flex:1;background:transparent;border:0;color:var(--dim);font-size:14px;font-weight:600;padding:9px;border-radius:9px;cursor:pointer}
.mode-btn.active{background:#1a212b;color:var(--txt)}
#lookbtn{background:transparent;border:1px solid var(--acc);color:var(--acc);border-radius:12px;padding:11px;font-size:14px;font-weight:700;cursor:pointer}
#lookbtn:disabled{opacity:.5}
form{display:flex;gap:8px}input{flex:1;padding:13px 14px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:#fff;font-size:16px;outline:none}
input:focus{border-color:var(--acc)}
#sendbtn{padding:0 22px;border-radius:12px;border:0;background:var(--acc);color:#fff;font-weight:700;font-size:16px;cursor:pointer}
.talk-row{display:flex;gap:8px}
#talk{flex:2;padding:15px;border-radius:12px;border:0;background:var(--acc);color:#fff;font-weight:700;font-size:16px;cursor:pointer;touch-action:none;user-select:none;-webkit-user-select:none}
#ptt{flex:1;padding:15px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--txt);font-weight:600;font-size:16px;cursor:pointer}
.row-btn{background:transparent;border:1px solid var(--line);color:var(--txt);font-size:14px;padding:11px;border-radius:10px;cursor:pointer;text-align:left}
.footrow{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--dim)}
.footrow a{color:var(--acc);text-decoration:none}
body.facemode{padding:0}
body.facemode #app{max-width:none}
body.facemode #app>*:not(#stage){display:none!important}
body.facemode #stage{position:fixed;inset:0;z-index:50;background:#000}
body.facemode #face,body.facemode #eyes{width:100%;height:100%;object-fit:contain;border:0;border-radius:0;box-shadow:none}
body.facemode #caption{display:none!important}
</style></head><body>
<div id="app">
<header class="topbar">
<div class="wordmark">A.R.I.A.</div>
<div class="topbtns">
<button id="flipcam" type="button" class="icon-btn" style="display:none">front ⇄</button>
<button id="logbtn" type="button" class="icon-btn" aria-label="Log">📜</button>
<button id="cog" type="button" class="icon-btn" aria-label="Settings">⚙</button>
</div>
</header>
<main id="stage">
<img id="face" alt="A.R.I.A.">
<img id="eyes" alt="Camera view" style="display:none">
<div id="caption"></div>
</main>
<div id="settings" class="panel" style="display:none">
<button id="spk" type="button" class="row-btn">Speak replies: ON</button>
<button id="testspk" type="button" class="row-btn">Test Audio</button>
<button id="cam" type="button" class="row-btn">Camera: OFF</button>
</div>
<div id="log" class="panel" style="display:none"></div>
<div class="modes">
<button id="mode-face" type="button" class="mode-btn active">Face</button>
<button id="mode-eyes" type="button" class="mode-btn">Eyes</button>
<button id="mode-full" type="button" class="mode-btn">⛶ Full</button>
</div>
<button id="lookbtn" type="button" style="display:none">✦ Describe what you see</button>
<form id="msgform">
<input id="t" placeholder="Directive..." autocomplete="off">
<button type="submit" id="sendbtn">Send</button>
</form>
<div class="talk-row">
<button id="talk">Hold to talk</button>
<button id="ptt" type="button">Tap to talk</button>
</div>
<div class="footrow">
<a href="/commands">Command reference</a>
<a href="/upload">Upload files</a>
<span id="astat">Audio: ready</span>
</div>
</div>
<audio id="aria-audio" playsinline webkit-playsinline preload="auto" style="display:none"></audio>
<audio id="aria-bg" loop playsinline webkit-playsinline preload="auto" style="display:none" src="/silent.wav"></audio>
<script>
async function api(path,opts){
  opts=opts||{};
  const r=await fetch(path,opts);
  if(r.status===401){ location.href='/'; }
  return r;
}
let spkOn=localStorage.getItem('spk')!=='0';
function updateSpkBtn(){document.getElementById('spk').innerText='Speak replies: '+(spkOn?'ON':'OFF');}
updateSpkBtn();

let actx=null,curSrc=null,voiceErrT=null;

function b64ToArrayBuffer(b64){
  const bin=window.atob(b64);
  const len=bin.length;
  const bytes=new Uint8Array(len);
  for(let i=0;i<len;i++){bytes[i]=bin.charCodeAt(i);}
  return bytes.buffer;
}

function ensureAudio(){
  if(!actx){
    try{
      const AC=window.AudioContext||window.webkitAudioContext;
      if(AC)actx=new AC();
    }catch(e){console.log('actx init err',e);}
  }
  if(actx&&(actx.state==='suspended'||actx.state==='interrupted')){
    try{actx.resume().catch(()=>{});}catch(e){}
  }
  return actx;
}

function unlockAudio(){
  try{
    const bg=document.getElementById('aria-bg');
    if(bg&&bg.paused){
      bg.play().catch(()=>{});
    }
  }catch(e){}
  // NOTE: the audio session category is NOT set here. It is set at the
  // point of use instead — 'playback' in playAudio(), 'playAndRecord'
  // around hold-to-talk capture. Setting it globally per-gesture raced
  // with getUserMedia and broke recording on iOS.
  try{
    const ctx=ensureAudio();
    if(ctx){
      if(ctx.state==='suspended'||ctx.state==='interrupted'){
        ctx.resume().catch(()=>{});
      }
      const b=ctx.createBuffer(1,1,22050);
      const s=ctx.createBufferSource();
      s.buffer=b;
      s.connect(ctx.destination);
      s.start(0);
    }
  }catch(e){}
  const st=document.getElementById('astat');
  if(st&&st.innerText.indexOf('error')===-1)st.innerText='Audio: active';
}

['touchstart','touchend','pointerdown','click','keydown'].forEach(evt=>{
  document.addEventListener(evt,unlockAudio,{passive:true});
});

let lastVoiceErr='';
function voiceError(msg,detail){
  lastVoiceErr=detail||msg||'';
  const st=document.getElementById('astat');
  if(st)st.innerText='Audio error: '+(msg||'tap Speak for details');
  const b=document.getElementById('spk');
  b.innerText='Speak replies: ERROR - tap for details';
  // Sticky on purpose: the old auto-clear hid total TTS outages behind
  // "Audio: ready". Clears on next successful playback or when tapped.
  clearTimeout(voiceErrT);
  b.onclick=()=>{
    alert('Voice failed. '+(lastVoiceErr?('PC says: '+lastVoiceErr+' '):'')+
      'On the PC, check the ARIA action stream for the line starting '+
      '"Bridge TTS failed:" — it names the exact synthesis error.');
    clearVoiceError();
  };
}
function clearVoiceError(){
  lastVoiceErr='';
  const b=document.getElementById('spk');
  clearTimeout(voiceErrT);
  b.onclick=spkToggle;
  updateSpkBtn();
  const st=document.getElementById('astat');
  if(st)st.innerText='Audio: ready';
}

function spkToggle(){
  spkOn=!spkOn;
  localStorage.setItem('spk',spkOn?'1':'0');
  updateSpkBtn();
  if(spkOn)unlockAudio();
}
document.getElementById('spk').onclick=spkToggle;

function playViaWebAudio(buf){
  return new Promise((resolve,reject)=>{
    const ctx=ensureAudio();
    if(!ctx){reject(new Error('no audio context'));return;}
    if(ctx.state==='suspended'||ctx.state==='interrupted'){
      ctx.resume().catch(()=>{});
    }
    if(curSrc){try{curSrc.stop();}catch(e){}curSrc=null;}
    const copy=buf.slice(0);
    ctx.decodeAudioData(copy,(decoded)=>{
      try{
        const src=ctx.createBufferSource();
        src.buffer=decoded;
        src.connect(ctx.destination);
        src.onended=()=>{curSrc=null;resolve();};
        src.start(0);
        curSrc=src;
      }catch(err){reject(err);}
    },(err)=>{reject(err);});
  });
}

function playAudio(buf,mime){
  return new Promise((resolve)=>{
    if(!spkOn||!buf||!buf.byteLength){resolve();return;}
    unlockAudio();
    const st=document.getElementById('astat');
    if(st)st.innerText='Audio: playing...';

    let resolved=false;
    const finish=(ok)=>{
      if(!resolved){
        resolved=true;
        if(ok){clearVoiceError();}
        else if(st)st.innerText='Audio: ready';
        resolve();
      }
    };

    const player=document.getElementById('aria-audio');
    let blobUrl=null;
    try{
      // Use the server's real content type. The old hardcoded
      // 'audio/mpeg' mislabeled SAPI WAV bytes and broke the primary
      // playback path whenever Edge was down.
      const blob=new Blob([buf],{type:mime||'audio/mpeg'});
      blobUrl=URL.createObjectURL(blob);
      player.src=blobUrl;

      const cleanupPlayer=()=>{
        if(blobUrl){URL.revokeObjectURL(blobUrl);blobUrl=null;}
        player.onended=null;
        player.onerror=null;
        finish(true);
      };

      player.onended=cleanupPlayer;
      player.onerror=()=>{
        if(blobUrl){URL.revokeObjectURL(blobUrl);blobUrl=null;}
        player.onended=null;
        player.onerror=null;
        playViaWebAudio(buf).then(()=>finish(true)).catch((e)=>{
          console.log('web audio fallback error',e);
          voiceError('playback failed',String(e&&e.message||e));
          finish(false);
        });
      };

      const p=player.play();
      if(p!==undefined){
        p.catch((err)=>{
          console.log('HTMLAudio play rejected, using WebAudio fallback',err);
          if(blobUrl){URL.revokeObjectURL(blobUrl);blobUrl=null;}
          player.onended=null;
          player.onerror=null;
          playViaWebAudio(buf).then(()=>finish(true)).catch((e)=>{
            console.log('web audio fallback error',e);
            voiceError('playback failed',String(e&&e.message||e));
            finish(false);
          });
        });
      }
    }catch(err){
      console.log('HTMLAudio error, using WebAudio fallback',err);
      playViaWebAudio(buf).then(()=>finish(true)).catch((e)=>{
        voiceError('playback failed',String(e&&e.message||e));
        finish(false);
      });
    }
  });
}

async function playReply(text){
  if(!spkOn||!text)return;
  try{
    const r=await api('/api/say?text='+encodeURIComponent(text.slice(0,500)));
    if(!r.ok){
      // Surface the PC's real TTS reason (e.g. "tts unavailable: edge-tts
      // failed (...); SAPI fallback failed (...)") instead of a bare status.
      let srv='tts http '+r.status;
      try{const j=await r.json();if(j&&j.error)srv=j.error;}catch(e){}
      throw new Error(srv);
    }
    const mime=r.headers.get('Content-Type')||'audio/mpeg';
    const buf=await r.arrayBuffer();
    await playAudio(buf,mime);
  }catch(e){console.log('voice:',e);voiceError('tts failed',String(e&&e.message||e));}
}

document.getElementById('testspk').onclick=async()=>{
  unlockAudio();
  const st=document.getElementById('astat');
  if(st)st.innerText='Audio: synthesizing test...';
  try{
    const r=await api('/api/say?text='+encodeURIComponent('Speech test successful. Phone audio is active.'));
    if(!r.ok){
      let srv='http '+r.status;
      try{const j=await r.json();if(j&&j.error)srv=j.error;}catch(e){}
      throw new Error(srv);
    }
    const mime=r.headers.get('Content-Type')||'audio/mpeg';
    const buf=await r.arrayBuffer();
    await playAudio(buf,mime);
  }catch(e){
    alert('Test failed: '+(e&&e.message||e));
    voiceError('test failed',String(e&&e.message||e));
  }
};

const logEl=document.getElementById('log');
function add(s,m){
  const d=document.createElement('div');
  d.innerHTML='<b class="'+s+'">'+s.toUpperCase()+':</b> '+m.replace(/</g,'&lt;');
  logEl.appendChild(d);logEl.scrollTop=logEl.scrollHeight;
}
async function refreshLog(){
  try{
    const r=await api('/api/log');
    if(!r.ok)return;
    const rows=await r.json();logEl.innerHTML='';
    rows.forEach(x=>add(x[1].toLowerCase()==='user'?'you':'aria',x[2]));
  }catch(e){}
}
refreshLog();
setInterval(refreshLog,3000);
document.getElementById('face').src='/face.mjpg';

const msgForm=document.getElementById('msgform');
msgForm.addEventListener('submit',async function(e){
  e.preventDefault();
  e.stopPropagation();
  unlockAudio();
  const inp=document.getElementById('t');
  const t=inp.value.trim();
  if(!t)return false;
  inp.value='';
  add('you',t);
  const btn=document.getElementById('sendbtn');
  btn.disabled=true;
  try{
    const r=await api('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t,want_audio:spkOn})});
    btn.disabled=false;
    if(r.ok){
      const d=await r.json();
      const replies=d.reply||[];
      for(const s of replies){
        if(s&&s.trim())add('aria',s);
      }
      if(d.audio&&spkOn){
        try{
          const buf=b64ToArrayBuffer(d.audio);
          await playAudio(buf,d.audio_mime||'audio/mpeg');
        }catch(err){
          console.log('play audio err',err);
          voiceError('audio decode',String(err&&err.message||err));
        }
      }else if(replies.length>0&&spkOn){
        await playReply(replies[0]);
      }
    }else{
      let rd={};try{rd=await r.json();}catch(e){}
      alert('Directive failed: '+(rd.error||r.status));
    }
  }catch(err){
    btn.disabled=false;
    alert('Network error: '+err);
  }
  return false;
});

let mr=null,chunks=[],talkActive=false,talkStream=null;
const talkBtn=document.getElementById('talk');
const pttBtn=document.getElementById('ptt');
function setTalkUI(state){
  // state: 'idle' | 'listening' | 'processing'
  talkBtn.innerText=state==='listening'?'Listening...':(state==='processing'?'Processing...':'Hold to talk');
  pttBtn.innerText=state==='listening'?'Tap to stop':(state==='processing'?'Processing...':'Tap to talk');
}
async function beginTalk(){
  if(talkActive)return;
  talkActive=true;
  chunks=[];
  setTalkUI('listening');
  try{
    const stream=await navigator.mediaDevices.getUserMedia({audio:true});
    if(!talkActive){
      stream.getTracks().forEach(t=>t.stop());
      setTalkUI('idle');
      return;
    }
    talkStream=stream;
    let mimeType='';
    if(window.MediaRecorder&&typeof MediaRecorder.isTypeSupported==='function'){
      if(MediaRecorder.isTypeSupported('audio/webm;codecs=opus'))mimeType='audio/webm;codecs=opus';
      else if(MediaRecorder.isTypeSupported('audio/webm'))mimeType='audio/webm';
      else if(MediaRecorder.isTypeSupported('audio/mp4'))mimeType='audio/mp4';
      else if(MediaRecorder.isTypeSupported('audio/aac'))mimeType='audio/aac';
    }
    mr=mimeType?new MediaRecorder(stream,{mimeType}):new MediaRecorder(stream);
    mr.ondataavailable=ev=>{if(ev.data&&ev.data.size>0)chunks.push(ev.data);};
    mr.onstop=async()=>{
      if(talkStream){talkStream.getTracks().forEach(t=>t.stop());talkStream=null;}
      const recType=(mr&&mr.mimeType)||mimeType||'audio/webm';
      const blob=new Blob(chunks,{type:recType});
      setTalkUI('processing');
      try{
        const r=await api('/api/voice',{method:'POST',headers:{'Content-Type':recType},body:blob});
        if(r.ok){
          const mime=r.headers.get('Content-Type')||'audio/mpeg';
          const buf=await r.arrayBuffer();
          const transcript=decodeURIComponent(r.headers.get('X-Transcript')||'');
          const reply=decodeURIComponent(r.headers.get('X-Reply')||'');
          if(transcript)add('you',transcript);
          if(reply)add('aria',reply);
          try{await playAudio(buf,mime);}catch(e){console.log('voice:',e);voiceError('voice play',String(e&&e.message||e));}
        }else{
          let rd={};try{rd=await r.json();}catch(e){}
          if(rd.reply)add('aria',rd.reply);
          voiceError('voice error',rd.error||('http '+r.status));
          if(!rd.reply)alert('Voice request failed: '+(rd.error||r.status));
        }
      }catch(err){alert('Voice error: '+err);}
      setTalkUI('idle');
    };
    mr.start();
  }catch(err){
    talkActive=false;
    setTalkUI('idle');
    alert('Mic error ('+err+'). Ensure HTTPS certificate is accepted.');
  }
}
function endTalk(){
  if(!talkActive)return;
  talkActive=false;
  if(mr&&mr.state==='recording'){mr.stop();}
  else{
    if(talkStream){talkStream.getTracks().forEach(t=>t.stop());talkStream=null;}
    setTalkUI('idle');
  }
}
talkBtn.onpointerdown=(e)=>{
  e.preventDefault();
  unlockAudio();
  beginTalk();
};
talkBtn.onpointerup=(e)=>{
  e.preventDefault();
  unlockAudio();
  endTalk();
};
talkBtn.onpointercancel=talkBtn.onpointerup;
pttBtn.addEventListener('click',()=>{
  unlockAudio();
  if(talkActive)endTalk();
  else beginTalk();
});

// Phone-as-eyes: stream this page's camera to the laptop as ARIA's body
// camera (set ARIA_BODY_CAMERA=bridge on the laptop first).
let camOn=false,camStream=null,camTimer=null,camFacing='user';
const camBtn=document.getElementById('cam');
const camVideo=document.createElement('video');
camVideo.setAttribute('playsinline','');camVideo.muted=true;camVideo.style.display='none';
document.body.appendChild(camVideo);
const camCanvas=document.createElement('canvas');camCanvas.width=480;camCanvas.height=360;
function camFacingName(){return camFacing==='user'?'front':'rear';}
function updateCamBtn(){
  camBtn.innerText='Camera: '+(camOn?('ON ('+camFacingName()+')'):'OFF');
  const fc=document.getElementById('flipcam');
  fc.style.display=camOn?'':'none';
  fc.innerText=camFacingName()+' ⇄';
}
async function startCamStream(){
  if(camStream){camStream.getTracks().forEach(t=>t.stop());camStream=null;}
  camStream=await navigator.mediaDevices.getUserMedia(
    {video:{facingMode:camFacing,width:{ideal:480},height:{ideal:360}},audio:false});
  camVideo.srcObject=camStream;
  await camVideo.play();
}
async function toggleCam(){
  if(camOn){
    camOn=false;updateCamBtn();
    if(camTimer){clearInterval(camTimer);camTimer=null;}
    if(camStream){camStream.getTracks().forEach(t=>t.stop());camStream=null;}
    return;
  }
  try{
    await startCamStream();
    camOn=true;updateCamBtn();
    const ctx=camCanvas.getContext('2d');
    let posting=false;
    camTimer=setInterval(()=>{
      if(!camOn||posting)return;
      if(camVideo.readyState<2||camVideo.videoWidth===0)return;
      ctx.drawImage(camVideo,0,0,camCanvas.width,camCanvas.height);
      posting=true;
      camCanvas.toBlob(async(blob)=>{
        posting=false;
        if(!blob||!camOn)return;
        try{await api('/api/camframe',{method:'POST',headers:{'Content-Type':'image/jpeg'},body:blob});}catch(e){}
      },'image/jpeg',0.6);
    },350);
  }catch(err){
    alert('Camera error ('+err+'). Grant camera permission and ensure the HTTPS certificate is accepted.');
  }
}
camBtn.addEventListener('click',()=>{unlockAudio();toggleCam();});
async function flipCam(){
  if(!camOn)return;
  camFacing=(camFacing==='user')?'environment':'user';
  updateCamBtn();
  try{await startCamStream();}
  catch(err){alert('Camera flip failed ('+err+').');}
}
document.getElementById('flipcam').addEventListener('click',()=>{unlockAudio();flipCam();});

// ---- view modes: face / eyes / fullscreen ----
const stageEl=document.getElementById('stage');
const faceImg=document.getElementById('face');
const eyesImg=document.getElementById('eyes');
const captionEl=document.getElementById('caption');
const lookBtn=document.getElementById('lookbtn');
function setCaption(t){
  captionEl.innerText=t||'';
  captionEl.classList.toggle('on',!!t);
}
async function setView(mode){
  document.getElementById('mode-face').classList.toggle('active',mode==='face');
  document.getElementById('mode-eyes').classList.toggle('active',mode==='eyes');
  const eyes=mode==='eyes';
  faceImg.style.display=eyes?'none':'';
  eyesImg.style.display=eyes?'':'none';
  lookBtn.style.display=eyes?'':'none';
  if(eyes){
    setCaption('Connecting…');
    try{
      const r=await api('/api/camstatus');
      if(r.ok){
        const d=await r.json();
        if(d.active){setCaption('');eyesImg.src='/phone_cam.mjpg';}
        else{setCaption('Camera is off — enable it in ⚙ settings.');eyesImg.style.display='none';}
      }else{setCaption('Could not reach camera.');}
    }catch(e){setCaption('Could not reach camera.');}
  }else{
    eyesImg.removeAttribute('src');
    setCaption('');
  }
}
document.getElementById('mode-face').addEventListener('click',()=>setView('face'));
document.getElementById('mode-eyes').addEventListener('click',()=>setView('eyes'));
document.getElementById('mode-full').addEventListener('click',()=>{
  if(document.getElementById('mode-eyes').classList.contains('active'))setView('face');
  document.body.classList.add('facemode');
});
stageEl.addEventListener('click',()=>{
  if(document.body.classList.contains('facemode'))document.body.classList.remove('facemode');
});
lookBtn.addEventListener('click',async()=>{
  lookBtn.disabled=true;lookBtn.innerText='Looking…';setCaption('');
  try{
    const r=await api('/api/look',{method:'POST'});
    let d={};try{d=await r.json();}catch(e){}
    if(r.ok&&d.reply){setCaption(d.reply);add('aria',d.reply);}
    else setCaption('Look failed: '+(d.error||r.status));
  }catch(e){setCaption('Look failed: '+e);}
  lookBtn.disabled=false;lookBtn.innerText='✦ Describe what you see';
});
document.getElementById('cog').addEventListener('click',()=>{
  const s=document.getElementById('settings');
  s.style.display=(s.style.display==='none')?'flex':'none';
});
document.getElementById('logbtn').addEventListener('click',()=>{
  const l=document.getElementById('log');
  l.style.display=(l.style.display==='none')?'flex':'none';
  if(l.style.display!=='none'){l.scrollTop=l.scrollHeight;}
});
</script></body></html>"""


BRIDGE_LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>A.R.I.A. Bridge - Login</title>
<style>body{background:#0b0e12;color:#e8f4ff;font-family:sans-serif;margin:0;padding:16px}
h2{color:#ff5fa2}p{color:#9fb2c3;font-size:14px}
form{display:flex;gap:8px;margin-top:24px}input{flex:1;padding:12px;border-radius:8px;border:1px
solid #2a3138;background:#14181d;color:#fff;font-size:16px}
button{padding:12px 18px;border-radius:8px;border:0;background:#ff5fa2;color:#fff;
font-weight:bold;font-size:16px}</style></head><body>
<h2>A.R.I.A. // Phone Bridge</h2>
<p>Enter your bridge token to connect. Find it in the ARIA console, or ask ARIA for it.</p>
<form onsubmit="login();return false"><input id="t" type="password" placeholder="Bridge token..."
autocomplete="off"><button>Connect</button></form>
<script>
async function login(){
  const t=document.getElementById('t').value;
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:t})});
  if(r.ok){location.href='/';}
  else{document.getElementById('t').value='';alert('Bad bridge token.');}
  return false;
}
</script></body></html>"""


UPLOAD_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1">
<title>A.R.I.A. Upload</title>
<style>
:root{--acc:#ff5fa2;--bg:#05070a;--card:#0d1117;--line:#1c232c;--txt:#eef4fa;--dim:#8ba2b5}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;margin:0;padding:16px}
#app{max-width:560px;margin:0 auto;display:flex;flex-direction:column;gap:12px}
h2{color:var(--acc);font-size:18px;letter-spacing:2px;margin:4px 0}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
#drop{border:2px dashed var(--line);border-radius:14px;padding:28px 14px;text-align:center;color:var(--dim);cursor:pointer}
#drop.over{border-color:var(--acc);color:var(--txt)}
#file{position:absolute;left:-9999px}
#upbtn{padding:13px;border-radius:12px;border:0;background:var(--acc);color:#fff;font-weight:700;font-size:16px;cursor:pointer;width:100%}
#upbtn:disabled{opacity:.5}
#status{font-size:14px;color:var(--dim);min-height:20px}
#status.ok{color:#28f078}#status.err{color:#ff6b6b}
.item{font-size:14px;padding:8px 0;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:8px}
.item span:last-child{color:var(--dim);font-size:12px;white-space:nowrap}
a{color:var(--acc);text-decoration:none}
</style></head><body>
<div id="app">
<h2>A.R.I.A. // UPLOAD</h2>
<div class="panel">
<div id="drop">Tap to pick photos or files<br><small>multiple allowed, up to 50 MB each</small></div>
<input id="file" type="file" multiple>
</div>
<button id="upbtn" type="button" disabled>Upload</button>
<div id="status"></div>
<div class="panel">
<h2 style="font-size:14px">INBOX</h2>
<div id="list"><div class="item"><span>Loading...</span><span></span></div></div>
</div>
<p><a href="/">&larr; Bridge</a></p>
</div>
<script>
var drop=document.getElementById('drop'),file=document.getElementById('file'),
    upbtn=document.getElementById('upbtn'),status=document.getElementById('status'),
    list=document.getElementById('list'),chosen=[];
drop.onclick=function(){file.click();};
file.onchange=function(){chosen=Array.prototype.slice.call(file.files);renderChosen();};
function renderChosen(){
  upbtn.disabled=!chosen.length;
  drop.innerHTML=chosen.length?chosen.map(function(f){return f.name;}).join('<br>'):'Tap to pick photos or files<br><small>multiple allowed, up to 50 MB each</small>';
}
function note(msg,cls){status.className=cls||'';status.textContent=msg;}
async function refresh(){
  try{
    var r=await fetch('/api/inbox');
    if(r.status===401){location.href='/';return;}
    var items=await r.json();
    list.innerHTML=items.length?items.map(function(f){
      return '<div class="item"><span>'+f.name+'</span><span>'+f.size+' &middot; '+f.when+'</span></div>';
    }).join(''):'<div class="item"><span>Empty - send something up.</span><span></span></div>';
  }catch(e){list.innerHTML='<div class="item"><span>Could not load inbox.</span><span></span></div>';}
}
upbtn.onclick=async function(){
  if(!chosen.length)return;
  upbtn.disabled=true;note('Uploading '+chosen.length+' file(s)...');
  var fd=new FormData();
  chosen.forEach(function(f){fd.append('files',f,f.name);});
  try{
    var r=await fetch('/api/upload',{method:'POST',body:fd});
    if(r.status===401){location.href='/';return;}
    var j=await r.json();
    if(j.ok){
      note('Saved: '+(j.saved.join(', ')||'nothing')+(j.skipped&&j.skipped.length?' - skipped: '+j.skipped.join(', '):''),'ok');
      chosen=[];file.value='';renderChosen();refresh();
    }else{note('Upload failed: '+(j.error||'unknown'),'err');}
  }catch(e){note('Upload failed: '+e,'err');}
  upbtn.disabled=!chosen.length;
};
refresh();
</script></body></html>"""


def _commands_html() -> str:
    """Command reference page. Falls back honestly when no guide is present."""
    if _COMMAND_GUIDE:
        parts = []
        last = None
        for cat, tool, ex in _COMMAND_GUIDE:
            if cat != last:
                if last is not None:
                    parts.append("</div>")
                parts.append(f"<h3>{cat}</h3><div class='grp'>")
                last = cat
            parts.append(
                f"<div class='cmd' data-t='{tool} {ex} {cat}'><b>{tool}</b>"
                f"<span>&quot;{ex}&quot;</span></div>")
        parts.append("</div>")
        listing = f"<div id='list'>{''.join(parts)}</div>"
    else:
        listing = ("<p>[unavailable: command guide not present in this build]</p>")
    return (
        "<!DOCTYPE html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>A.R.I.A. Commands</title><style>"
        "body{background:#0b0e12;color:#e8f4ff;font-family:sans-serif;margin:0;padding:16px}"
        "h2{color:#ff5fa2}h3{color:#1edcff;margin:18px 0 6px}"
        ".cmd{background:#14181d;border:1px solid #2a3138;border-radius:8px;padding:10px;margin-bottom:6px}"
        ".cmd b{color:#1edcff;display:block}.cmd span{color:#9fb2c3;font-size:14px}"
        "#q{width:100%;padding:12px;border-radius:8px;border:1px solid #2a3138;background:#14181d;color:#fff;font-size:16px;box-sizing:border-box}"
        "a{color:#ff5fa2}</style></head><body>"
        "<h2>A.R.I.A. // Commands</h2>"
        "<input id='q' placeholder='Filter commands...' oninput='f()'>"
        f"{listing}"
        "<p><a href='/'>&larr; Bridge</a></p>"
        "<script>function f(){var q=document.getElementById('q').value.toLowerCase();"
        "document.querySelectorAll('.cmd').forEach(function(e){"
        "e.style.display=e.getAttribute('data-t').toLowerCase().indexOf(q)>=0?'':'none';});}</script></body></html>"
    )


# ---------------------------------------------------------------------------
# Inbox: files uploaded from the phone land here (v1 had aria/inbox.py;
# v2 keeps a minimal local inbox under data/inbox).
# ---------------------------------------------------------------------------

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_MAX_FILE_BYTES = 50 * 1024 * 1024


def _inbox_dir() -> str:
    d = os.path.join(_DATA_DIR, "inbox")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    base = os.path.basename((name or "upload").replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base) or "upload"
    return base[:120]


def _list_inbox() -> List[Dict[str, str]]:
    entries = []
    try:
        for fn in sorted(os.listdir(_inbox_dir()),
                         key=lambda f: os.path.getmtime(os.path.join(_inbox_dir(), f)),
                         reverse=True):
            full = os.path.join(_inbox_dir(), fn)
            if not os.path.isfile(full):
                continue
            sz = os.path.getsize(full)
            when = datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M")
            entries.append({
                "name": fn,
                "size_h": f"{sz // 1024} KB",
                "when": when,
            })
    except OSError:
        pass
    return entries


def _parse_multipart(body: bytes, content_type: str) -> List[Tuple[str, bytes]]:
    """Parse multipart/form-data with the stdlib email parser."""
    head = b"Content-Type: " + content_type.encode("utf-8", "replace") + b"\r\n\r\n"
    msg = BytesParser(policy=policy.HTTP).parsebytes(head + body)
    parts = []
    if msg.is_multipart():
        for part in msg.iter_parts():
            fname = part.get_filename()
            data = part.get_payload(decode=True)
            if fname and data is not None:
                parts.append((fname, data))
    return parts


def _save_upload(fname: str, data: bytes) -> str:
    name = _safe_name(fname)
    dest = os.path.join(_inbox_dir(), name)
    stem, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(dest):
        i += 1
        dest = os.path.join(_inbox_dir(), f"{stem}_{i}{ext}")
    fd, tmp = tempfile.mkstemp(dir=_inbox_dir(), prefix=".up-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return os.path.basename(dest)


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "ARIA-Bridge/2"

    def log_message(self, format, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_unavailable(self, what: str):
        self._send(503, {"error": f"[unavailable: {what}]"})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "*")
        self.end_headers()

    def _token_from_request(self) -> str:
        tok = self.headers.get("X-Bridge-Token", "")
        if not tok:
            for part in self.headers.get("Cookie", "").split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k.strip() == "aria_bridge_token":
                        tok = urllib.parse.unquote(v.strip())
                        break
        if not tok and "?" in self.path:
            qs = self.path.split("?", 1)[1]
            tok = urllib.parse.unquote_plus(
                dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("token", ""))
        return tok

    def _authed(self):
        tok = bridge_token()
        return bool(tok) and self._token_from_request() == tok

    def _bridge_cookie(self) -> str:
        cookie = ("aria_bridge_token=" + urllib.parse.quote(bridge_token(), safe="")
                  + "; HttpOnly; Path=/; SameSite=Strict")
        if BRIDGE_SCHEME == "https":
            cookie += "; Secure"
        return cookie

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        if self.path.startswith("/api/"):
            if not self._authed():
                self._send(401, b'{"error":"bad or missing bridge token"}')
                return
            if self.path.startswith("/api/log"):
                logs = _CHAT_LOG_CALL() if _CHAT_LOG_CALL else []
                try:
                    payload = json.dumps(list(logs)[-30:]).encode("utf-8")
                except Exception:
                    payload = b"[]"
                self._send(200, payload)
                return
            if self.path.startswith("/api/camstatus"):
                if _get_phone_frame_status is None:
                    self._send(503, b'{"error":"[unavailable: phone camera pipeline not present in this build]"}')
                    return
                self._send(200, json.dumps(_get_phone_frame_status()).encode("utf-8"))
                return
            if self.path.startswith("/api/inbox"):
                files = _list_inbox()
                self._send(200, json.dumps(
                    [{"name": f["name"], "size": f["size_h"], "when": f["when"]}
                     for f in files]).encode("utf-8"))
                return
            if self.path.startswith("/api/say"):
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
                text = urllib.parse.unquote_plus(params.get("text", ""))
                if not text.strip():
                    self._send(400, b'{"error":"missing text"}')
                    return
                if _tts_bytes_for_bridge is None:
                    self._send(503, b'{"error":"[unavailable: bridge TTS not present in this build]"}')
                    return
                try:
                    audio, ctype = _tts_bytes_for_bridge(text[:500])
                except Exception as e:
                    _log(f"Bridge TTS failed: {e}")
                    self._send(500, json.dumps(
                        {"error": f"tts unavailable: {str(e)[:150]}"}).encode("utf-8"))
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(audio)
                return
            self._send(404, b'{"error":"not found"}')
            return

        if self.path.startswith("/silent.wav"):
            if not self._authed():
                self._send(401, b'{"error":"bad or missing bridge token"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(_SILENT_WAV_BYTES)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(_SILENT_WAV_BYTES)
            return

        if self.path.startswith("/phone_cam.mjpg"):
            if not self._authed():
                self._send(401, b'{"error":"bad or missing bridge token"}')
                return
            if _get_phone_frame_jpeg is None:
                self._send(503, b'{"error":"[unavailable: phone camera pipeline not present in this build]"}')
                return
            if not _get_phone_frame_jpeg():
                self._send(404, b'{"error":"no camera frames yet"}')
                return
            self._stream_mjpeg(_get_phone_frame_jpeg, 0.25)
            return

        if self.path.startswith("/face.mjpg"):
            if not self._authed():
                self._send(401, b'{"error":"bad or missing bridge token"}')
                return
            if _get_face_frame_jpeg is None:
                self._send(503, b'{"error":"[unavailable: face stream not present in this build]"}')
                return
            self._stream_mjpeg(_get_face_frame_jpeg, 0.08)
            return

        path = self.path.split("?", 1)[0]
        if path == "/":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            qtok = urllib.parse.unquote_plus(
                dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("token", ""))
            tok = bridge_token()
            if tok and qtok and qtok == tok:
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", self._bridge_cookie())
                self.end_headers()
                return
            if self._authed():
                self._send(200, BRIDGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            self._send(200, BRIDGE_LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/commands":
            if not self._authed():
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self._send(200, _commands_html().encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/upload":
            if not self._authed():
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self._send(200, UPLOAD_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return

        self._send(404, b'{"error":"not found"}')

    def _stream_mjpeg(self, frame_fn, interval: float):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                jpg = frame_fn()
                if jpg:
                    chunk = (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                             + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                    self.wfile.write(chunk)
                time.sleep(interval)
        except Exception:
            return

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        if self.path.startswith("/api/login"):
            length = int(self.headers.get("Content-Length", 0))
            try:
                tok = json.loads(self.rfile.read(length).decode("utf-8")).get("token", "")
            except Exception:
                tok = ""
            if bridge_token() and tok == bridge_token():
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", self._bridge_cookie())
                self.end_headers()
                self.wfile.write(body)
            else:
                if bridge_token():
                    _log("failed login attempt.")
                self._send(401, b'{"error":"bad bridge token"}')
            return
        if not (self.path.startswith("/api/ask") or self.path.startswith("/api/voice")
                or self.path.startswith("/api/camframe") or self.path.startswith("/api/look")
                or self.path.startswith("/api/upload")):
            self.send_response(404)
            self.end_headers()
            return
        if not self._authed():
            self._send(401, b'{"error":"bad or missing bridge token"}')
            return

        length = int(self.headers.get("Content-Length", 0))
        if self.path.startswith("/api/upload"):
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                self._send(400, b'{"error":"multipart/form-data required"}')
                return
            if length > _MAX_UPLOAD_BYTES:
                self._send(413, b'{"error":"upload too large (100 MB cap)"}')
                return
            body = self.rfile.read(length)
            try:
                parts = _parse_multipart(body, ctype)
            except Exception as e:
                _log(f"upload parse failed: {e}")
                self._send(400, b'{"error":"could not parse upload"}')
                return
            if not parts:
                self._send(400, b'{"error":"no files in upload"}')
                return
            saved, skipped = [], []
            for fname, data in parts:
                if not data:
                    skipped.append(fname or "unnamed")
                    continue
                if len(data) > _MAX_FILE_BYTES:
                    skipped.append(fname or "unnamed")
                    continue
                try:
                    saved.append(_save_upload(fname, data))
                except Exception as e:
                    _log(f"upload save failed: {e}")
                    skipped.append(fname or "unnamed")
            _log(f"upload: saved {len(saved)}, skipped {len(skipped)}")
            self._send(200, json.dumps(
                {"ok": True, "saved": saved, "skipped": skipped}).encode("utf-8"))
            return
        if self.path.startswith("/api/look"):
            if _describe_phone_view is None:
                self._send(503, b'{"error":"[unavailable: phone camera pipeline not present in this build]"}')
                return
            try:
                reply = _describe_phone_view()
            except Exception as e:
                self._send(500, json.dumps({"error": f"look failed: {e}"}).encode("utf-8"))
                return
            self._send(200, json.dumps({"reply": reply}).encode("utf-8"))
            return
        if self.path.startswith("/api/camframe"):
            if _publish_phone_frame is None:
                self._send(503, b'{"error":"[unavailable: phone camera pipeline not present in this build]"}')
                return
            if length > 1_000_000:
                self._send(413, b'{"error":"frame too large"}')
                return
            body = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip()
            if ctype not in ("image/jpeg", "application/octet-stream") \
                    or not _publish_phone_frame(body):
                self._send(400, b'{"error":"bad frame"}')
                return
            self._send(200, b'{"ok":true}')
            return
        if self.path.startswith("/api/voice"):
            if _transcribe_audio is None:
                self._send(503, b'{"error":"[unavailable: speech transcription not present in this build]"}')
                return
            audio_in = self.rfile.read(length)
            mime = self.headers.get("Content-Type", "audio/webm")
            try:
                text = _transcribe_audio(audio_in, mime)
            except Exception as e:
                self._send(500, json.dumps({"error": f"transcribe failed: {e}"}).encode("utf-8"))
                return
            _log(f"voice: {text[:30]}")
            reply = _BRIDGE_PROCESS_CALL(text, True) if _BRIDGE_PROCESS_CALL else ""
            if _tts_bytes_for_bridge is None:
                self._send(500, json.dumps(
                    {"error": "[unavailable: bridge TTS not present in this build]",
                     "reply": reply[:500]}).encode("utf-8"))
                return
            try:
                audio_out, ctype = _tts_bytes_for_bridge(reply[:2000])
            except Exception as e:
                _log(f"voice TTS failed: {e}")
                self._send(500, json.dumps(
                    {"error": f"tts failed: {str(e)[:150]}", "reply": reply[:500]}).encode("utf-8"))
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(audio_out)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("X-Transcript", urllib.parse.quote(text[:300]))
            self.send_header("X-Reply", urllib.parse.quote(reply[:500]))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "X-Transcript, X-Reply")
            self.end_headers()
            self.wfile.write(audio_out)
            return

        # /api/ask — text directive from the phone.
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            text = payload.get("text", "")
            want_audio = bool(payload.get("want_audio", False))
        except Exception:
            text = ""
            want_audio = False

        if text.strip():
            _log(f"directive: {text[:25]}")
            if _BRIDGE_PROCESS_CALL is None:
                reply = ("[unavailable: bridge processor not wired — "
                         "call set_bridge_processor() from the agent loop]")
            else:
                reply = _BRIDGE_PROCESS_CALL(text.strip(), True)
        else:
            reply = ""

        audio_b64 = ""
        audio_mime = ""
        if reply.strip() and want_audio:
            if _tts_bytes_for_bridge is None:
                _log("ask TTS skipped: [unavailable: bridge TTS not present]")
            else:
                try:
                    audio_out, audio_mime = _tts_bytes_for_bridge(reply[:2000])
                    if audio_out:
                        audio_b64 = base64.b64encode(audio_out).decode("ascii")
                except Exception as e:
                    _log(f"ask TTS failed: {e}")
                    audio_mime = ""

        self._send(200, json.dumps(
            {"reply": [reply], "audio": audio_b64, "audio_mime": audio_mime}
        ).encode("utf-8"))


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def start_bridge_server(port: Optional[int] = None,
                        host: Optional[str] = None,
                        blocking: bool = True) -> ThreadingHTTPServer:
    """Start the phone bridge. Returns the server.

    blocking=True (v1 parity) serves on this thread; blocking=False serves
    on a daemon thread and returns immediately (for tool-based starts).
    """
    global _BRIDGE_SERVER, _BRIDGE_THREAD
    port = PHONE_BRIDGE_PORT if port is None else port
    host = BRIDGE_BIND_HOST if host is None else host
    cert_status = ensure_bridge_cert()
    srv = ThreadingHTTPServer((host, port), BridgeHandler)
    _BRIDGE_SERVER = srv
    if BRIDGE_SCHEME == "https" and BRIDGE_CERT and BRIDGE_KEY:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=BRIDGE_CERT, keyfile=BRIDGE_KEY)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            _log(f"online ({cert_status}): https://{lan_ip()}:{port}")
        except Exception as e:
            _log(f"TLS wrap failed: {e}")
    else:
        _log(f"online ({cert_status}): http://{lan_ip()}:{port}")
    if blocking:
        try:
            srv.serve_forever()
        except Exception as e:
            _log(f"server stopped: {e}")
    else:
        t = threading.Thread(target=srv.serve_forever, name="aria-bridge", daemon=True)
        t.start()
        _BRIDGE_THREAD = t
    return srv


def stop_bridge_server() -> bool:
    """Stop the bridge if running. Returns True if a server was stopped."""
    global _BRIDGE_SERVER, _BRIDGE_THREAD
    srv = _BRIDGE_SERVER
    if srv is None:
        return False
    try:
        srv.shutdown()
    except Exception:
        pass
    try:
        srv.server_close()
    except Exception:
        pass
    _BRIDGE_SERVER = None
    _BRIDGE_THREAD = None
    _log("stopped.")
    return True


def bridge_running() -> bool:
    return _BRIDGE_SERVER is not None


# ---------------------------------------------------------------------------
# Tool registration (the agent starts/stops the bridge on demand)
# ---------------------------------------------------------------------------

def _tool_bridge_start(args: Dict[str, Any]) -> str:
    if bridge_running():
        return f"Phone bridge already running: {get_bridge_url()}"
    if not bridge_token():
        return ("[phone bridge needs a BRIDGE_TOKEN — set it in aria_keys.json "
                "or the BRIDGE_TOKEN env var, then start again]")
    port = args.get("port")
    try:
        port = int(port) if port else None
    except (TypeError, ValueError):
        return "[port must be a number]"
    try:
        start_bridge_server(port=port, blocking=False)
    except OSError as e:
        return f"[could not start phone bridge: {e}]"
    return f"Phone bridge online: {get_bridge_url()}"


def _tool_bridge_stop(args: Dict[str, Any]) -> str:
    if stop_bridge_server():
        return "Phone bridge stopped."
    return "[phone bridge was not running]"


def _tool_bridge_status(args: Dict[str, Any]) -> str:
    lines = [
        f"running: {bridge_running()}",
        f"url: {get_bridge_url()}",
        f"token configured: {bool(bridge_token())}",
        f"tls: {BRIDGE_SCHEME}",
        f"processor wired: {_BRIDGE_PROCESS_CALL is not None}",
        f"chat log wired: {_CHAT_LOG_CALL is not None}",
        f"voice in/out: {HAS_BRIDGE_STT}/{HAS_BRIDGE_TTS}",
        f"vision streams: {HAS_VISION_STREAM}/{HAS_PHONE_CAM}",
    ]
    return "\n".join(lines)


def register(registry) -> None:
    """Register phone-bridge tools. Called by the v2 tool registry loader."""
    registry.register("phone_bridge_start", _tool_bridge_start, {
        "name": "phone_bridge_start",
        "description": "Start the phone bridge HTTPS server (phone control, voice, dashboard) in the background.",
        "parameters": {"type": "object",
                       "properties": {"port": {"type": "integer",
                                               "description": "override port (default 8777)"}}},
    })
    registry.register("phone_bridge_stop", _tool_bridge_stop, {
        "name": "phone_bridge_stop",
        "description": "Stop the phone bridge server.",
        "parameters": {"type": "object", "properties": {}},
    })
    registry.register("phone_bridge_status", _tool_bridge_status, {
        "name": "phone_bridge_status",
        "description": "Report phone bridge state (running, URL, token, wired hooks, capabilities).",
        "parameters": {"type": "object", "properties": {}},
    })
