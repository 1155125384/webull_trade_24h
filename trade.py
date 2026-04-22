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

url = 'https://raw.githubusercontent.com/1155125384/trading_all_day_long/main/etf_data.csv'
response = requests.get(url)

df = pd.read_csv(io.BytesIO(response.content))
print("File downloaded and stored in df successfully.")
ticker_list = df['Ticker'].tolist()

# print(df.to_string())
# print(ticker_list)

filtered_df = df[df['Total_Score'] > 50]
ticker_list_50 = filtered_df['Ticker'].tolist()

# print(filtered_df.to_string())
print(f"Tickers with Total_Score > 50: {ticker_list_50}")

your_app_key = "63d0bd6afb98053ff2ef998f47c7106e"
your_app_secret = "a2f11b87ef8d765c6e07f75e7583ca38"

api_client = ApiClient(your_app_key, your_app_secret, Region.HK.value)
api = API(api_client)

res_acct = api.account.get_app_subscriptions()
account_id = None

result = res_acct.json()
account_id = result[0]['account_id']

# print('App subscriptions:', res_acct.json())
# print("Account id:", account_id)

res_stock = api.account.get_account_position(account_id,page_size=100)
account_position = res_stock.json()
# print("Account Position:", json.dumps(account_position, indent=4, sort_keys=True))

holdings = account_position.get("holdings", [])
current_holdings_list = [item['symbol'] for item in holdings]

print("My Current Holdings:", current_holdings_list)

print(f"{'Symbol':<10} | {'Profit/Loss Rate':<15}")
print("-" * 30)

sorted_holdings = sorted(
    holdings, 
    key=lambda x: float(x.get("unrealized_profit_loss_rate", 0)), 
    reverse=True
)

for item in sorted_holdings:
    u_pnl_rate = float(item.get("unrealized_profit_loss_rate", 0))    
    symbol = item.get("symbol")
    print(f"{symbol:<10} | {u_pnl_rate:.2%}")

tickers_can_be_sell = [
    item["symbol"] 
    for item in holdings 
    if float(item.get("unrealized_profit_loss_rate", 0)) > 0.005 and item["symbol"] != "AAPL"
]

tickers_confirmed_to_sell = [
    t for t in tickers_can_be_sell if t not in ticker_list_50
]

print("\nETFs to sell:", tickers_confirmed_to_sell)

res_orders = api.order.list_open_orders(account_id, 100)
open_orders = res_orders.json()
orders = open_orders.get("orders", [])

print(f"Exiting Orders Count: {len(orders)}")
print("Exiting Orders Symbols:", [order.get("symbol") for order in orders])
# print("Open Orders:", json.dumps(open_orders, indent=4, sort_keys=True))

for order in orders:
    print("-"*50)
    symbol = order.get("symbol")
    client_id = order.get("client_order_id")
    is_update = symbol in ticker_list_50
    
    if is_update:
        print(f"Attempting to update order for: {symbol}")
    else:
        print(f"Attempting to cancel order for: {symbol}")

    res_cancel = api.order.cancel_order(account_id, client_id)

    if res_cancel.status_code == 200:
        order_res = res_cancel.json()
        print(f"Successfully requested cancellation for {symbol}.")
        
        if not is_update:
            print(f"New sell order will be made for {symbol}...")
            
    else:
        print(f"Failed to cancel {symbol}. Status Code: {res_cancel.status_code}")

print("-"*50)

holdings_lookup = {
    item['symbol']: {
        'instrument_id': item['instrument_id'],
        'qty': item['qty'],
        'last_price': item.get('last_price', '0.00') 
    } 
    for item in account_position.get('holdings', [])
}

time.sleep(3)

