"""
Auto Trader v2
--------------
Upgrades over the original script:
  1) Current holdings are classified into STOCK vs ETF via yfinance (quoteType).
  2) Buying is tilted to keep the portfolio near TARGET_ETF_RATIO (default 60% ETF / 40% stock),
     with natural drift allowed rather than a hard split.
  3) Buy candidates are pulled from two separate scanner CSVs (stock + ETF), ordered by a
     tiered preference: within each score threshold (70, 65, 60, ... step -5), candidates
     with the "Downtrend" flag go first, then non-"Uptrend" candidates, then "Uptrend"
     candidates, before moving to the next lower threshold. Already-held tickers are NOT
     excluded — MAX_BUY_AMOUNT (1200 USD) is treated as a cap on total position size per
     symbol, so a held position under the cap can still be topped up (buying only enough
     to reach the cap), while a position already at/over the cap is skipped.
  4) A listed position is only sold if unrealized P/L > SELL_PROFIT_THRESHOLD AND its
     final_score (looked up from whichever scanner list it belongs to) is below
     SELL_SCORE_THRESHOLD. A held ETF that has dropped off the ETF scanner list entirely
     is sold once its P/L exceeds UNLISTED_ETF_SELL_PROFIT_THRESHOLD, regardless of score.
     (See the CONFIG section below for current threshold values.)

Run requirements:
    pip install yfinance tqdm pandas requests

Before using with real money: run at least one full cycle with live order placement
commented out (see PLACE_LIVE_ORDERS below) to confirm the classification, scoring,
and sizing logic behaves the way you expect on your actual holdings.
"""

from webullsdkcore.client import ApiClient
from webullsdktrade.api import API
from webullsdkcore.common.region import Region
from webullsdkmdata.common.category import Category
import json
import requests
import pandas as pd
import io
import uuid
import time
import os
import yfinance as yf
from tqdm import tqdm

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

STOCK_SCANNER_URL = 'https://raw.githubusercontent.com/1155125384/scanner_stock/main/stock_scanner.csv'
ETF_SCANNER_URL   = 'https://raw.githubusercontent.com/1155125384/scanner_etf/main/etf_scanner.csv'

TICKER_COL = 'Ticker'
SCORE_COL  = 'final_score'
FLAG_COL   = 'Flags'

# Buy preference tiers: within each score threshold (70, 65, 60, ... stepping down by
# PREFERENCE_STEP), candidates are ordered: (1) has "Downtrend" flag, (2) does not have
# "Uptrend" flag, (3) has "Uptrend" flag. A ticker is placed in the first/highest tier
# it qualifies for and is not reconsidered in lower tiers.
PREFERENCE_START_THRESHOLD = 67
PREFERENCE_STEP = 5

TARGET_ETF_RATIO = 0.60          # aim for ~60% ETF / 40% stock by market value
MAX_BUY_AMOUNT = 1200.0          # cap on total position size per symbol (existing + new buys)
MIN_TRANSACTION_AMOUNT = 300.0   # don't bother placing tiny orders
MAX_LOW_CASH_STRIKES = 10        # stop trying to buy after this many consecutive skips

SELL_PROFIT_THRESHOLD = 0.003    # +0.3% (used for holdings that ARE in a scanner list)
SELL_SCORE_THRESHOLD = 55        # sell only if score is BELOW this

# ETFs currently held that no longer appear in the ETF scanner CSV at all get sold
# once they're up by at least this much, regardless of score (they have none).
UNLISTED_ETF_SELL_PROFIT_THRESHOLD = 0.005   # +0.5%

EXCLUDE_FROM_SELL = {"FUTU"}  # never auto-sell these

NUM_CYCLES = 14
ODD_WAIT_SECONDS = 60
EVEN_WAIT_SECONDS = 900

PLACE_LIVE_ORDERS = True   # set to False for a dry run that logs intended orders only

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def fetch_csv(url: str) -> pd.DataFrame:
    response = requests.get(url)
    response.raise_for_status()
    return pd.read_csv(io.BytesIO(response.content))


