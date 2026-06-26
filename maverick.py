#!/usr/bin/env python3
"""
MAVERICK — Polymarket paper copy-trading bot.

No money. No wallet. No API keys. Reads Polymarket's public data and
simulates a virtual £1,000 bankroll that mirrors the top traders.

Strategy (Ryan's spec):
  - follow the top 40 traders ranked by PROFIT (P&L)
  - flat £5 per copied trade, whatever size they bet
  - mirror EXITS too: when a followed trader sells a market we hold, we sell
  - one bite per market (ignore split-fill bursts) for safety
  - hard caps on bets-per-run and total money deployed

It also records a SLIPPAGE METER: for every copy, how much worse our price
was than the trader's own fill. That is the experiment — it measures the
"you're always late" tax that the research warns kills copy trading.

Writes:
  data/state.json      -> live portfolio (cash, open positions, realised P&L)
  data/snapshots.json  -> append-only time series (feeds the dashboard)
"""

import urllib.request, json, time, os

# ----------------------------- CONFIG -----------------------------
START_BANKROLL_GBP = 1000.0
FX_USD_PER_GBP     = 1.27
FOLLOW_N           = 40          # top 40 traders by profit
FLAT_STAKE_GBP     = 5.0         # flat £5 per copied bet
MIN_WALLET_VOL_USD = 100_000     # quality filter: serious traders only
MAX_NEW_PER_RUN    = 8           # max fresh bets per tick
MAX_DEPLOYED_PCT   = 0.60        # keep >=40% of bankroll in cash
MAX_MARKET_PCT     = 0.20        # <=20% of bankroll in any one market
MIN_PRICE, MAX_PRICE = 0.05, 0.95
FIRST_RUN_LOOKBACK_H = 6
SLEEP = 0.15
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE     = os.path.join(DATA_DIR, "state.json")
SNAPSHOTS_FILE = os.path.join(DATA_DIR, "snapshots.json")
# ------------------------------------------------------------------

DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def top_traders(n):
    rows = _get(f"{DATA}/v1/leaderboard?window=MONTH&category=OVERALL&limit=200")
    good = [w for w in rows if float(w.get("pnl", 0)) > 0
            and float(w.get("vol", 0)) >= MIN_WALLET_VOL_USD]
    good.sort(key=lambda w: float(w.get("pnl", 0)), reverse=True)   # rank by PROFIT
    return good[:n]


def recent_trades(wallet, limit=10):
    try:
        return _get(f"{DATA}/activity?user={wallet}&limit={limit}")
    except Exception:
        return []


def ask(token_id):   # cost to BUY  (side=sell returns best ask)
    try:
        return float(_get(f"{CLOB}/price?token_id={token_id}&side=sell")["price"])
    except Exception:
        return None


def bid(token_id):   # proceeds to SELL (side=buy returns best bid)
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
            "positions": {}, "seen_tx": [], "realised_usd": 0.0,
            "slippage_log": [], "first_run_done": False}


