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
MIN_LEADER_TRADE_USD = 5.0       # ignore the leader's tiny noise trades
FIRST_RUN_LOOKBACK_H = 6
SLEEP = 0.15

# --- circuit breakers (the "safety layer") ---
# These operate on the paper bankroll, but prove the mechanism before any
# real money. Drawdown halt writes a lock file you must delete to resume.
DAILY_LOSS_LIMIT_PCT = 0.05      # stop NEW entries for the day after -5% on the day
MAX_DRAWDOWN_PCT     = 0.10      # halt entirely after -10% from peak equity
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE     = os.path.join(DATA_DIR, "state.json")
SNAPSHOTS_FILE = os.path.join(DATA_DIR, "snapshots.json")
HALT_FLAG_FILE = os.path.join(DATA_DIR, "HALTED.flag")
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
            "slippage_log": [], "first_run_done": False,
            # win/loss tracking (resolved on mirrored exits)
            "wins": 0, "losses": 0,
            # circuit-breaker state
            "peak_equity_gbp": START_BANKROLL_GBP,
            "day": "", "day_start_equity_gbp": START_BANKROLL_GBP,
            "halted": False}


def run():
    s = load(STATE_FILE, fresh_state())
    for k, v in fresh_state().items():       # backfill keys for older state files
        s.setdefault(k, v)
    snaps = load(SNAPSHOTS_FILE, [])
    seen = set(s["seen_tx"])
    bankroll = START_BANKROLL_GBP * FX_USD_PER_GBP
    stake = FLAT_STAKE_GBP * FX_USD_PER_GBP
    now = int(time.time())
    cutoff = now - FIRST_RUN_LOOKBACK_H * 3600 if not s["first_run_done"] else 0
    skipped = failed = 0     # per-run counters for the dashboard

    # ---- SAFETY LAYER: evaluate circuit breakers before trading ----
    # Use last snapshot's equity as the reference for this run's gating.
    ref_equity = snaps[-1]["equity_gbp"] if snaps else START_BANKROLL_GBP
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if s["day"] != today:                       # new day -> reset the daily baseline
        s["day"] = today
        s["day_start_equity_gbp"] = ref_equity
    # manual reset: if we were halted but the user deleted the lock file, resume.
    # Re-baseline the peak to current equity so a still-deep drawdown doesn't
    # instantly re-trip the halt — the user has reviewed and chosen to continue.
    if s["halted"] and not os.path.exists(HALT_FLAG_FILE):
        s["halted"] = False
        s["peak_equity_gbp"] = ref_equity
        print("Circuit breaker manually reset (HALTED.flag removed). Resuming, peak re-baselined.")
    peak = max(s["peak_equity_gbp"], ref_equity)
    drawdown_pct = (peak - ref_equity) / peak if peak > 0 else 0.0
    daily_pnl_pct = (ref_equity - s["day_start_equity_gbp"]) / START_BANKROLL_GBP

    if drawdown_pct >= MAX_DRAWDOWN_PCT and not s["halted"]:
        s["halted"] = True
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(HALT_FLAG_FILE, "w") as f:
            f.write(f"Halted {today}: drawdown {drawdown_pct*100:.1f}% from peak "
                    f"£{peak:.2f}. Review, then delete this file to resume.\n")
    entries_blocked = (
        s["halted"] or                                   # hard drawdown halt
        daily_pnl_pct <= -DAILY_LOSS_LIMIT_PCT           # soft daily-loss stop
    )
    if s["halted"]:
        print(f"!! HALTED: drawdown {drawdown_pct*100:.1f}% >= "
              f"{MAX_DRAWDOWN_PCT*100:.0f}%. No new entries until HALTED.flag is deleted.")
    elif entries_blocked:
        print(f"!! Daily-loss stop: down {daily_pnl_pct*100:.1f}% today "
              f"(limit {DAILY_LOSS_LIMIT_PCT*100:.0f}%). No new entries today; exits still run.")

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
            if realised >= 0: s["wins"] += 1
            else:             s["losses"] += 1
            print(f"  SOLD  (mirroring {e['_leader']:>12}) {p['title'][:40]} "
                  f"@ {px:.3f}  realised £{realised/FX_USD_PER_GBP:+.2f}")
            del s["positions"][tok]

    # ---- 2. ENTRIES: mirror buys (with risk rails) ----
    # Entries are skipped entirely while a circuit breaker is tripped; exits above
    # always run so the bot can still de-risk.
    new = 0
    for e in events:
        if entries_blocked:
            break
        if new >= MAX_NEW_PER_RUN or e.get("side") != "BUY":
            continue
        if e.get("timestamp", 0) < cutoff:
            continue
        tok, title = e.get("asset"), e.get("title", "?")
        if not tok or tok in s["positions"]:      # one bite per market
            continue
        their_price = float(e.get("price", 0)) or None
        # min-leader-trade filter: ignore the leader's tiny noise trades
        notional = e.get("usdcSize")
        if notional is None:
            sz = e.get("size")
            notional = (float(sz) * their_price) if (sz and their_price) else None
        if notional is not None and notional < MIN_LEADER_TRADE_USD:
            skipped += 1
            continue
        px = ask(tok); time.sleep(SLEEP)
        if px is None:
            failed += 1
            continue
        if not (MIN_PRICE <= px <= MAX_PRICE):
            skipped += 1
            continue
        deployed = sum(p["cost_usd"] for p in s["positions"].values())
        same_mkt = sum(p["cost_usd"] for p in s["positions"].values() if p["title"] == title)
        if s["cash_usd"] < stake: skipped += 1; continue
        if deployed + stake > bankroll * MAX_DEPLOYED_PCT: skipped += 1; continue
        if same_mkt + stake > bankroll * MAX_MARKET_PCT: skipped += 1; continue

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

    # update peak with live equity and re-check the drawdown halt now
    s["peak_equity_gbp"] = round(max(s["peak_equity_gbp"], equity_gbp), 2)
    dd_now = (s["peak_equity_gbp"] - equity_gbp) / s["peak_equity_gbp"] if s["peak_equity_gbp"] > 0 else 0.0
    if dd_now >= MAX_DRAWDOWN_PCT and not s["halted"]:
        s["halted"] = True
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(HALT_FLAG_FILE, "w") as f:
            f.write(f"Halted {today}: drawdown {dd_now*100:.1f}% from peak "
                    f"£{s['peak_equity_gbp']:.2f}. Review, then delete this file to resume.\n")

    wins, losses = s["wins"], s["losses"]
    resolved = wins + losses
    win_rate = round(wins / resolved, 3) if resolved else None

    snap = {"ts": now, "iso": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
            "equity_gbp": round(equity_gbp, 2),
            "cash_gbp": round(s["cash_usd"] / FX_USD_PER_GBP, 2),
            "positions_gbp": round(pos_usd / FX_USD_PER_GBP, 2),
            "realised_gbp": round(s["realised_usd"] / FX_USD_PER_GBP, 2),
            "pnl_gbp": round(equity_gbp - START_BANKROLL_GBP, 2),
            "open_positions": len(s["positions"]),
            "avg_slippage": avg_slip, "copies_total": len(slips),
            # new: win/loss + safety telemetry for the dashboard
            "wins": wins, "losses": losses, "win_rate": win_rate,
            "skipped": skipped, "failed": failed,
            "halted": s["halted"],
            "drawdown_pct": round(dd_now, 4),
            "peak_equity_gbp": s["peak_equity_gbp"]}
    snaps.append(snap)
    save(STATE_FILE, s)
    save(SNAPSHOTS_FILE, snaps)

    wr_txt = f"{win_rate*100:.0f}% ({wins}W/{losses}L)" if resolved else "n/a"
    print(f"\nEquity £{equity_gbp:,.2f}  |  P&L £{snap['pnl_gbp']:+,.2f}  "
          f"|  {len(s['positions'])} open  |  win rate {wr_txt}  "
          f"|  avg slippage {avg_slip:+.4f} over {len(slips)} copies")
    if slips:
        print("  ^ that average slippage IS the experiment: if it stays "
              "clearly positive, the 'always late' tax is real for us.")


