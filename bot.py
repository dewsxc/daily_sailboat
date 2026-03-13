import os
import sqlite3
import yaml
import requests
import datetime
import google.generativeai as genai
from pathlib import Path

# --- Configuration ---
BASE_DIR = Path(__file__).parent
SECRET_FILE = BASE_DIR / "secret.yml"
DB_FILE = BASE_DIR / "chat_history.db"

def load_config():
    if not SECRET_FILE.exists():
        # Using simple print here as log function might not be defined yet or contextually appropriate
        print(f"Error: {SECRET_FILE} not found. Please create it from secret.yml.example.")
        exit(1)
    with open(SECRET_FILE, "r") as f:
        return yaml.safe_load(f)

config = load_config()
TG_TOKEN = config.get("telegram_bot_token")
TG_CHAT_ID = str(config.get("telegram_chat_id"))
GEMINI_API_KEY = config.get("gemini_api_key")

# --- Database ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Updated schema to store full name more accurately
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY,
            chat_id TEXT,
            user_id INTEGER,
            full_name TEXT,
            text TEXT,
            date INTEGER,
            update_id INTEGER
        )
    """)
    # Add indexes for performance optimization
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages (chat_id, date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_update_id ON messages (update_id)")
    conn.commit()
    return conn

# --- Telegram API ---
def fetch_updates(conn):
    cursor = conn.cursor()
    
    # Get the last update_id to use as offset
    cursor.execute("SELECT MAX(update_id) FROM messages")
    last_update_id = cursor.fetchone()[0]
    
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    params = {"limit": 100, "allowed_updates": ["message"]}
    if last_update_id:
        params["offset"] = last_update_id + 1

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        updates = data.get("result", [])
        
        cursor = conn.cursor()
        new_msgs = 0
        for update in updates:
            if "message" in update:
                msg = update["message"]
                chat_id = str(msg["chat"]["id"])
                
                # Filter for the specific chat_id
                if chat_id != TG_CHAT_ID:
                    continue
                
                m_id = msg["message_id"]
                from_user = msg.get("from", {})
                u_id = from_user.get("id")
                
                # Parsing name according to the provided format
                first_name = from_user.get("first_name", "")
                last_name = from_user.get("last_name", "")
                username = from_user.get("username", "")
                
                if username:
                    full_name = f"{first_name} {last_name} (@{username})".strip()
                else:
                    full_name = f"{first_name} {last_name}".strip()
                
                if not full_name:
                    full_name = f"User_{u_id}"
                
                text = msg.get("text", "")
                date = msg.get("date") # Unix timestamp
                
                if text:
                    update_id = update.get("update_id")
                    cursor.execute("""
                        INSERT OR IGNORE INTO messages (message_id, chat_id, user_id, full_name, text, date, update_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (m_id, chat_id, u_id, full_name, text, date, update_id))
                    if cursor.rowcount > 0:
                        new_msgs += 1
        conn.commit()
        print(f"Fetched {len(updates)} updates, saved {new_msgs} new messages for target chat.")
    except Exception as e:
        print(f"Error fetching updates: {e}")

def send_message(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error sending message: {e}")

# --- Gemini API ---
def analyze_with_gemini(content, is_weekly=False):
    # Using official google-generativeai SDK for better stability
    genai.configure(api_key=GEMINI_API_KEY)
    
    # Updated to gemini-3.0-pro as gemini-1.5 is deprecated
    # Note: If the user environment supports it, this model will be used.
    model = genai.GenerativeModel('gemini-3.1-pro-preview')
    
    title = "每週交易心理與諮商總結" if is_weekly else "每日交易心理與諮商分析"
    prompt = f"""
你是一位專業的心理諮商師與交易心理分析專家。
以下是來自用戶的對話紀錄（按時間順序排列）。
請針對這些對話進行深度的心理分析，特別關注交易者的情緒波動、認知偏差、集體焦慮或過度自信。
並以諮商師的角度給予簡短扼要的專業建議，幫助交易者維持良好的心理狀態。

請以繁體中文撰寫一份{title}。

對話紀錄內容：
---
{content}
---
"""
    
    try:
        # The SDK handles gRPC/REST and timeouts internally with better reliability
        response = model.generate_content(prompt)
        
        if response.candidates:
            # Check if there's any content parts
            if response.candidates[0].content.parts:
                return response.text
            else:
                return f"Gemini 回傳內容為空。原因可能是安全過濾或模型拒絕回答。Finish Reason: {response.candidates[0].finish_reason}"
        else:
            return "Gemini 未能生成任何候選回覆。"
            
    except Exception as e:
        return f"Gemini SDK 分析出錯: {e}"

# --- Main Logic ---
def main():
    conn = init_db()
    
    # 1. Fetch and store new messages
    fetch_updates(conn)
    
    # 2. Determine timeframe (UTC+8)
    # The bot is intended to run at 01:00 AM daily.
    # It will analyze messages from (Yesterday 01:00:00) to (Today 01:00:00).
    tz_taiwan = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz_taiwan)
    
    # Set end time to today at 01:00:00
    end_of_period = now.replace(hour=1, minute=0, second=0, microsecond=0)
    # Set start time to 24 hours before end time
    start_of_period = end_of_period - datetime.timedelta(days=1)

    # Weekly report on Saturday 01:00 AM
    is_saturday = (now.weekday() == 5) # 0=Mon, 5=Sat
    
    start_ts = int(start_of_period.timestamp())
    end_ts = int(end_of_period.timestamp())
    
    cursor = conn.cursor()
    if is_saturday:
        # Get messages from the last 7 days (7 * 24h before end_of_period)
        seven_days_ago_ts = int((end_of_period - datetime.timedelta(days=7)).timestamp())
        cursor.execute("""
            SELECT date, full_name, text FROM messages 
            WHERE chat_id = ? AND date >= ? AND date <= ?
            ORDER BY date ASC
        """, (TG_CHAT_ID, seven_days_ago_ts, end_ts))
    else:
        # Get messages from yesterday's range
        cursor.execute("""
            SELECT date, full_name, text FROM messages 
            WHERE chat_id = ? AND date >= ? AND date <= ?
            ORDER BY date ASC LIMIT 100
        """, (TG_CHAT_ID, start_ts, end_ts))
    
    messages = cursor.fetchall()
    
    if not messages:
        print("No messages found for analysis.")
        return

    print(f"Found {len(messages)} messages for {'weekly' if is_saturday else 'daily'} analysis.")

    # 3. Format messages for Gemini (including time)
    content_lines = []
    for m_date, m_user, m_text in messages:
        # Convert unix timestamp to readable time (UTC+8)
        dt = datetime.datetime.fromtimestamp(m_date, datetime.timezone(datetime.timedelta(hours=8)))
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        content_lines.append(f"[{time_str}] {m_user}: {m_text}")
    
    formatted_content = "\n".join(content_lines)
    
    # 4. Analyze
    analysis_result = analyze_with_gemini(formatted_content, is_weekly=is_saturday)
    
    # 5. Send back to Telegram
    send_message(analysis_result)
    
    conn.close()

if __name__ == "__main__":
    main()
