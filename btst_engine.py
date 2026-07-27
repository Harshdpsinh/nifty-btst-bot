import os
import pytz
import requests
import datetime
import pandas as pd
import numpy as np
import yfinance as yf

# Timezone Configuration
IST = pytz.timezone("Asia/Kolkata")

# Credentials from environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message: str):
    """Sends HTML formatted notifications to your Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing. Printing to stdout:\n", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

def get_live_daily_data():
    """Fetches live daily candles and computes forming Heikin-Ashi close at 3:20 PM."""
    df = yf.download("^NSEI", period="10d", interval="1d", progress=False)
    if df.empty:
        raise ValueError("Failed to fetch Nifty 50 data from Yahoo Finance.")
    
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Standard Spot Price (Live close of forming candle)
    live_spot = float(df['Close'].iloc[-1])
    open_price = float(df['Open'].iloc[-1])
    high_price = float(df['High'].iloc[-1])
    low_price = float(df['Low'].iloc[-1])

    # Formative Daily Heikin-Ashi Close calculation: (Open + High + Low + Close) / 4
    ha_live_close = (open_price + high_price + low_price + live_spot) / 4.0
    return live_spot, ha_live_close

def run_320_entry_scan():
    """Executes the daily 3:20 PM IST entry scanner."""
    now_ist = datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    
    try:
        live_spot, ha_close = get_live_daily_data()
    except Exception as e:
        send_telegram(f"❌ <b>BTST Scanner Error:</b> {e}")
        return

    divergence = live_spot - ha_close

    if divergence >= 11.0:
        msg = f"""🚨 <b>BTST SIGNAL DETECTED</b> 🚨
------------------------------------
Asset: <b>NIFTY 50 (Spot)</b>
Time: {now_ist}
Timeframe: Daily (1D) Live

📊 <b>MARKET DATA</b>
• Daily Spot Price: {live_spot:.2f}
• Live Daily Heikin-Ashi: {ha_close:.2f}
• Momentum Divergence: <b>+{divergence:.2f} pts</b>
• System Threshold: +11.0 pts

📈 <b>DIRECTION: BUY CALL (CE)</b>
------------------------------------
⚡ <b>ACTIONABLE STEPS</b>
1. Contract: Nifty Call Option (CE)
2. Target Premium: <b>~₹100</b>
3. Order Type: <b>NRML / CNC</b> (Do not use MIS)
4. Window: Execute between 3:21 PM – 3:28 PM IST
------------------------------------
🤖 Exit engine initialized for tomorrow's session."""
        send_telegram(msg)

    elif divergence <= -11.0:
        msg = f"""🚨 <b>BTST SIGNAL DETECTED</b> 🚨
------------------------------------
Asset: <b>NIFTY 50 (Spot)</b>
Time: {now_ist}
Timeframe: Daily (1D) Live

📊 <b>MARKET DATA</b>
• Daily Spot Price: {live_spot:.2f}
• Live Daily Heikin-Ashi: {ha_close:.2f}
• Momentum Divergence: <b>{divergence:.2f} pts</b>
• System Threshold: -11.0 pts

📉 <b>DIRECTION: BUY PUT (PE)</b>
------------------------------------
⚡ <b>ACTIONABLE STEPS</b>
1. Contract: Nifty Put Option (PE)
2. Target Premium: <b>~₹100</b>
3. Order Type: <b>NRML / CNC</b> (Do not use MIS)
4. Window: Execute between 3:21 PM – 3:28 PM IST
------------------------------------
🤖 Exit engine initialized for tomorrow's session."""
        send_telegram(msg)

    else:
        msg = f"""🛡️ <b>BTST SCAN COMPLETE — NO TRADE</b>
------------------------------------
Asset: NIFTY 50 (Spot)
Time: {now_ist}

📊 <b>MARKET DATA</b>
• Daily Spot Price: {live_spot:.2f}
• Live Daily Heikin-Ashi: {ha_close:.2f}
• Momentum Divergence: <b>{divergence:.2f} pts</b>
• Neutral Zone: -11.0 to +11.0 pts

🔒 <b>RESULT: Standby (Capital Preserved)</b>
Divergence is insufficient for high-conviction overnight momentum."""
        send_telegram(msg)

def run_30m_exit_scan():
    """Calculates 30m Heikin-Ashi trend reversal rules for open positions."""
    df_30m = yf.download("^NSEI", period="5d", interval="30m", progress=False)
    if df_30m.empty:
        return

    if isinstance(df_30m.columns, pd.MultiIndex):
        df_30m.columns = df_30m.columns.get_level_values(0)

    ha_df = df_30m.copy()
    ha_df['HA_Close'] = (df_30m['Open'] + df_30m['High'] + df_30m['Low'] + df_30m['Close']) / 4.0
    
    ha_open = [(df_30m['Open'].iloc[0] + df_30m['Close'].iloc[0]) / 2.0]
    for i in range(1, len(df_30m)):
        ha_open.append((ha_open[-1] + ha_df['HA_Close'].iloc[i-1]) / 2.0)
    ha_df['HA_Open'] = ha_open

    ha_df['HA_High'] = ha_df[['High', 'HA_Open', 'HA_Close']].max(axis=1)
    ha_df['HA_Low'] = ha_df[['Low', 'HA_Open', 'HA_Close']].min(axis=1)
    ha_df['Is_Red'] = ha_df['HA_Close'] < ha_df['HA_Open']
    ha_df['Is_Green'] = ha_df['HA_Close'] > ha_df['HA_Open']

    latest_candle = ha_df.iloc[-1]
    prev_candle = ha_df.iloc[-2]

    now_time = datetime.datetime.now(IST).strftime("%H:%M IST")
    
    # 3:13 PM Hard Cutoff Check
    if datetime.datetime.now(IST).hour == 15 and datetime.datetime.now(IST).minute >= 13:
        msg = f"""⏰ <b>TIME CUTOFF REACHED (3:13 PM IST)</b>
------------------------------------
Asset: NIFTY 50

⚡ <b>ACTION REQUIRED:</b>
Square off ALL remaining open lots immediately!
Do not carry this position into a second night."""
        send_telegram(msg)
        return

    # Check for Red Candle Low Break (CE Exit)
    if prev_candle['Is_Red'] and latest_candle['Close'] < prev_candle['HA_Low']:
        msg = f"""🛑 <b>CALL (CE) EXIT SIGNAL TRIGGERED</b>
------------------------------------
Time: {now_time}
Reason: 30-min Red Heikin-Ashi Low Broken

📊 <b>MARKET DATA</b>
• Reference 30m Red HA Low: {prev_candle['HA_Low']:.2f}
• Current Spot Price: {latest_candle['Close']:.2f} (Broken 👇)

⚡ <b>ACTION REQUIRED:</b> Exit ALL open Call (CE) lots!"""
        send_telegram(msg)

    # Check for Green Candle High Break (PE Exit)
    elif prev_candle['Is_Green'] and latest_candle['Close'] > prev_candle['HA_High']:
        msg = f"""🛑 <b>PUT (PE) EXIT SIGNAL TRIGGERED</b>
------------------------------------
Time: {now_time}
Reason: 30-min Green Heikin-Ashi High Broken

📊 <b>MARKET DATA</b>
• Reference 30m Green HA High: {prev_candle['HA_High']:.2f}
• Current Spot Price: {latest_candle['Close']:.2f} (Broken 👆)

⚡ <b>ACTION REQUIRED:</b> Exit ALL open Put (PE) lots!"""
        send_telegram(msg)

if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "entry"
    if mode == "entry":
        run_320_entry_scan()
    elif mode == "exit":
        run_30m_exit_scan()
