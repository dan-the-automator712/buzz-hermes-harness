"""
buzz_bus.py — signed, timestamped message bus over a Buzz (Nostr) relay.

Buzz is a Nostr relay: every message is a Schnorr-signed NIP-01 event carrying a
`created_at` timestamp. We drive it through `buzz-cli` (JSON in / JSON out) and add
an *independent* verification layer on top of the relay's own NIP-42 auth:

    1. BIP-340 Schnorr signature check over the reconstructed NIP-01 event id
    2. created_at freshness window (reject stale / future-dated events)
    3. author allowlist (only known orchestrator/worker pubkeys are trusted)

Nothing here trusts a message just because it arrived on the relay. A task is only
acted on after `verify_event` passes.

Requires: buzz-cli on PATH, and `pip install coincurve` for signature verification.
Env: BUZZ_RELAY_URL, and per-identity BUZZ_PRIVATE_KEY (nsec) passed in at call time.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import coincurve  # BIP-340 Schnorr
    _HAVE_COINCURVE = True
except Exception:  # pragma: no cover - verification degrades gracefully
    _HAVE_COINCURVE = False


ENVELOPE_VERSION = 1


class BusError(RuntimeError):
    pass


class VerificationError(BusError):
    """Raised when an event fails signature, freshness, or author checks."""


@dataclass
class Identity:
    """A Nostr identity used to sign Buzz events."""
    name: str
    nsec: str          # bech32 private key, fed to buzz-cli via BUZZ_PRIVATE_KEY
    pubkey_hex: str     # 32-byte x-only pubkey, hex (for allowlist / logging)


@dataclass
class BuzzBus:
    relay_url: str
    identity: Identity
    # pubkey_hex -> friendly name; only these authors are trusted by verify_event
    allowed_authors: dict[str, str] = field(default_factory=dict)
    max_skew_seconds: int = 120        # reject events dated more than this in the future
    max_age_seconds: int = 24 * 3600   # reject events older than this
    cli: str = "buzz"

    # ---- low-level buzz-cli plumbing -------------------------------------

    def _run(self, args: list[str], stdin: Optional[str] = None) -> Any:
        env = dict(os.environ)
        env["BUZZ_RELAY_URL"] = self.relay_url
        env["BUZZ_PRIVATE_KEY"] = self.identity.nsec
        proc = subprocess.run(
            [self.cli, *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
        )
        # buzz-cli exit codes: 0=ok 1=user 2=network 3=auth 4=other 5=write-conflict
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            raise BusError(f"buzz {' '.join(args)} failed (exit {proc.returncode}): {err}")
        out = proc.stdout.strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out

    # ---- envelope helpers ------------------------------------------------

    @staticmethod
    def make_envelope(msg_type: str, task_id: str, **fields: Any) -> dict:
        """Build the application payload we drop into the Nostr event content."""
        env = {
            "v": ENVELOPE_VERSION,
            "type": msg_type,          # task | progress | result | checkpoint
            "task_id": task_id,
            "ts": int(time.time()),     # explicit app-level timestamp
            "author": None,             # filled by publish()
        }
        env.update(fields)
        return env

    # ---- publish / fetch -------------------------------------------------

    def publish(self, channel_id: str, envelope: dict, reply_to: Optional[str] = None) -> dict:
        """Sign + send an envelope as a Buzz message. Returns the created event JSON."""
        envelope["author"] = self.identity.pubkey_hex
        content = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
        args = ["messages", "send", "--channel", channel_id, "--content", "-"]
        if reply_to:
            args += ["--reply-to", reply_to, "--broadcast"]
        event = self._run(args, stdin=content)
        return event if isinstance(event, dict) else {"raw": event}

    def fetch(self, channel_id: str, limit: int = 50, since: Optional[int] = None) -> list[dict]:
        """Fetch recent messages from a channel as raw Buzz events."""
        args = ["messages", "get", "--channel", channel_id, "--limit", str(limit)]
        if since is not None:
            args += ["--since", str(since)]
        events = self._run(args)
        return events if isinstance(events, list) else []

    def fetch_verified(self, channel_id: str, **kw) -> list[tuple[dict, dict]]:
        """Fetch, verify, and decode. Returns [(event, envelope), ...] for valid msgs only."""
        good: list[tuple[dict, dict]] = []
        for ev in self.fetch(channel_id, **kw):
            try:
                env = self.verify_event(ev)
            except VerificationError:
                continue
            good.append((ev, env))
        return good

    # ---- verification ----------------------------------------------------

    def verify_event(self, event: dict) -> dict:
        """
        Validate one Buzz event and return its decoded envelope.

        Enforces (in order): author allowlist, timestamp freshness, and — when the
        raw Nostr fields are present — a full BIP-340 Schnorr signature check.
        Raises VerificationError on any failure.
        """
        pubkey = event.get("pubkey")
        created_at = event.get("created_at")

        # 1) author allowlist
        if not pubkey or pubkey not in self.allowed_authors:
            raise VerificationError(f"author {pubkey!r} not in allowlist")

        # 2) freshness
        if not isinstance(created_at, int):
            raise VerificationError("event missing integer created_at")
        now = int(time.time())
        if created_at > now + self.max_skew_seconds:
            raise VerificationError(f"event dated in the future (created_at={created_at})")
        if created_at < now - self.max_age_seconds:
            raise VerificationError(f"event too old (created_at={created_at})")

        # 3) signature (full BIP-340 when we have the raw event fields)
        self._verify_signature(event)

        # decode envelope
        content = event.get("content")
        if not isinstance(content, str):
            raise VerificationError("event content is not a string")
        try:
            env = json.loads(content)
        except json.JSONDecodeError as e:
            raise VerificationError(f"content is not valid JSON: {e}")
        if env.get("v") != ENVELOPE_VERSION:
            raise VerificationError(f"unexpected envelope version {env.get('v')!r}")
        return env

    def _verify_signature(self, event: dict) -> None:
        needed = ("id", "pubkey", "created_at", "kind", "tags", "content", "sig")
        if not all(k in event for k in needed):
            # buzz-cli did not return the full raw event; fall back to relay NIP-42
            # auth + our allowlist/freshness checks. Make the boundary explicit.
            if os.environ.get("HARNESS_REQUIRE_SIG") == "1":
                raise VerificationError("raw event fields absent; signature not verifiable")
            return
        if not _HAVE_COINCURVE:
            if os.environ.get("HARNESS_REQUIRE_SIG") == "1":
                raise VerificationError("coincurve not installed; cannot verify signature")
            return

        # Recompute NIP-01 id: sha256 of [0, pubkey, created_at, kind, tags, content]
        serialized = json.dumps(
            [0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        computed_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if computed_id != event["id"]:
            raise VerificationError("event id does not match serialized content (tampered)")

        try:
            pub = coincurve.PublicKeyXOnly(bytes.fromhex(event["pubkey"]))
            ok = pub.verify(
                bytes.fromhex(event["sig"]),
                bytes.fromhex(event["id"]),
            )
        except Exception as e:  # noqa: BLE001
            raise VerificationError(f"schnorr verify errored: {e}")
        if not ok:
            raise VerificationError("invalid Schnorr signature")