def build_buy_preference_order(df, start_threshold=PREFERENCE_START_THRESHOLD, step=PREFERENCE_STEP):
    """
    Order tickers by preference tier:
      For threshold = 70, 65, 60, ... (stepping down by `step`):
        1) final_score >= threshold AND has "Downtrend" flag
        2) final_score >= threshold AND does NOT have "Uptrend" flag
        3) final_score >= threshold AND has "Uptrend" flag
      Each ticker is claimed by the first (highest-preference) group it matches and is
      skipped in every subsequent group/threshold.
    """
    df = df.copy()
    df[FLAG_COL] = df[FLAG_COL].fillna('')

    is_downtrend = df[FLAG_COL].str.contains('Downtrend')
    is_uptrend = df[FLAG_COL].str.contains('Uptrend')

    assigned = set()
    order = []

    def claim(mask, threshold):
        sub = df[mask & (df[SCORE_COL] >= threshold) & (~df[TICKER_COL].isin(assigned))]
        sub = sub.sort_values(SCORE_COL, ascending=False)
        for t in sub[TICKER_COL]:
            order.append(t)
            assigned.add(t)

    min_score = df[SCORE_COL].min() if len(df) else start_threshold
    threshold = start_threshold

    while threshold > min_score - step:
        claim(is_downtrend, threshold)
        claim(~is_uptrend, threshold)
        claim(is_uptrend, threshold)
        threshold -= step
        if len(assigned) >= len(df):
            break

    # Fallback for anything somehow not captured above (shouldn't normally trigger).
    leftover = df[~df[TICKER_COL].isin(assigned)].sort_values(SCORE_COL, ascending=False)
    order.extend(leftover[TICKER_COL].tolist())

    return order


def load_scanner_lists():
    """Load both scanner CSVs and order each by the tiered buy-preference rule."""
    stock_df = fetch_csv(STOCK_SCANNER_URL)
    etf_df = fetch_csv(ETF_SCANNER_URL)

    stock_scores = dict(zip(stock_df[TICKER_COL], stock_df[SCORE_COL]))
    etf_scores = dict(zip(etf_df[TICKER_COL], etf_df[SCORE_COL]))

    stock_order = build_buy_preference_order(stock_df)
    etf_order = build_buy_preference_order(etf_df)

    return stock_order, stock_scores, etf_order, etf_scores


def classify_holdings(symbols):
    """Classify current holdings into EQUITY / ETF / other via yfinance."""
    results = []
    for ticker in tqdm(symbols, desc="Classifying holdings", unit="ticker"):
        try:
            info = yf.Ticker(ticker).info
            quote_type = info.get('quoteType', 'UNKNOWN')
            long_name = info.get('longName', info.get('shortName', ''))
            results.append({'Ticker': ticker, 'Type': quote_type, 'Name': long_name})
        except Exception as e:
            results.append({'Ticker': ticker, 'Type': 'ERROR', 'Name': str(e)})
        time.sleep(0.3)
    return pd.DataFrame(results)


def get_account_and_client():
    your_app_key = os.getenv('APP_KEY')
    your_app_secret = os.getenv('APP_SECRET')
    api_client = ApiClient(your_app_key, your_app_secret, Region.HK.value)
    api = API(api_client)
    res_acct = api.account.get_app_subscriptions()
    result = res_acct.json()
    account_id = result[0]['account_id']
    return api, account_id


def get_holdings(api, account_id):
    res_stock = api.account.get_account_position(account_id, page_size=100)
    account_position = res_stock.json()
    return account_position.get("holdings", [])


def cancel_orders(api, account_id, keep_symbols):
    """Cancel open orders, except symbols we want to keep pursuing (high score)."""
    res_orders = api.order.list_open_orders(account_id, 100)
    open_orders = res_orders.json()
    orders = open_orders.get("orders", [])
    print(f"Existing Orders Count: {len(orders)}")

    for order in orders:
        symbol = order.get("symbol")
        client_id = order.get("client_order_id")
        is_keep = symbol in keep_symbols
        print(f"{'Leaving/updating' if is_keep else 'Cancelling'} order for {symbol}")

        if is_keep:
            continue

        res_cancel = api.order.cancel_order(account_id, client_id)
        if res_cancel.status_code == 200:
            print(f"✅ Cancelled {symbol}")
        else:
            print(f"❌ Failed to cancel {symbol}. Status: {res_cancel.status_code}")


