"""hardware_proto.py — canonical robot-body wire protocol for ARIA v2.

The physical body firmware (arduino/aria_body/aria_body.ino) speaks ONE
protocol, and it is this one — verified against the firmware source:

    baud: 115200
    one command per line, LF-terminated
    P<pan>T<tilt>      e.g. b"P90T45\\n"    pan 0..180, tilt 0..90
    W<left>,<right>    e.g. b"W50,-50\\n"   each -100..100, 0 = stopped
    S                  e.g. b"S\\n"          emergency stop (also re-centers
                                             the head: pan 90, tilt 45)

This module owns the protocol (encoders, clamps, port matching, body
state) so every sender encodes the same bytes. It does NOT own the serial
port itself — connection management lives with the caller (currently
toolkits/web.py), which is why this module is pure stdlib and never
raises.

v1 parity notes (from aria/hardware.py in aria-ultimate):
  - send_servo_command() clamp rules        -> clamp_servo()
  - send_drive_command() clamp rules        -> clamp_wheels()
  - get_hardware_status()                   -> HardwareState.snapshot()
  - Arduino/CH340/USB Serial port matching  -> port_matches()

GAPS IN toolkits/web.py (owned by another worker — DO NOT FIX HERE):
  1. _serial_open() uses baud 9600. The firmware runs at 115200 — at 9600
     every byte is garbled and NOTHING is received. Fix:
         serial.Serial(port, 115200, timeout=1)
  2. move_head_servos() sends b"HEAD {pan} {tilt}\\n". The firmware only
     reacts to lines starting with 'P' — this is silently ignored. Fix:
         from aria.tools.hardware_proto import encode_servo
         s.write(encode_servo(pan, tilt)); s.flush()
  3. drive_wheels() sends b"DRIVE {l} {r} {s}\\n". Same problem: the
     firmware only reacts to lines starting with 'W', and it has NO
     seconds parameter — the client must auto-stop. Fix (client-side
     auto-stop, ported from v1 tool_drive):
         from aria.tools.hardware_proto import encode_drive
         import threading
         s.write(encode_drive(left, right)); s.flush()
         if seconds > 0:
             secs = max(0.1, min(30.0, float(seconds)))
             threading.Timer(secs, lambda: _serial_write(encode_stop()),
                             ...).start()   # daemon timer
     Without this the body drives forever when seconds > 0.
  4. body_stop() sends b"STOP\\n". This happens to work ONLY because the
     firmware matches line[0] == 'S' — fragile. Fix:
         from aria.tools.hardware_proto import encode_stop
         s.write(encode_stop()); s.flush()
  5. No port enumeration: v1 init_hardware() scanned list_ports for
     "Arduino"/"CH340"/"USB Serial" and held ONE persistent SERIAL_CONN
     (opening per command resets most Arduinos via DTR). Fix sketch:
         import serial.tools.list_ports
         from aria.tools.hardware_proto import port_matches
         for port in serial.tools.list_ports.comports():
             if port_matches(port.description or ""):
                 SERIAL_CONN = serial.Serial(port.device, 115200, timeout=1)
                 break
     plus ARIA_BODY_SERIAL_URL support via serial.serial_for_url() for
     the virtual body (v1 parity).
  6. No get_hardware_status() equivalent — use HardwareState.snapshot().

Import rule: stdlib only at top level; NO indented imports.
"""
from __future__ import annotations

# --- wire constants (match aria_body.ino exactly) ---------------------------
BAUD = 115200

PAN_MIN, PAN_MAX = 0, 180
TILT_MIN, TILT_MAX = 0, 90
WHEEL_MIN, WHEEL_MAX = -100, 100

HOME_PAN, HOME_TILT = 90, 45

# Substrings the v1 init_hardware() matched against port descriptions.
_PORT_HINTS = ("Arduino", "CH340", "USB Serial")


# --- clamps (v1 send_servo_command / send_drive_command rules) --------------

def clamp_servo(pan: int, tilt: int) -> tuple:
    """Clamp pan to 0..180 and tilt to 0..90 (v1 rules). Never raises."""
    try:
        p = max(PAN_MIN, min(PAN_MAX, int(pan)))
    except Exception:
        p = HOME_PAN
    try:
        t = max(TILT_MIN, min(TILT_MAX, int(tilt)))
    except Exception:
        t = HOME_TILT
    return p, t


def clamp_wheels(left: int, right: int) -> tuple:
    """Clamp wheel speeds to -100..100 (v1 rules). Never raises."""
    def _c(v):
        try:
            return max(WHEEL_MIN, min(WHEEL_MAX, int(v)))
        except Exception:
            return 0
    return _c(left), _c(right)


# --- wire encoders (bytes, LF-terminated, firmware format) ------------------

def encode_servo(pan: int, tilt: int) -> bytes:
    """b"P<pan>T<tilt>\\n" — clamped first (v1 send_servo_command)."""
    p, t = clamp_servo(pan, tilt)
    return f"P{p}T{t}\n".encode("utf-8")


def encode_drive(left: int, right: int) -> bytes:
    """b"W<left>,<right>\\n" — clamped first (v1 send_drive_command)."""
    l, r = clamp_wheels(left, right)
    return f"W{l},{r}\n".encode("utf-8")


def encode_stop() -> bytes:
    """b"S\\n" — emergency stop; firmware also re-centers the head."""
    return b"S\n"


# --- port matching (v1 init_hardware enumeration rule) ----------------------

def port_matches(description: str) -> bool:
    """True when a serial port description looks like the body Arduino."""
    desc = str(description or "")
    return any(h in desc for h in _PORT_HINTS)


# --- body state (v1 SERVO_POS / WHEEL_STATE / get_hardware_status) ----------

class HardwareState:
    """In-memory body state. Callers update it when a command is SENT
    (v1 updated SERVO_POS/WHEEL_STATE even in virtual mode)."""

    def __init__(self):
        self.connected = False
        self.pan = HOME_PAN
        self.tilt = HOME_TILT
        self.wheel_left = 0
        self.wheel_right = 0
        self.serial_available = False

    def note_servo(self, pan: int, tilt: int) -> None:
        self.pan, self.tilt = clamp_servo(pan, tilt)

    def note_drive(self, left: int, right: int) -> None:
        self.wheel_left, self.wheel_right = clamp_wheels(left, right)

    def note_stop(self) -> None:
        self.wheel_left = 0
        self.wheel_right = 0
        self.pan, self.tilt = HOME_PAN, HOME_TILT

    def snapshot(self) -> dict:
        """v1 get_hardware_status() parity."""
        return {
            "connected": self.connected,
            "pan": self.pan,
            "tilt": self.tilt,
            "wheels": {"left": self.wheel_left, "right": self.wheel_right},
            "serial_available": self.serial_available,
        }