def run():
    s = load(STATE_FILE, fresh_state())
    snaps = load(SNAPSHOTS_FILE, [])
    seen = set(s["seen_tx"])
    bankroll = START_BANKROLL_GBP * FX_USD_PER_GBP
    stake = FLAT_STAKE_GBP * FX_USD_PER_GBP
    now = int(time.time())
    cutoff = now - FIRST_RUN_LOOKBACK_H * 3600 if not s["first_run_done"] else 0

    leaders = top_traders(FOLLOW_N)
    print(f"Following {len(leaders)} traders ranked by profit "
          f"(top by P&L, vol>=${MIN_WALLET_VOL_USD:,.0f}).")

    # gather every fresh event across followed wallets
    events = []
    for w in leaders:
        uname = w.get("userName") or w["proxyWallet"][:8]
        for t in recent_trades(w["proxyWallet"]):
            tx = t.get("transactionHash")
            if not tx or tx in seen or t.get("type") != "TRADE":
                continue
            seen.add(tx)
            t["_leader"] = uname
            events.append(t)
        time.sleep(SLEEP)
    events.sort(key=lambda e: e.get("timestamp", 0))

    # ---- 1. EXITS: mirror sells on anything we hold ----
    for e in events:
        if e.get("side") != "SELL":
            continue
        tok = e.get("asset")
        if tok in s["positions"]:
            p = s["positions"][tok]
            px = bid(tok); time.sleep(SLEEP)
            if px is None:
                continue
            proceeds = p["shares"] * px
            realised = proceeds - p["cost_usd"]
            s["cash_usd"] = round(s["cash_usd"] + proceeds, 2)
            s["realised_usd"] = round(s["realised_usd"] + realised, 2)
            print(f"  SOLD  (mirroring {e['_leader']:>12}) {p['title'][:40]} "
                  f"@ {px:.3f}  realised £{realised/FX_USD_PER_GBP:+.2f}")
            del s["positions"][tok]

    # ---- 2. ENTRIES: mirror buys (with risk rails) ----
    new = 0
    for e in events:
        if new >= MAX_NEW_PER_RUN or e.get("side") != "BUY":
            continue
        if e.get("timestamp", 0) < cutoff:
            continue
        tok, title = e.get("asset"), e.get("title", "?")
        if not tok or tok in s["positions"]:      # one bite per market
            continue
        their_price = float(e.get("price", 0)) or None
        px = ask(tok); time.sleep(SLEEP)
        if px is None or not (MIN_PRICE <= px <= MAX_PRICE):
            continue
        deployed = sum(p["cost_usd"] for p in s["positions"].values())
        same_mkt = sum(p["cost_usd"] for p in s["positions"].values() if p["title"] == title)
        if s["cash_usd"] < stake: continue
        if deployed + stake > bankroll * MAX_DEPLOYED_PCT: continue
        if same_mkt + stake > bankroll * MAX_MARKET_PCT: continue

        shares = stake / px
        s["positions"][tok] = {"shares": shares, "cost_usd": round(stake, 2),
                               "entry_price": px, "their_price": their_price,
                               "title": title, "outcome": e.get("outcome"),
                               "leader": e["_leader"]}
        s["cash_usd"] = round(s["cash_usd"] - stake, 2)
        if their_price:
            slip = px - their_price                # +ve = we paid more (the late tax)
            s["slippage_log"].append(round(slip, 4))
        new += 1
        slip_txt = f"  (slip {px - their_price:+.3f} vs their {their_price:.3f})" if their_price else ""
        print(f"  COPIED {e['_leader']:>12} BUY {e.get('outcome'):>3} @ {px:.3f}"
              f"  £{FLAT_STAKE_GBP:.0f}{slip_txt} | {title[:40]}")

    # ---- mark to market + snapshot ----
    pos_usd = 0.0
    for tok, p in s["positions"].items():
        cur = bid(tok); time.sleep(SLEEP)
        p["last_price"] = cur
        p["value_usd"] = round(p["shares"] * cur, 2) if cur is not None else p["cost_usd"]
        pos_usd += p["value_usd"]

    equity_gbp = (s["cash_usd"] + pos_usd) / FX_USD_PER_GBP
    slips = s["slippage_log"]
    avg_slip = round(sum(slips) / len(slips), 4) if slips else 0.0
    s["seen_tx"] = list(seen)[-8000:]
    s["first_run_done"] = True

    snap = {"ts": now, "iso": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
            "equity_gbp": round(equity_gbp, 2),
            "cash_gbp": round(s["cash_usd"] / FX_USD_PER_GBP, 2),
            "positions_gbp": round(pos_usd / FX_USD_PER_GBP, 2),
            "realised_gbp": round(s["realised_usd"] / FX_USD_PER_GBP, 2),
            "pnl_gbp": round(equity_gbp - START_BANKROLL_GBP, 2),
            "open_positions": len(s["positions"]),
            "avg_slippage": avg_slip, "copies_total": len(slips)}
    snaps.append(snap)
    save(STATE_FILE, s)
    save(SNAPSHOTS_FILE, snaps)

    print(f"\nEquity £{equity_gbp:,.2f}  |  P&L £{snap['pnl_gbp']:+,.2f}  "
          f"|  {len(s['positions'])} open  |  avg slippage {avg_slip:+.4f} "
          f"over {len(slips)} copies")
    if slips:
        print("  ^ that average slippage IS the experiment: if it stays "
              "clearly positive, the 'always late' tax is real for us.")


if __name__ == "__main__":
    run()
