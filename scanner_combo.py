#!/usr/bin/env python3
"""
THE ANALYST II — combinatorial arbitrage scanner (paper).

Polymarket groups some events as mutually-exclusive AND exhaustive (the
`negRisk` flag): a football match's Draw/Home/Away, an election across all
candidates, "which team wins the World Cup" across every team. Exactly ONE
leg resolves YES. So if you buy one YES share of EVERY leg, you are
guaranteed exactly £1 back. If the YES asks across all legs sum to under £1,
that gap is locked, outcome-independent profit.

This is the *only* combinatorial check we trust, and here's why we trust it:
we rely on Polymarket's own negRisk flag to tell us a group is genuinely
exhaustive. We deliberately DO NOT try to infer logical links from question
wording ("before June" implies "before July") — that needs judgement, throws
false positives, and is exactly how you fool yourself into a fake arb.

No wallet, no keys, paper only.

Honest caveats:
  - Needs ALL legs to fill at once. Execution risk scales with leg count, so
    we only 'capture' groups with <= MAX_LEGS_CAPTURE legs; bigger ones are
    logged as theoretical (a 60-leg World Cup arb is not fillable by hand).
  - Skips very large events for speed; their sum needs every leg to be valid.
  - 3% gross hurdle to clear fees + gas + slippage, same as the binary scanner.

Writes:  data/combo_state.json , data/combo_snapshots.json
"""

import urllib.request, json, time, os

# ----------------------------- CONFIG -----------------------------
START_BANKROLL_GBP = 1000.0
FX_USD_PER_GBP     = 1.27
UNIVERSE_EVENTS    = 40        # top events by 24h volume to scan
MIN_LEGS           = 3         # 2-leg groups are just the binary scanner's job
MAX_LEGS_SCAN      = 20        # skip giant events (e.g. 60-team) for speed
MAX_LEGS_CAPTURE   = 8         # only 'capture' if few enough legs to actually fill
FEE_EDGE           = 0.03      # 3% gross gap needed to clear costs
STAKE_GBP          = 10.0
SLEEP              = 0.12
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE     = os.path.join(DATA_DIR, "combo_state.json")
SNAPSHOTS_FILE = os.path.join(DATA_DIR, "combo_snapshots.json")
# ------------------------------------------------------------------

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def neg_risk_events():
    evs = _get(f"{GAMMA}/events?active=true&closed=false&limit={UNIVERSE_EVENTS}"
               f"&order=volume24hr&ascending=false")
    out = []
    for e in evs:
        if not e.get("negRisk"):           # only genuinely mutually-exclusive groups
            continue
        legs = []
        for m in e.get("markets", []):
            if m.get("closed") or not m.get("enableOrderBook"):
                continue
            try:
                toks = json.loads(m.get("clobTokenIds", "[]"))
            except Exception:
                continue
            if len(toks) == 2:
                legs.append({"name": m.get("groupItemTitle") or m.get("question", "?"),
                             "yes": toks[0]})
        if MIN_LEGS <= len(legs) <= MAX_LEGS_SCAN:
            out.append({"title": e.get("title", "?"), "legs": legs})
    return out


def ask(token_id):
    try:
        return float(_get(f"{CLOB}/price?token_id={token_id}&side=sell")["price"])
    except Exception:
        return None


def load(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def run():
    s = load(STATE_FILE, {"locked_profit_usd": 0.0, "captured": [],
                          "seen_keys": [], "runs": 0})
    snaps = load(SNAPSHOTS_FILE, [])
    seen = set(s["seen_keys"])
    stake = STAKE_GBP * FX_USD_PER_GBP

    events = neg_risk_events()
    print(f"Scanning {len(events)} mutually-exclusive events (negRisk) "
          f"for sum(YES) < £1 ...\n")

    scanned = theoretical = captured = 0
    for ev in events:
        asks, ok = [], True
        for leg in ev["legs"]:
            a = ask(leg["yes"]); time.sleep(SLEEP)
            if a is None:
                ok = False; break
            asks.append(a)
        if not ok:
            continue
        scanned += 1
        total = sum(asks)
        gap = 1.0 - total
        n = len(asks)
        if gap <= 0:
            print(f"  sum {total:.3f}  ({n} legs)              | {ev['title'][:46]}")
            continue
        theoretical += 1
        tag = "  theoretical"
        if gap >= FEE_EDGE and n <= MAX_LEGS_CAPTURE:
            captured += 1
            key = ev["legs"][0]["yes"]
            if key not in seen:
                seen.add(key)
                shares = stake / total
                profit = gap * shares
                s["locked_profit_usd"] = round(s["locked_profit_usd"] + profit, 2)
                s["captured"].append({"event": ev["title"][:60], "legs": n,
                                      "sum": round(total, 4),
                                      "profit_gbp": round(profit / FX_USD_PER_GBP, 2)})
            tag = "  *** CAPTURABLE ***"
        elif gap >= FEE_EDGE:
            tag = f"  gap clears fees but {n} legs = too many to fill"
        print(f"  sum {total:.3f}  gap {gap*100:5.2f}%  ({n} legs){tag}"
              f"  | {ev['title'][:40]}")

    s["seen_keys"] = list(seen)[-4000:]
    s["runs"] += 1
    now = int(time.time())
    equity_gbp = START_BANKROLL_GBP + s["locked_profit_usd"] / FX_USD_PER_GBP
    snap = {"ts": now, "iso": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
            "events_scanned": scanned, "theoretical_gaps": theoretical,
            "capturable_gaps": captured,
            "locked_profit_gbp": round(s["locked_profit_usd"] / FX_USD_PER_GBP, 2),
            "equity_gbp": round(equity_gbp, 2)}
    snaps.append(snap)
    save(STATE_FILE, s)
    save(SNAPSHOTS_FILE, snaps)

    print(f"\n--- COMBINATORIAL SCAN COMPLETE ---")
    print(f"Events scanned     : {scanned}")
    print(f"Any sum < £1       : {theoretical}   (theoretical)")
    print(f"Capturable         : {captured}   (clears fees AND few enough legs)")
    print(f"Locked paper profit: £{snap['locked_profit_gbp']:+.2f}  (cumulative)")
    if theoretical and not captured:
        print("  Read: gaps exist but don't survive fees / too many legs to fill.")
    elif not theoretical:
        print("  Read: every group sums to >= £1 right now — efficient. Expected.")


if __name__ == "__main__":
    run()
