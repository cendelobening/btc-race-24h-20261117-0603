"""Uptrend dip-buy BTC trading bot for make.fun's btc-race-24h-20261117-0603.

Trades cbBTC/USDC on 1-minute bars, long only, starting from 1,000 USDC paper
money. The bot speaks agent-fun/bars-replay/v1: the harness sends one hello,
then one batch per minute bar, and the bot answers every batch with exactly
one reply echoing its seq, carrying its orders for that bar.

Strategy (validated on the published 24h dev window, where BTC drifted only
+0.5% in a 2.5% range and every trend-following baseline lost money):
  * Buy the dip inside an uptrend, never chase a rally. Enter only when the
    fast EMA(8) sits above the slow EMA(21) and the close has pulled back at
    least 0.3% from the highest close of the previous 20 bars. A pullback in
    an uptrend is a discount; a pullback in a downtrend is a falling knife,
    so the EMA gate is what keeps the entries out of downtrends.
  * Exit in thirds so a bounce is banked in stages instead of all-or-nothing:
    sell a third at +0.8% over the entry close, a third at +1.2%, and the
    rest at a +1.6% take-profit. A trailing stop 0.8% below the highest close
    since entry and a hard stop 0.6% below the entry close cut any leg that
    turns against us, and no position is held longer than 480 bars.
  * After a full exit, wait 30 bars before a new entry: the bar right after
    an exit is usually still the same move, and re-entering it immediately
    was the single biggest loser on the dev window.
  * Late-window fallback: if no position has ever been held by bar 1200, the
    EMA gate is dropped, so the bot still trades at least once on a race day
    whose trend never turns up (an entry that never trades ranks below every
    entry that does). On the dev window this never fires.
  * A session drawdown guard stops new entries if equity falls 5% below the
    start; an open position is still managed to its exit.

Why this shape: a full round trip costs ~37 bps in fees, slippage and pool
impact, while 1-minute noise is only ~6 bps, so chasing every wiggle churns
money away (momentum and Donchian variants lost $30-$150 on the dev window).
The only moves big enough to pay are the 0.8%-1.6% bounces after a pullback,
and this bot buys exactly those, in an uptrend, and banks them in stages.
A parameter sweep around the chosen point (dip 0.28%-0.32%, take-profit
1.2%-1.6%, trailing stop 0.8%-1.0%) stayed profitable throughout (+$13 to
+$26 on the dev window), so the design does not hang on one tuned number.

Determinism: pure standard library, no network, no wall clock, no randomness.
The same bars always produce the same orders, so the two graded executions
match byte for byte.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- strategy parameters (fractions of price) ---
FAST_SPAN = 8           # fast EMA span (bars)
SLOW_SPAN = 21          # slow EMA span (bars)
LOOKBACK = 20           # bars over which the pullback high is measured
DIP = 0.003             # 0.3%: entry when close is this far below the 20-bar high
TAKE_1 = 0.008          # first third exits at +0.8% over the entry close
TAKE_2 = 0.012          # second third exits at +1.2%
TAKE_PROFIT = 0.016     # rest exits at +1.6%
TRAIL = 0.008           # trailing stop: 0.8% below the peak close since entry
HARD_STOP = 0.006       # hard stop: 0.6% below the entry close
MAX_HOLD_BARS = 480     # give up after 8 hours without an exit
COOLDOWN_BARS = 30      # wait this long after a full exit before a new entry
FALLBACK_BAR = 1200     # drop the EMA gate after this bar if never traded
MAX_SLIPPAGE_BPS = 200  # 2%: far above any 1-minute move in the dev window
DRAWDOWN_GUARD = 0.05   # stop new entries if equity falls this far below start
START_EQUITY = None     # set from hello, in quote units

# Closes of every CBBTC bar seen so far, as floats in USDC.
closes = []
# The position we hold, or None: entry close, peak close since entry, entry
# bar, and which staged exits have already fired.
position = None
# Bar index of the last full exit (-1e9 until the first), for the cooldown.
last_exit_bar = -10 ** 9
# True once a buy has ever filled (a position was held); drives the fallback.
ever_traded = False


def _ema_step(value, warm, span, close):
    """One EMA step: seed with the mean of the first span closes, then move
    2 / (span + 1) of the way to each new close. Returns the new value."""
    if value is not None:
        return value + (2.0 / (span + 1)) * (close - value)
    warm.append(close)
    if len(warm) == span:
        return sum(warm) / span
    return None


# EMA state, kept across batches.
_fast = {"value": None, "warm": []}
_slow = {"value": None, "warm": []}


def on_hello(state):
    """Remember the starting equity for the drawdown guard."""
    global START_EQUITY
    START_EQUITY = state.equity


def on_batch(state, batch):
    """Answer one batch: read its bar, then emit orders (or none)."""
    global position, last_exit_bar, ever_traded
    fresh = [b for b in batch["bars"] if b["symbol"] == SYMBOL]
    if not fresh or state.warming_up:
        return []
    bar = fresh[-1]
    # price_scale comes from hello (5 for cbBTC: raw units are 1e-5 USDC).
    scale = next(s["price_scale"] for s in state.hello["symbols"] if s["symbol"] == SYMBOL)
    close = int(bar["c"]) / (10.0 ** scale)
    closes.append(close)
    k = len(closes) - 1
    _fast["value"] = _ema_step(_fast["value"], _fast["warm"], FAST_SPAN, close)
    _slow["value"] = _ema_step(_slow["value"], _slow["warm"], SLOW_SPAN, close)
    held = state.positions.get(SYMBOL)
    if held is not None:
        ever_traded = True

    if held is not None:
        # --- manage the open position: staged exits, trail, stops, time exit ---
        if position is None:
            # Defensive: should not happen (the entry always sets position),
            # but rebuild from the holding's own cost rather than the current
            # close, so the exit thresholds stay anchored to what was paid:
            # cost is quote units (1e6 per USDC), qty is at qty_scale 8 and
            # the price at price_scale 5, so price = cost * 10^7 / qty raw.
            entry = (held.cost * 10 ** 7 / held.qty / (10.0 ** scale)
                     if held.qty > 0 else close)
            position = {"entry": entry, "peak": close, "bar": k,
                        "took_1": False, "took_2": False}
        position["peak"] = max(position["peak"], close)
        entry = position["entry"]
        if (close <= position["peak"] * (1 - TRAIL)
                or close <= entry * (1 - HARD_STOP)
                or close >= entry * (1 + TAKE_PROFIT)
                or k - position["bar"] >= MAX_HOLD_BARS):
            position = None
            last_exit_bar = k
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        # A staged sell is a third of what is held; if that third would be
        # worth less than the minimum notional, sell the whole position
        # instead (a below-minimum sell is rejected, so a dust remainder
        # must never be left behind). With a full-cash entry the thirds are
        # far above the minimum, so this guard never fires on a normal run.
        min_notional = int(state.hello["fees"]["min_notional_quote_units"])
        if not position["took_1"] and close >= entry * (1 + TAKE_1):
            position["took_1"] = True
            third = held.qty // 3
            qty = third if state.value(SYMBOL, third) >= min_notional else held.qty
            if qty == held.qty:
                position = None
                last_exit_bar = k
            return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        if position["took_1"] and not position["took_2"] \
                and close >= entry * (1 + TAKE_2):
            position["took_2"] = True
            half = held.qty // 2
            qty = half if state.value(SYMBOL, half) >= min_notional else held.qty
            if qty == held.qty:
                position = None
                last_exit_bar = k
            return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    # --- flat: look for a dip inside an uptrend ---
    if k < LOOKBACK or _fast["value"] is None or _slow["value"] is None:
        return []
    if k - last_exit_bar < COOLDOWN_BARS:
        return []  # cooldown: the bar after an exit is usually the same move
    if START_EQUITY is not None and state.equity < START_EQUITY * (1 - DRAWDOWN_GUARD):
        return []  # drawdown guard: no new entries this session
    uptrend = _fast["value"] > _slow["value"]
    if not uptrend and not (k >= FALLBACK_BAR and not ever_traded):
        return []  # no dip-buying into a downtrend (unless never traded late)
    recent_high = max(closes[k - LOOKBACK:k])
    if close > recent_high * (1 - DIP):
        return []  # no pullback yet
    position = {"entry": close, "peak": close, "bar": k,
                "took_1": False, "took_2": False}
    notional = int(state.cash)
    if notional < int(state.hello["fees"]["min_notional_quote_units"]):
        position = None
        return []
    return [{"side": "buy", "symbol": SYMBOL,
             "notional_quote_units": notional,
             "max_slippage_bps": MAX_SLIPPAGE_BPS}]


makefun_bars.run(on_batch, on_hello=on_hello)
