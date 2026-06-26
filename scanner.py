#!/usr/bin/env python3
"""
THE ANALYST — Polymarket arbitrage scanner (paper).

The one place with a real, non-predictive edge: markets where you can buy
BOTH sides for less than £1. Hold one YES + one NO of the same market and
exactly one pays £1 at resolution — guaranteed. If the two best asks sum to
under £1 (after a fee hurdle), that gap is locked profit.

This does NOT predict anything. It's arithmetic. No wallet, no keys, paper only.

Honest caveats baked in:
  - Liquid markets are mostly efficient; real gaps are brief and HFT eats them
    in milliseconds. We can't win that race from here — we just MEASURE how
    often a capturable gap appears, which tells us if there's anything here.
  - We require a 3% gross gap to count as "capturable" (covers fees + gas +
    slippage). Sub-3% gaps are logged as 'theoretical' but not captured.
  - Paper assumes both legs fill at the shown ask. Real capture also needs
    book DEPTH (a v2 check via /book).

Writes:  data/scanner_state.json , data/scanner_snapshots.json
"""

import urllib.request, json, time, os

# ----------------------------- CONFIG -----------------------------
START_BANKROLL_GBP = 1000.0
FX_USD_PER_GBP     = 1.27
UNIVERSE_N         = 150        # how many top markets (by 24h volume) to scan
MIN_LIQUIDITY_USD  = 5_000      # only markets deep enough to plausibly fill
FEE_EDGE           = 0.03       # need a 3% gross gap to clear fees+gas+slippage
STAKE_GBP          = 10.0       # paper stake per captured arb (split across legs)
SLEEP              = 0.12
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE     = os.path.join(DATA_DIR, "scanner_state.json")
SNAPSHOTS_FILE = os.path.join(DATA_DIR, "scanner_snapshots.json")
# ------------------------------------------------------------------

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def universe():
    rows = _get(f"{GAMMA}/markets?active=true&closed=false&limit={UNIVERSE_N}"
                f"&order=volume24hr&ascending=false")
    out = []
    for m in rows:
        try:
            outs = json.loads(m.get("outcomes", "[]"))
            toks = json.loads(m.get("clobTokenIds", "[]"))
        except Exception:
            continue
        if len(outs) != 2 or len(toks) != 2:
            continue
        if not m.get("enableOrderBook") or not m.get("acceptingOrders"):
            continue
        if float(m.get("liquidityNum") or 0) < MIN_LIQUIDITY_USD:
            continue
        out.append({"q": m.get("question", "?"), "tok": toks, "outs": outs,
                    "liq": float(m.get("liquidityNum") or 0)})
    return out


def ask(token_id):   # cost to BUY this token (side=sell = best ask)
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
    mkts = universe()
    print(f"Scanning {len(mkts)} liquid binary markets for YES+NO < £1 ...\n")

    scanned = theoretical = captured = 0
    for m in mkts:
        ay = ask(m["tok"][0]); time.sleep(SLEEP)
        an = ask(m["tok"][1]); time.sleep(SLEEP)
        if ay is None or an is None:
            continue
        scanned += 1
        total = ay + an
        gap = 1.0 - total                      # >0 means both sides cost < £1
        if gap <= 0:
            continue
        theoretical += 1
        tag = "  theoretical"
        if gap >= FEE_EDGE:
            captured += 1
            key = m["tok"][0]
            net = gap - 0.0                     # gross gap; fee hurdle already cleared
            if key not in seen:                # don't double-count same standing gap
                seen.add(key)
                shares = stake / total
                profit = net * shares
                s["locked_profit_usd"] = round(s["locked_profit_usd"] + profit, 2)
                s["captured"].append({"q": m["q"][:60], "yes": ay, "no": an,
                                      "gap": round(gap, 4),
                                      "profit_gbp": round(profit / FX_USD_PER_GBP, 2)})
            tag = "  *** CAPTURABLE ***"
        print(f"  gap {gap*100:5.2f}%  (YES {ay:.3f} + NO {an:.3f} = {total:.3f}){tag}"
              f"  | {m['q'][:44]}")

    s["seen_keys"] = list(seen)[-4000:]
    s["runs"] += 1
    now = int(time.time())
    equity_gbp = START_BANKROLL_GBP + s["locked_profit_usd"] / FX_USD_PER_GBP
    snap = {"ts": now, "iso": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
            "scanned": scanned, "theoretical_gaps": theoretical,
            "capturable_gaps": captured,
            "locked_profit_gbp": round(s["locked_profit_usd"] / FX_USD_PER_GBP, 2),
            "equity_gbp": round(equity_gbp, 2)}
    snaps.append(snap)
    save(STATE_FILE, s)
    save(SNAPSHOTS_FILE, snaps)

    print(f"\n--- SCAN COMPLETE ---")
    print(f"Scanned            : {scanned}")
    print(f"Any sub-£1 gap     : {theoretical}   (theoretical, before costs)")
    print(f"Cleared 3% hurdle  : {captured}   (capturable after fees)")
    print(f"Locked paper profit: £{snap['locked_profit_gbp']:+.2f}  (cumulative)")
    if theoretical and not captured:
        print("  Read: gaps exist but none clear fees — the liquid book is efficient.")
    elif not theoretical:
        print("  Read: zero gaps right now — fully efficient at this moment. Normal.")


if __name__ == "__main__":
    run()