for symbol in tickers_confirmed_to_sell:
    if symbol in holdings_lookup:
        stock_info = holdings_lookup[symbol]

        current_mkt_price = stock_info['last_price']
        
        is_hk = symbol.isdigit() or ".HK" in symbol.upper()
        cat = Category.HK_STOCK.name if is_hk else Category.US_STOCK.name
        o_type = "ENHANCED_LIMIT" if is_hk else "LIMIT"

        sell_order = {
            "client_order_id": str(uuid.uuid4().hex),
            "instrument_id": int(float(stock_info['instrument_id'])),
            "side": "SELL",
            "tif": "GTC",
            "order_type": o_type, 
            "limit_price": current_mkt_price,
            "qty": int(float(stock_info['qty'])),
            "extended_hours_trading": True
        }

        api.order.add_custom_headers({"category": cat})
        response = api.order.place_order_v2(account_id, sell_order)
        api.order.remove_custom_headers()

        if response.status_code == 200:
            print(f"✅ SUCCESS: Sold {symbol} at {current_mkt_price}")
        else:
            print(f"❌ FAILED: {symbol} | Error: {response.text}")

print("-"*50)

time.sleep(10)
res_bal = api.account.get_account_balance(account_id,"USD")
account_balance = res_bal.json()
current_cash = float(account_balance.get("total_cash_balance", 0))

# print("Account Balance:", json.dumps(account_balance, indent=4, sort_keys=True))
print("Current Cash Balance: USD", current_cash)

MIN_RESERVE_CASH = 200.0 
MAX_BUY_AMOUNT = 720.0 
MAX_LOW_CASH_STRIKES = 10

buy_targets = [t for t in ticker_list_50 if t not in current_holdings_list]

low_cash_counter = 0  

print(f"Starting buy sequence for {len(buy_targets)} targets...")

for symbol in buy_targets:
    if low_cash_counter >= MAX_LOW_CASH_STRIKES:
        print(f"🛑 Stopping: Hit {MAX_LOW_CASH_STRIKES} consecutive 'low cash' skips.")
        break

    if current_cash <= MIN_RESERVE_CASH:
        print(f"🛑 Stopping: Cash balance (${current_cash:.2f}) at or below reserve.")
        break

    inst_id = None
    last_price = 0.0
    active_cat = "US_STOCK" 

    try:
        for cat in ["US_ETF", "US_STOCK"]:
            res_inst = api.instrument.get_instrument([symbol], cat)
            if res_inst.status_code == 200:
                inst_list = res_inst.json()
                if inst_list and len(inst_list) > 0:
                    inst_id = inst_list[0].get('instrument_id')
                    active_cat = cat
                    break 

        if not inst_id:
            print(f"⏩ Skipping {symbol}: Symbol not found.")
            continue

        quote_res = api.market_data.get_snapshot([symbol], active_cat)
        if quote_res.status_code == 200:
            quote_data = quote_res.json()
            if isinstance(quote_data, list) and len(quote_data) > 0:
                last_price = float(quote_data[0].get('price', 0))
        
        if last_price <= 0:
            print(f"⏩ Skipping {symbol}: Price unavailable.")
            continue

        available_to_spend = current_cash - MIN_RESERVE_CASH
        amount_to_spend = min(MAX_BUY_AMOUNT, available_to_spend)
        qty_to_buy = int(amount_to_spend / last_price)

        if qty_to_buy > 0:
            low_cash_counter = 0 
            clean_limit_price = round(last_price, 2)
            print(f"🚀 Preparing Order: {qty_to_buy} shares of {symbol} @ ${clean_limit_price}")
            
            buy_order = {
                "client_order_id": str(uuid.uuid4().hex),
                "instrument_id": int(float(inst_id)), 
                "side": "BUY",
                "tif": "GTC",
                "order_type": "LIMIT", 
                "limit_price": str(clean_limit_price),
                "qty": str(qty_to_buy),
                "extended_hours_trading": True
            }

            api.order.add_custom_headers({"category": "US_STOCK"})
            response = api.order.place_order_v2(account_id, buy_order)
            api.order.remove_custom_headers()
            
            if response.status_code == 200:
                current_cash -= (qty_to_buy * last_price)
                print(f"✅ Success! Remaining Est. Cash: ${current_cash:.2f}")
            else:
                print(f"❌ API Rejected {symbol}: {response.text}")
            
            time.sleep(1) 
        else:
            low_cash_counter += 1
            print(f"⚠️ Skipping {symbol}: Not enough cash for 1 share. (Strike {low_cash_counter}/{MAX_LOW_CASH_STRIKES})")

    except Exception as e:
        print(f"🔥 Error on {symbol}: {e}")

print(f"Final Estimated Cash Balance: ${current_cash:.2f}")
print("-"*50)
print("Completed!")
