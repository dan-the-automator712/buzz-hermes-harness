#!/usr/bin/env python3
"""
test_smoke.py — offline validation of the crypto + governance layers.

Does NOT require buzz-cli, a relay, or Hermes. It forges real NIP-01 Schnorr-signed
events and checks that:
  * a valid event from an allowed author verifies
  * a tampered content is rejected
  * an unknown author is rejected
  * a stale / future-dated event is rejected
  * the governor stops at the 5-turn budget and completes when criteria are met

Run:  python harness/test_smoke.py
"""

from __future__ import annotations

import hashlib
import json
import time

import coincurve

from buzz_bus import BuzzBus, Identity, VerificationError
from governor import Criterion, Decision, Governor, TaskState


def make_signed_event(priv: coincurve.PrivateKey, envelope: dict, created_at=None, kind=1, tags=None):
    tags = tags or []
    created_at = created_at or int(time.time())
    pubkey = priv.public_key.format(compressed=True)[1:].hex()
    content = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
    serialized = json.dumps([0, pubkey, created_at, kind, tags, content],
                            separators=(",", ":"), ensure_ascii=False)
    eid = hashlib.sha256(serialized.encode()).hexdigest()
    sig = priv.sign_schnorr(bytes.fromhex(eid)).hex()
    return {"id": eid, "pubkey": pubkey, "created_at": created_at,
            "kind": kind, "tags": tags, "content": content, "sig": sig}


def main():
    priv = coincurve.PrivateKey()
    pubkey = priv.public_key.format(compressed=True)[1:].hex()
    ident = Identity("worker-1", "nsec-unused-here", pubkey)
    bus = BuzzBus("ws://localhost:3000", ident, allowed_authors={pubkey: "worker-1"})

    env = BuzzBus.make_envelope("task", "t1", goal="do the thing", max_turns=5)

    # 1) valid event
    ev = make_signed_event(priv, env)
    decoded = bus.verify_event(ev)
    assert decoded["task_id"] == "t1", "valid event should decode"
    print("ok  valid signed event verifies")

    # 2) tampered content
    bad = dict(ev)
    tampered = json.loads(ev["content"]); tampered["goal"] = "steal the thing"
    bad["content"] = json.dumps(tampered, separators=(",", ":"), sort_keys=True)
    try:
        bus.verify_event(bad); assert False, "tampered content must fail"
    except VerificationError:
        print("ok  tampered content rejected")

    # 3) unknown author
    other = coincurve.PrivateKey()
    ev2 = make_signed_event(other, env)
    try:
        bus.verify_event(ev2); assert False, "unknown author must fail"
    except VerificationError:
        print("ok  unknown author rejected")

    # 4) stale event
    old = make_signed_event(priv, env, created_at=int(time.time()) - 999999)
    try:
        bus.verify_event(old); assert False, "stale event must fail"
    except VerificationError:
        print("ok  stale timestamp rejected")

    # 5) future-dated event
    future = make_signed_event(priv, env, created_at=int(time.time()) + 99999)
    try:
        bus.verify_event(future); assert False, "future event must fail"
    except VerificationError:
        print("ok  future timestamp rejected")

    # 6) governor: budget stop
    gov = Governor()
    st = TaskState("t1", "goal", criteria=[Criterion("contains", "NEVER")], max_turns=5)
    d = None
    for _ in range(5):
        d = gov.evaluate(st, "still working", None)
    assert d is Decision.CHECKPOINT, f"expected CHECKPOINT at budget, got {d}"
    assert st.turn == 5, "must stop at exactly 5 turns"
    print("ok  governor checkpoints at 5-turn budget")

    # 7) governor: completion
    st2 = TaskState("t2", "goal", criteria=[Criterion("contains", "STATUS: done")], max_turns=5)
    d2 = gov.evaluate(st2, "finished. STATUS: done", None)
    assert d2 is Decision.COMPLETE, f"expected COMPLETE, got {d2}"
    assert st2.turn == 1, "should complete on first satisfying turn"
    print("ok  governor completes when criteria met")

    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
