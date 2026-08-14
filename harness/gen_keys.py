#!/usr/bin/env python3
"""
gen_keys.py — mint Nostr keypairs for the queen and the worker pool.

Buzz identities are Nostr keypairs. Each agent (orchestrator + N workers) gets its
own secp256k1 key; the private key is written as a bech32 `nsec` (what buzz-cli
wants in BUZZ_PRIVATE_KEY) and the x-only public key is stored in hex for the
harness allowlist.

    python harness/gen_keys.py --workers 4 --out keys.json

Treat keys.json as a secret. Anyone holding an nsec can post as that identity.

Deps: pip install coincurve
"""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path

import coincurve

# --- bech32 (BIP-173), as used by Nostr NIP-19 for nsec/npub ---------------
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _create_checksum(hrp, data):
    values = _hrp_expand(hrp) + data
    polymod = _polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def bech32_encode(hrp: str, data_bytes: bytes) -> str:
    data = _convertbits(list(data_bytes), 8, 5)
    combined = data + _create_checksum(hrp, data)
    return hrp + "1" + "".join(_CHARSET[d] for d in combined)


def new_identity(name: str) -> dict:
    sk = secrets.token_bytes(32)
    priv = coincurve.PrivateKey(sk)
    xonly = priv.public_key.format(compressed=True)[1:]  # 32-byte x-only pubkey
    return {
        "name": name,
        "nsec": bech32_encode("nsec", sk),
        "npub": bech32_encode("npub", xonly),
        "pubkey": xonly.hex(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="keys.json")
    args = ap.parse_args()

    data = {
        "orchestrator": new_identity("queen"),
        "workers": [new_identity(f"worker-{i+1}") for i in range(args.workers)],
    }
    out = Path(args.out)
    out.write_text(json.dumps(data, indent=2))
    try:
        out.chmod(0o600)
    except OSError:
        pass
    print(f"wrote {out} with 1 orchestrator + {args.workers} worker identities")
    print("queen npub:", data["orchestrator"]["npub"])
    for w in data["workers"]:
        print(f"  {w['name']} npub:", w["npub"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