def place_sell_order(api, account_id, symbol, stock_info):
    is_hk = symbol.isdigit() or ".HK" in symbol.upper()
    cat = Category.HK_STOCK.name if is_hk else Category.US_STOCK.name
    o_type = "ENHANCED_LIMIT" if is_hk else "LIMIT"

    sell_order = {
        "client_order_id": str(uuid.uuid4().hex),
        "instrument_id": int(float(stock_info['instrument_id'])),
        "side": "SELL",
        "tif": "GTC",
        "order_type": o_type,
        "limit_price": stock_info['last_price'],
        "qty": int(float(stock_info['qty'])),
        "extended_hours_trading": True
    }

    if not PLACE_LIVE_ORDERS:
        print(f"[DRY RUN] Would SELL {symbol}: {sell_order}")
        return None

    api.order.add_custom_headers({"category": cat})
    response = api.order.place_order_v2(account_id, sell_order)
    api.order.remove_custom_headers()
    return response


def get_instrument_and_price(api, symbol, is_etf):
    cat_order = ["US_ETF", "US_STOCK"] if is_etf else ["US_STOCK", "US_ETF"]
    inst_id = None
    active_cat = "US_STOCK"

    for cat in cat_order:
        res_inst = api.instrument.get_instrument([symbol], cat)
        if res_inst.status_code == 200:
            inst_list = res_inst.json()
            if inst_list:
                inst_id = inst_list[0].get('instrument_id')
                active_cat = cat
                break

    if not inst_id:
        return None, None, active_cat

    quote_res = api.market_data.get_snapshot([symbol], active_cat)
    last_price = 0.0
    if quote_res.status_code == 200:
        quote_data = quote_res.json()
        if isinstance(quote_data, list) and quote_data:
            last_price = float(quote_data[0].get('price', 0))

    return inst_id, last_price, active_cat


def place_buy_order(api, account_id, symbol, inst_id, qty, limit_price, active_cat):
    buy_order = {
        "client_order_id": str(uuid.uuid4().hex),
        "instrument_id": int(float(inst_id)),
        "side": "BUY",
        "tif": "GTC",
        "order_type": "LIMIT",
        "limit_price": str(round(limit_price, 2)),
        "qty": str(qty),
        "extended_hours_trading": True
    }

    if not PLACE_LIVE_ORDERS:
        print(f"[DRY RUN] Would BUY {symbol}: {buy_order}")
        return None

    api.order.add_custom_headers({"category": active_cat})
    response = api.order.place_order_v2(account_id, buy_order)
    api.order.remove_custom_headers()
    return response


def position_value(item):
    try:
        return float(item.get('qty', 0)) * float(item.get('last_price', 0))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

