"""Shared error taxonomy (rev 2). Every module maps its failures into
ErrorCategory — no ad-hoc error strings drive control flow."""

from enum import Enum


class ErrorCategory(str, Enum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"   # 429 -> quarantine key 60s, fail over
    BAD_KEY = "bad_key"             # 401/403 -> quarantine key 1h, fail over
    BAD_PAYLOAD = "bad_payload"     # 400 -> OUR bug: fail fast, log exact detail, key untouched
    ROLE_FAILED = "role_failed"     # role tag errored -> park tag 15min, retry default model
    NETWORK = "network"             # timeouts/drops -> fail over, short quarantine
    ALL_DOWN = "all_down"           # nothing left -> one honest message
    UNKNOWN = "unknown"