if __name__ == "__main__":
    run()
        tok, title = e.get("asset"), e.get("title", "?")
        if not tok or tok in s["positions"]:      # one bite per market
            continue
        their_price = float(e.get("price", 0)) or None
        # min-leader-trade filter: ignore the leader's tiny noise trades
        notional = e.get("usdcSize")
        if notional is None:
            sz = e.get("size")
            notional = (float(sz) * their_price) if (sz and their_price) else None
        if notional is not None and notional < MIN_LEADER_TRADE_USD:
            skipped += 1
            continue
        px = ask(tok); time.sleep(SLEEP)
        if px is None:
            failed += 1
            continue
        if not (MIN_PRICE <= px <= MAX_PRICE):
            skipped += 1
            continue
        deployed = sum(p["cost_usd"] for p in s["positions"].values())
        same_mkt = sum(p["cost_usd"] for p in s["positions"].values() if p["title"] == title)
        if s["cash_usd"] < stake: skipped += 1; continue
        if deployed + stake > bankroll * MAX_DEPLOYED_PCT: skipped += 1; continue
        if same_mkt + stake > bankroll * MAX_MARKET_PCT: skipped += 1; continue

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

    # update peak with live equity and re-check the drawdown halt now
    s["peak_equity_gbp"] = round(max(s["peak_equity_gbp"], equity_gbp), 2)
    dd_now = (s["peak_equity_gbp"] - equity_gbp) / s["peak_equity_gbp"] if s["peak_equity_gbp"] > 0 else 0.0
    if dd_now >= MAX_DRAWDOWN_PCT and not s["halted"]:
        s["halted"] = True
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(HALT_FLAG_FILE, "w") as f:
            f.write(f"Halted {today}: drawdown {dd_now*100:.1f}% from peak "
                    f"£{s['peak_equity_gbp']:.2f}. Review, then delete this file to resume.\n")

    wins, losses = s["wins"], s["losses"]
    resolved = wins + losses
    win_rate = round(wins / resolved, 3) if resolved else None

    snap = {"ts": now, "iso": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
            "equity_gbp": round(equity_gbp, 2),
            "cash_gbp": round(s["cash_usd"] / FX_USD_PER_GBP, 2),
            "positions_gbp": round(pos_usd / FX_USD_PER_GBP, 2),
            "realised_gbp": round(s["realised_usd"] / FX_USD_PER_GBP, 2),
            "pnl_gbp": round(equity_gbp - START_BANKROLL_GBP, 2),
            "open_positions": len(s["positions"]),
            "avg_slippage": avg_slip, "copies_total": len(slips),
            # new: win/loss + safety telemetry for the dashboard
            "wins": wins, "losses": losses, "win_rate": win_rate,
            "skipped": skipped, "failed": failed,
            "halted": s["halted"],
            "drawdown_pct": round(dd_now, 4),
            "peak_equity_gbp": s["peak_equity_gbp"]}
    snaps.append(snap)
    save(STATE_FILE, s)
    save(SNAPSHOTS_FILE, snaps)

    wr_txt = f"{win_rate*100:.0f}% ({wins}W/{losses}L)" if resolved else "n/a"
    print(f"\nEquity £{equity_gbp:,.2f}  |  P&L £{snap['pnl_gbp']:+,.2f}  "
          f"|  {len(s['positions'])} open  |  win rate {wr_txt}  "
          f"|  avg slippage {avg_slip:+.4f} over {len(slips)} copies")
    if slips:
        print("  ^ that average slippage IS the experiment: if it stays "
              "clearly positive, the 'always late' tax is real for us.")


if __name__ == "__main__":
    run()