for cycle in range(1, NUM_CYCLES + 1):
    print("=" * 60)
    print(f"CYCLE {cycle} of {NUM_CYCLES}")
    print("=" * 60)

    # 1) Load & sort both scanner buy lists ---------------------------------
    stock_ranked, stock_scores, etf_ranked, etf_scores = load_scanner_lists()
    print(f"Loaded {len(stock_ranked)} stock candidates, {len(etf_ranked)} ETF candidates.")

    # 2) Connect + fetch current holdings ------------------------------------
    api, account_id = get_account_and_client()
    holdings = get_holdings(api, account_id)
    current_holdings_list = [item['symbol'] for item in holdings]
    print("My Current Holdings:", current_holdings_list)

    # 3) Classify current holdings into STOCK / ETF (requirement 1) ---------
    class_df = classify_holdings(current_holdings_list)
    stock_symbols = set(class_df[class_df['Type'] == 'EQUITY']['Ticker'])
    etf_symbols = set(class_df[class_df['Type'] == 'ETF']['Ticker'])
    other_symbols = set(class_df[~class_df['Type'].isin(['EQUITY', 'ETF'])]['Ticker'])
    if other_symbols:
        print("⚠️ Unclassified holdings (excluded from allocation math):", other_symbols)

    holdings_by_symbol = {item['symbol']: item for item in holdings}
    current_stock_value = sum(position_value(holdings_by_symbol[s]) for s in stock_symbols)
    current_etf_value = sum(position_value(holdings_by_symbol[s]) for s in etf_symbols)
    total_value = current_stock_value + current_etf_value
    current_etf_ratio = (current_etf_value / total_value) if total_value > 0 else TARGET_ETF_RATIO

    print(f"Current allocation -> ETF: ${current_etf_value:.2f} ({current_etf_ratio:.1%}) | "
          f"Stock: ${current_stock_value:.2f} ({1 - current_etf_ratio:.1%})")

    # 4) Show P/L table -------------------------------------------------------
    sorted_holdings = sorted(
        holdings,
        key=lambda x: float(x.get("unrealized_profit_loss_rate", 0)),
        reverse=True
    )
    print(f"{'Symbol':<10} | {'Profit/Loss Rate':<15}")
    print("-" * 30)
    for item in sorted_holdings:
        u_pnl_rate = float(item.get("unrealized_profit_loss_rate", 0))
        print(f"{item.get('symbol'):<10} | {u_pnl_rate:.2%}")

    # 5) Sell logic (requirement 4 + unlisted-ETF rule) -----------------------
    def score_for(symbol):
        if symbol in stock_symbols:
            return stock_scores.get(symbol)
        if symbol in etf_symbols:
            return etf_scores.get(symbol)
        return None

    sell_listed_stock = []
    sell_listed_etf = []
    sell_unlisted_etf = []

    for item in holdings:
        symbol = item.get("symbol")
        if symbol in EXCLUDE_FROM_SELL:
            continue
        pnl_rate = float(item.get("unrealized_profit_loss_rate", 0))
        score = score_for(symbol)

        if score is None:
            # Not present in either scanner list. If it's a currently-held ETF that has
            # dropped off the ETF scanner entirely, sell it once it's up enough.
            if symbol in etf_symbols and pnl_rate > UNLISTED_ETF_SELL_PROFIT_THRESHOLD:
                sell_unlisted_etf.append(symbol)
            # Unlisted stocks (no score, not an ETF) are left alone — no rule for those yet.
            continue

        if pnl_rate > SELL_PROFIT_THRESHOLD and score < SELL_SCORE_THRESHOLD:
            if symbol in etf_symbols:
                sell_listed_etf.append(symbol)
            else:
                sell_listed_stock.append(symbol)

    tickers_confirmed_to_sell = sell_listed_stock + sell_listed_etf + sell_unlisted_etf

    print(f"\nListed stocks to sell (P/L > {SELL_PROFIT_THRESHOLD:.1%} AND "
          f"final_score < {SELL_SCORE_THRESHOLD}): {sell_listed_stock}")
    print(f"Listed ETFs to sell (P/L > {SELL_PROFIT_THRESHOLD:.1%} AND "
          f"final_score < {SELL_SCORE_THRESHOLD}): {sell_listed_etf}")
    print(f"Unlisted ETFs to sell (not in ETF scanner, P/L > "
          f"{UNLISTED_ETF_SELL_PROFIT_THRESHOLD:.1%}): {sell_unlisted_etf}")

    # Anything scoring well enough to keep gets its open order preserved/updated
    keep_symbols = {
        s for s in current_holdings_list
        if score_for(s) is not None and score_for(s) >= SELL_SCORE_THRESHOLD
    }
    cancel_orders(api, account_id, keep_symbols)
    time.sleep(3)

    holdings_lookup = {
        item['symbol']: {
            'instrument_id': item['instrument_id'],
            'qty': item['qty'],
            'last_price': item.get('last_price', '0.00')
        }
        for item in holdings
    }

    for symbol in tickers_confirmed_to_sell:
        stock_info = holdings_lookup.get(symbol)
        if not stock_info:
            continue
        response = place_sell_order(api, account_id, symbol, stock_info)
        if response is None:
            continue
        if response.status_code == 200:
            print(f"✅ SOLD {symbol} at {stock_info['last_price']}")
        else:
            print(f"❌ FAILED to sell {symbol}: {response.text}")

    print("-" * 50)

    # 6) Buy logic (requirements 2 + 3): ranked lists, $1200 total-position cap, 60/40 tilt -
    time.sleep(10)
    res_bal = api.account.get_account_balance(account_id, "USD")
    account_balance = res_bal.json()
    current_cash = float(account_balance.get("total_cash_balance", 0))
    print("Current Cash Balance: USD", current_cash)

    stock_targets = list(stock_ranked)
    etf_targets = list(etf_ranked)

    low_cash_counter = 0
    stock_idx, etf_idx = 0, 0

    print(f"Starting buy sequence. Stock candidates: {len(stock_targets)}, ETF candidates: {len(etf_targets)}")

    while current_cash >= MIN_TRANSACTION_AMOUNT and low_cash_counter < MAX_LOW_CASH_STRIKES:
        if stock_idx >= len(stock_targets) and etf_idx >= len(etf_targets):
            print("🛑 No more buy candidates left.")
            break

        # Decide which bucket to draw from next based on how far we are from target ratio
        projected_total = current_stock_value + current_etf_value
        projected_ratio = (current_etf_value / projected_total) if projected_total > 0 else TARGET_ETF_RATIO
        want_etf = projected_ratio < TARGET_ETF_RATIO

        if want_etf:
            pools = [("ETF", etf_targets, "etf_idx"), ("STOCK", stock_targets, "stock_idx")]
        else:
            pools = [("STOCK", stock_targets, "stock_idx"), ("ETF", etf_targets, "etf_idx")]

        placed = False
        for kind, pool, idx_name in pools:
            idx = etf_idx if idx_name == "etf_idx" else stock_idx
            if idx >= len(pool):
                continue
            symbol = pool[idx]
            if idx_name == "etf_idx":
                etf_idx += 1
            else:
                stock_idx += 1

            try:
                inst_id, last_price, active_cat = get_instrument_and_price(api, symbol, is_etf=(kind == "ETF"))
                if not inst_id or not last_price or last_price <= 0:
                    print(f"⏩ Skipping {symbol}: no instrument/price found.")
                    continue

                existing_position_value = position_value(holdings_by_symbol.get(symbol, {}))
                remaining_room = MAX_BUY_AMOUNT - existing_position_value
                if remaining_room < MIN_TRANSACTION_AMOUNT:
                    print(f"⏩ Skipping {symbol}: already holds ${existing_position_value:.2f} "
                          f"(cap ${MAX_BUY_AMOUNT:.0f}), no room left to add.")
                    continue

                amount_to_spend = min(remaining_room, current_cash)
                if amount_to_spend < MIN_TRANSACTION_AMOUNT:
                    low_cash_counter += 1
                    continue

                qty_to_buy = int(amount_to_spend / last_price)
                if qty_to_buy <= 0:
                    low_cash_counter += 1
                    print(f"⚠️ Skipping {symbol}: price ${last_price:.2f} too high for 1 share within cap.")
                    continue

                actual_order_value = qty_to_buy * last_price
                if actual_order_value < MIN_TRANSACTION_AMOUNT:
                    low_cash_counter += 1
                    print(f"⚠️ Skipping {symbol}: order value ${actual_order_value:.2f} "
                          f"under ${MIN_TRANSACTION_AMOUNT:.0f} minimum.")
                    continue

                response = place_buy_order(api, account_id, symbol, inst_id, qty_to_buy, last_price, active_cat)
                if response is None:
                    # dry run
                    placed = True
                    break
                if response.status_code == 200:
                    current_cash -= actual_order_value
                    if kind == "ETF":
                        current_etf_value += actual_order_value
                    else:
                        current_stock_value += actual_order_value
                    low_cash_counter = 0
                    placed = True
                    print(f"✅ BOUGHT {kind} {symbol}: {qty_to_buy} shares @ ${last_price:.2f} "
                          f"(${actual_order_value:.2f})")
                    time.sleep(1)
                    break
                else:
                    print(f"❌ API rejected {symbol}: {response.text}")
            except Exception as e:
                print(f"🔥 Error buying {symbol}: {e}")

        if not placed:
            low_cash_counter += 1

    print(f"Final Estimated Cash Balance: ${current_cash:.2f}")
    print("-" * 50)
    print(f"Completed cycle {cycle}")

    if cycle % 2 != 0:
        wait_time = ODD_WAIT_SECONDS
        print(f"Waiting {ODD_WAIT_SECONDS}s (odd cycle)...")
    else:
        wait_time = EVEN_WAIT_SECONDS
        print(f"Waiting {EVEN_WAIT_SECONDS}s (even cycle)...")

    time.sleep(wait_time)
