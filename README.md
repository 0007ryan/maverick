# MAVERICK 🛩️

A **paper** trading-research stack for Polymarket. No money, no wallet, no API
keys, nothing custodial. It pretends you put **£1,000** in and runs three
different strategies side by side for **8 weeks** — a bake-off — so you can see
which (if any) actually clears the bar before a single real penny is at risk.

---

## The three hunters

**1. `maverick.py` — the Copy bot**
Follows the top 40 traders by profit, flat £5 a trade, mirrors their exits too.
Records a **slippage meter**: how much worse our price was than theirs. That
number is the experiment — if it stays clearly positive, the "always late" tax
is real and copying is dead.

**2. `scanner.py` — the Analyst (intra-market arbitrage)**
Hunts single markets where YES + NO cost under £1 (guaranteed profit). Pure
maths, no prediction. *First-run finding: liquid markets are efficient — the
tightest sum was 1.001, i.e. you'd pay a toll, not collect one. The "guaranteed
arb" sold on YouTube doesn't exist where you can actually fill.*

**3. `scanner_combo.py` — the Analyst II (combinatorial arbitrage)**
Sums the YES legs across genuinely mutually-exclusive events (Polymarket's
`negRisk` groups: a match's Draw/Home/Away, "which team wins"). If they sum to
under £1, that's a real outcome-independent gap. *First-run finding: sports are
efficient, but a 5-leg "Fed Decision" market summed to 0.985 — a genuine 1.5%
gap. Below our fee hurdle, but it points at where edge hides: slow multi-leg
markets, not sports.* Deliberately does **not** guess logical links from
wording — that's how you invent fake arbs.

## The honest backdrop
Only ~12.7% of Polymarket users are profitable. A team built the copy+AI-filter
idea and ran it live: ~25% win rate, edge eaten by fees. Liquid arbitrage is
gone. This stack is built to find out, with fake money, whether anything is left
for us — not to promise there is.

---

## Running it
Pure Python standard library, no installs:
```bash
python3 maverick.py        # copy strategy
python3 scanner.py         # intra-market arb
python3 scanner_combo.py   # combinatorial arb
```
Each writes its own files in `data/` (`*_state.json`, `*_snapshots.json`) so the
three strategies stay separate and comparable.

## Always-on (free, no Mac left on)
`.github/workflows/run.yml` runs all three in the cloud every 30 minutes via a
free GitHub Action and commits each snapshot. The dashboard reads the snapshots.
Push to GitHub, enable Actions, done.

## The week-8 gate — NOT now
Before any real money: UK access is the blocker. Polymarket is geo-restricted;
a VPN to dodge that can breach their terms and freeze funds, plus UK regulatory
questions. We do **not** build around that. The paper phase needs none of it —
public read-only data only.

*Not financial advice. A learning tool.*
