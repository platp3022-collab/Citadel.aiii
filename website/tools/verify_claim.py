#!/usr/bin/env python3
"""Verifies a Deep Dive claim ticket before you pay it out.

A ticket is base64 JSON produced by the game after the player signs it with
their Solana wallet. This checks the ed25519 signature against the wallet in
the ticket, so a made-up score cannot be paid: only the wallet that played can
produce a valid ticket.

    python3 tools/verify_claim.py <ticket>
    python3 tools/verify_claim.py <ticket> --balance          # also check holdings
    python3 tools/verify_claim.py <ticket> --allow-unsigned   # typed-address claims
    cat tickets.txt | python3 tools/verify_claim.py -         # one ticket per line

A ticket from a connected wallet is signed and provable. A ticket from a
pasted address is not: it only says where to send the coins, and anyone could
have typed it. Those need a second check of your own - the chat account, the
balance, a signature you ask for by hand.

Pure standard library - no packages to install.
"""

import argparse
import base64
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone

MINT = "AzoyECzeEbmi3bczngZZnZDj4M9EreNM93mDXqNcpump"
RPC = "https://api.mainnet-beta.solana.com"
NAME, TICKER, MIN_TOKENS = "BITFISH", "$BFISH", 100000

# ------------------------------------------------------------------ base58 --
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(text):
    n = 0
    for ch in text:
        if ch not in B58:
            raise ValueError("not base58: %r" % ch)
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + raw


# ----------------------------------------------------------------- ed25519 --
# Reference implementation, straight from the ed25519 spec. Slow (a fraction of
# a second per check) but exact, and it keeps this script dependency free.
Q = 2 ** 255 - 19
D = -121665 * pow(121666, Q - 2, Q) % Q
I = pow(2, (Q - 1) // 4, Q)


def _xrecover(y):
    xx = (y * y - 1) * pow(D * y * y + 1, Q - 2, Q)
    x = pow(xx, (Q + 3) // 8, Q)
    if (x * x - xx) % Q != 0:
        x = (x * I) % Q
    return Q - x if x % 2 else x


BY = 4 * pow(5, Q - 2, Q) % Q
B = [_xrecover(BY) % Q, BY]


def _add(p, r):
    x1, y1 = p
    x2, y2 = r
    k = D * x1 * x2 * y1 * y2
    x3 = (x1 * y2 + x2 * y1) * pow(1 + k, Q - 2, Q)
    y3 = (y1 * y2 + x1 * x2) * pow(1 - k, Q - 2, Q)
    return [x3 % Q, y3 % Q]


def _mul(p, e):
    result, base = [0, 1], p
    while e:
        if e & 1:
            result = _add(result, base)
        base = _add(base, base)
        e >>= 1
    return result


def _decode_point(raw):
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != (raw[31] >> 7) & 1:
        x = Q - x
    point = [x, y]
    if (-x * x + y * y - 1 - D * x * x * y * y) % Q != 0:
        raise ValueError("point is not on the curve")
    return point


def _encode_point(point):
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def ed25519_verify(signature, message, public_key):
    if len(signature) != 64 or len(public_key) != 32:
        return False
    try:
        r = _decode_point(signature[:32])
        a = _decode_point(public_key)
    except ValueError:
        return False
    s = int.from_bytes(signature[32:], "little")
    h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little")
    return _encode_point(_mul(B, s)) == _encode_point(_add(r, _mul(a, h)))


# ------------------------------------------------------------------ tickets --
def signed_text(ticket):
    """The exact string the page asked the wallet to sign."""
    stamp = datetime.fromtimestamp(ticket["at"] / 1000, tz=timezone.utc)
    iso = stamp.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (stamp.microsecond // 1000)
    return (
        "%s reward claim\nwallet: %s\nscore: %s\nreward: %s %s\ntime: %s"
        % (NAME, ticket["wallet"], ticket["score"], ticket["reward"], TICKER, iso)
    )


def token_balance(owner, rpc=RPC, mint=MINT):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    }).encode()
    req = urllib.request.Request(rpc, payload, {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.load(resp)
    if "error" in body:
        raise RuntimeError(body["error"].get("message", "rpc error"))
    return sum(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0
               for acc in body["result"]["value"])


def check(raw, want_balance=False, rpc=RPC, allow_unsigned=False):
    try:
        ticket = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception as exc:
        return False, "not a ticket (%s)" % exc

    for field in ("wallet", "score", "reward", "at"):
        if field not in ticket:
            return False, "ticket is missing '%s'" % field
    signed = bool(ticket.get("sig"))
    if signed:
        try:
            ok = ed25519_verify(base64.b64decode(ticket["sig"]),
                                signed_text(ticket).encode(),
                                b58decode(ticket["wallet"]))
        except Exception as exc:
            return False, "signature could not be checked (%s)" % exc
        if not ok:
            return False, "SIGNATURE DOES NOT MATCH - do not pay this one"
    elif not allow_unsigned:
        return False, ("UNSIGNED - the player typed an address instead of signing with the "
                       "wallet, so nothing here is proof: not the score, not the ownership. "
                       "Re-run with --allow-unsigned to read it anyway.")

    when = datetime.fromtimestamp(ticket["at"] / 1000, tz=timezone.utc)
    lines = ["signature OK" if signed else
             "UNSIGNED - typed address, verify the player some other way",
             "  wallet : %s" % ticket["wallet"],
             "  score  : %s" % ticket["score"],
             "  reward : %s %s" % (ticket["reward"], TICKER),
             "  played : %s" % when.strftime("%Y-%m-%d %H:%M:%S UTC")]

    if want_balance:
        try:
            held = token_balance(ticket["wallet"], rpc)
            lines.append("  holds  : %s %s%s" % (
                format(int(held), ","), TICKER,
                "" if held >= MIN_TOKENS else "  <-- below the %s minimum" % format(MIN_TOKENS, ",")))
        except Exception as exc:
            lines.append("  holds  : could not read (%s)" % exc)

    return True, "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticket", help="the ticket string, or - to read them from stdin")
    parser.add_argument("--balance", action="store_true",
                        help="also ask the chain what the wallet holds right now")
    parser.add_argument("--rpc", default=RPC, help="Solana RPC endpoint")
    parser.add_argument("--allow-unsigned", action="store_true",
                        help="also print tickets that carry no wallet signature")
    args = parser.parse_args()

    tickets = ([line.strip() for line in sys.stdin if line.strip()]
               if args.ticket == "-" else [args.ticket])

    bad = 0
    for raw in tickets:
        ok, report = check(raw, args.balance, args.rpc, args.allow_unsigned)
        print(("[ok]   " if ok else "[BAD]  ") + report)
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
