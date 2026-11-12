# auto-trader-vps

Risk-control core for the trading bot. This repository was started fresh; the
previous `mexc-trading-system` bot has been retired.

## K-4 — `open_coin` intent full prevention

Prevents a **duplicate stop-loss (SL)** when two actors race to protect the same
position:

| Actor | When it places an SL |
|-------|----------------------|
| `open_coin()` | immediately after a position opens |
| `ensure_sl_coverage()` (orphan killer) | on a periodic scan of uncovered positions |

Without coordination both can observe "no SL yet" and each place one. The
deterministic dedupe (keep the largest `algo_id`) only *recovers* from this, with
up to ~60s latency. K-4 **prevents** it at the source with an intent lock.

### How it works

`risk/ledger.py` — `RiskLedger`, a SQLite intent store:

- **WAL** journal mode, `busy_timeout` set.
- `claim(symbol, epoch, algo_id, owner)` runs inside `BEGIN IMMEDIATE`, so the
  write lock is taken up front and the two racing callers are serialized.
- First caller inserts the `(symbol, epoch)` row and **wins** → places the SL.
  Every other caller finds the row and **busy-skips** → does not place.
- **Fail-closed:** if the DB stays locked past `busy_timeout`, `claim` returns
  `False` (skip). Not placing is safe — the orphan killer covers a genuinely
  missing SL on its next pass. Placing twice is the dangerous case, and that is
  what we eliminate.

`risk/sl_intent.py` — `ensure_sl_placed(...)`, the single entry point both
`open_coin` and `ensure_sl_coverage` call. `place_sl_fn` (the real exchange
call) runs only if this actor won the claim.

### Wiring into the bot

```python
from risk.ledger import RiskLedger
from risk.sl_intent import ensure_sl_placed

RISK_LEDGER = RiskLedger(db_path="risk_ledger.db")

# in open_coin(), after the fill:
ensure_sl_placed(symbol, epoch,
                 place_sl_fn=lambda s, e: exchange.place_stop_loss(s, sl_price),
                 ledger=RISK_LEDGER, algo_id=next_algo_id(), owner="open_coin")

# in ensure_sl_coverage() / orphan_killer, per uncovered position:
ensure_sl_placed(symbol, epoch,
                 place_sl_fn=lambda s, e: exchange.place_stop_loss(s, sl_price),
                 ledger=RISK_LEDGER, algo_id=next_algo_id(), owner="ensure_sl_coverage")
```

`epoch` is the coverage window for the position (e.g. a monotonic counter bumped
each time a fresh position is opened for the symbol). It keeps a later, legitimate
re-entry on the same symbol from being blocked by an old intent.

> Scope note (K-4): this changes only SL-placement coordination. Strategy,
> leverage, scanning engine, position sizing and `MAX_OPEN=2` are untouched.

## Tests

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest -v
```

- `test_race_without_intent_places_double_sl` — **RED** condition: the naive
  check-then-place path produces 2 SLs.
- `test_race_with_intent_places_single_sl` — **GREEN**: the same race through
  `ensure_sl_placed` produces exactly 1 SL.
- `test_second_pass_is_idempotent` — a later `ensure_sl_coverage` pass does not
  add a second SL.
