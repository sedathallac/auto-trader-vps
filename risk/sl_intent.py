"""Single entry point both ``open_coin`` and ``ensure_sl_coverage`` call.

Wiring (pseudocode)::

    # inside open_coin(), right after the position fills:
    ensure_sl_placed(
        symbol, epoch,
        place_sl_fn=lambda s, e: exchange.place_stop_loss(s, sl_price),
        ledger=RISK_LEDGER, algo_id=next_algo_id(), owner="open_coin",
    )

    # inside ensure_sl_coverage() / orphan_killer, for each uncovered position:
    ensure_sl_placed(
        symbol, epoch,
        place_sl_fn=lambda s, e: exchange.place_stop_loss(s, sl_price),
        ledger=RISK_LEDGER, algo_id=next_algo_id(), owner="ensure_sl_coverage",
    )

Because ``claim`` is atomic, only ONE of the two concurrent calls runs
``place_sl_fn``; the other returns ``(False, None)`` without touching the
exchange. No reliance on the ~60s deterministic dedupe.
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple, Any

from .ledger import RiskLedger


def ensure_sl_placed(
    symbol: str,
    epoch: int,
    place_sl_fn: Callable[[str, int], Any],
    *,
    algo_id: int,
    owner: str,
    ledger: Optional[RiskLedger] = None,
    db_path: str = "risk_ledger.db",
) -> Tuple[bool, Any]:
    """Place the SL for (symbol, epoch) at most once across all actors/processes.

    ``place_sl_fn(symbol, epoch)`` is the real exchange call; it runs ONLY if
    this actor wins the intent claim.

    Returns ``(placed, result)``. ``placed is False`` means another actor owns
    the intent (or the DB was busy) and we skipped -- i.e. no duplicate SL.
    """
    led = ledger or RiskLedger(db_path=db_path)
    if not led.claim(symbol, epoch, algo_id=algo_id, owner=owner):
        return (False, None)
    result = place_sl_fn(symbol, epoch)
    return (True, result)
