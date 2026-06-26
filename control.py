#!/usr/bin/env python3
"""
CONTROL — the "random monkey" baseline for the Maverick bake-off.

No money. No wallet. No skill. Each run it picks a few RANDOM liquid markets,
buys £5 of a RANDOM outcome, and holds to resolution. Same £1,000 paper
bankroll and the same risk rails as the copy bot.

Why it exists: it is the benchmark. Over 8 weeks, if the copy bot or the
arbitrage scanners cannot clearly beat this dumb random strategy, then what
looks like "edge" is really just luck. A control is what turns the experiment
from a story into a measurement.

Writes:
  data/control_state.json      -> portfolio (cash, open positions)
  data/control_snapshots.json  -> append-only time series (feeds the dashboard)
"""

import urllib.request, json, time, os, random

# ----------------------------- CONFIG -----------------------------
START_BANKROLL_GBP = 1000.0
FX_USD_PER_GBP     = 1.27
FLAT_STAKE_GBP     = 5.0         # flat £5 per random bet (matches the copy bot)
UNIVERSE_N         = 150         # pull the top markets by 24h volume
MIN_LIQUIDITY_USD  = 5_000       # only markets deep enough to plausibly fill
MAX_NEW_PER_RUN    = 2           # how many random bets to place per tick
MAX_OPEN_POSITIONS = 50          # bound total positions (keeps each run quick)
MAX_DEPLOYED_PCT   = 0.40        # keep >=60% of the bankroll in cash
MIN_PRICE, MAX_PRICE = 0.05, 0.95
SLEEP = 0.12
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE     = os.path.join(DATA_DIR, "control_state.json")
SNAPSHOTS_FILE = os.path.join(DATA_DIR, "control_snapshots.json")
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
        out.append({"q": m.get("question", "?"), "tok": toks, "outs": outs})
    return out


def ask(token_id):   # cost to BUY this token (side=sell = best ask)
    try:
        return float(_get(f"{CLOB}/price?token_id={token_id}&side=sell")["price"])
    except Exception:
        return None


def bid(token_id):   # proceeds to SELL (side=buy = best bid)
    try:
        return float(_get(f"{CLOB}/price?token_id={token_id}&side=buy")["price"])
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


def fresh_state():
    return {"cash_usd": round(START_BANKROLL_GBP * FX_USD_PER_GBP, 2),
            "positions": {}, "picks_total": 0, "first_run_done": False}


def run():
    s = load(STATE_FILE, fresh_state())
    for k, v in fresh_state().items():
        s.setdefault(k, v)
    snaps = load(SNAPSHOTS_FILE, [])
    bankroll = START_BANKROLL_GBP * FX_USD_PER_GBP
    stake = FLAT_STAKE_GBP * FX_USD_PER_GBP
    now = int(time.time())

    mkts = universe()
    random.shuffle(mkts)
    print(f"Universe: {len(mkts)} liquid markets. Holding {len(s['positions'])} positions.")

    # ---- place a few RANDOM bets ----
    new = 0
    for m in mkts:
        if new >= MAX_NEW_PER_RUN or len(s["positions"]) >= MAX_OPEN_POSITIONS:
            break
        side = random.randint(0, 1)            # coin flip: which outcome
        tok = m["tok"][side]
        if tok in s["positions"]:              # one bite per token
            continue
        px = ask(tok); time.sleep(SLEEP)
        if px is None or not (MIN_PRICE <= px <= MAX_PRICE):
            continue
        deployed = sum(p["cost_usd"] for p in s["positions"].values())
        if s["cash_usd"] < stake: continue
        if deployed + stake > bankroll * MAX_DEPLOYED_PCT: continue

        shares = stake / px
        s["positions"][tok] = {"shares": shares, "cost_usd": round(stake, 2),
                               "entry_price": px, "q": m["q"],
                               "outcome": m["outs"][side]}
        s["cash_usd"] = round(s["cash_usd"] - stake, 2)
        s["picks_total"] += 1
        new += 1
        print(f"  BET {m['outs'][side]:>3} @ {px:.3f}  £{FLAT_STAKE_GBP:.0f} | {m['q'][:45]}")

    # ---- mark to market (resolved markets settle toward 0/1 here) ----
    pos_usd = 0.0
    for tok, p in s["positions"].items():
        cur = bid(tok); time.sleep(SLEEP)
        p["last_price"] = cur
        p["value_usd"] = round(p["shares"] * cur, 2) if cur is not None else p["cost_usd"]
        pos_usd += p["value_usd"]

    equity_gbp = (s["cash_usd"] + pos_usd) / FX_USD_PER_GBP
    s["first_run_done"] = True

    snap = {"ts": now, "iso": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
            "equity_gbp": round(equity_gbp, 2),
            "cash_gbp": round(s["cash_usd"] / FX_USD_PER_GBP, 2),
            "positions_gbp": round(pos_usd / FX_USD_PER_GBP, 2),
            "pnl_gbp": round(equity_gbp - START_BANKROLL_GBP, 2),
            "open_positions": len(s["positions"]),
            "picks_total": s["picks_total"]}
    snaps.append(snap)
    save(STATE_FILE, s)
    save(SNAPSHOTS_FILE, snaps)

    print(f"\nEquity £{equity_gbp:,.2f}  |  P&L £{snap['pnl_gbp']:+,.2f}  "
          f"|  {len(s['positions'])} open  |  {s['picks_total']} random bets total")
    print("  ^ this is the monkey. The real strategies have to beat THIS to claim an edge.")


if __name__ == "__main__":
    run()
