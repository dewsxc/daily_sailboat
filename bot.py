import os
import sqlite3
import yaml
import requests
import datetime
import argparse
import anthropic
import google.generativeai as genai
from pathlib import Path

# --- Configuration ---
BASE_DIR = Path(__file__).parent
SECRET_FILE = BASE_DIR / "secret.yml"
DB_FILE = BASE_DIR / "chat_history.db"

CLAUDE_MODEL = "claude-opus-4-6"

def load_config():
    if not SECRET_FILE.exists():
        print(f"Error: {SECRET_FILE} not found. Please create it from secret.yml.example.")
        exit(1)
    with open(SECRET_FILE, "r") as f:
        return yaml.safe_load(f)

config = load_config()
TG_TOKEN = config.get("telegram_bot_token")
TG_CHAT_ID = str(config.get("telegram_chat_id"))
GEMINI_API_KEY = config.get("gemini_api_key")
ANTHROPIC_API_KEY = config.get("anthropic_api_key")

# --- Database ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages (chat_id, date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_update_id ON messages (update_id)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            model TEXT NOT NULL,
            content TEXT NOT NULL,
            period_start INTEGER,
            period_end INTEGER,
            created_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn

def save_analysis(conn, analysis_type, model, content, period_start, period_end):
    cursor = conn.cursor()
    now_ts = int(datetime.datetime.now().timestamp())
    cursor.execute("""
        INSERT INTO ai_analyses (type, model, content, period_start, period_end, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (analysis_type, model, content, period_start, period_end, now_ts))
    conn.commit()

# --- Telegram API ---
def fetch_updates(conn):
    cursor = conn.cursor()
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

                if chat_id != TG_CHAT_ID:
                    continue

                m_id = msg["message_id"]
                from_user = msg.get("from", {})
                u_id = from_user.get("id")

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
                date = msg.get("date")

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
    """Send text to Telegram, splitting into chunks if over the 4096-char limit."""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    max_length = 4096
    chunks = [text[i:i+max_length] for i in range(0, len(text), max_length)]
    print(f"傳送訊息至 Telegram（共 {len(text)} 字，{len(chunks)} 則訊息）...")
    for idx, chunk in enumerate(chunks, 1):
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML"
        }
        try:
            resp = requests.post(url, json=payload, timeout=30)
            data = resp.json()
            if data.get("ok"):
                print(f"  訊息 {idx}/{len(chunks)} 傳送成功")
            else:
                print(f"  訊息 {idx}/{len(chunks)} 傳送失敗：{data.get('description', '未知錯誤')}")
                # Retry without parse_mode in case of HTML formatting error
                payload["parse_mode"] = ""
                resp2 = requests.post(url, json=payload, timeout=30)
                data2 = resp2.json()
                if data2.get("ok"):
                    print(f"  訊息 {idx}/{len(chunks)} 重試（純文字）成功")
                else:
                    print(f"  訊息 {idx}/{len(chunks)} 重試失敗：{data2.get('description')}")
        except Exception as e:
            print(f"  訊息 {idx}/{len(chunks)} 傳送例外：{e}")

# --- Gemini API ---
def analyze_with_gemini(content, is_weekly=False):
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-3.1-pro-preview')

    title = "每週交易心理與諮商總結" if is_weekly else "每日交易心理與諮商分析"
    prompt = f"""
你是一位富有同理心的心理諮商師，同時具備深厚的交易心理學知識。
以下是來自用戶的對話紀錄（按時間順序排列）。

請針對這些對話進行深度分析，區分生活與交易兩個面向：
- **生活面**：辨識用戶的認知模式、情緒狀態與心理健康狀況，指出相關心理學現象（如認知扭曲、習得性無助、迴避行為等）。
- **交易面**：辨識情緒波動、認知偏差（如確認偏誤、損失厭惡、過度自信）、焦慮或衝動決策等現象。

**最重要的核心任務**：以助人者的身份，幫助用戶突破心理障礙、解開行動阻力。
請具體指出是什麼心理機制阻礙了用戶採取積極行動，並提供可立即執行的具體建議，
引導用戶從「知道」走向「做到」，展開積極且有建設性的行動。

請以繁體中文撰寫一份{title}，語氣溫暖、直接且具有行動導向。

對話紀錄內容：
---
{content}
---
"""

    try:
        response = model.generate_content(prompt)
        if response.candidates:
            if response.candidates[0].content.parts:
                return response.text
            else:
                return f"Gemini 回傳內容為空。原因可能是安全過濾或模型拒絕回答。Finish Reason: {response.candidates[0].finish_reason}"
        else:
            return "Gemini 未能生成任何候選回覆。"
    except Exception as e:
        return f"Gemini SDK 分析出錯: {e}"

# --- Claude API ---
def analyze_with_claude(content, is_weekly=False):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    title = "每週交易心理與諮商總結" if is_weekly else "每日交易心理與諮商分析"
    prompt = f"""
你是一位富有同理心的心理諮商師，同時具備深厚的交易心理學知識。
以下是來自用戶的對話紀錄（按時間順序排列）。

請針對這些對話進行深度分析，區分生活與交易兩個面向：
- **生活面**：辨識用戶的認知模式、情緒狀態與心理健康狀況，指出相關心理學現象（如認知扭曲、習得性無助、迴避行為等）。
- **交易面**：辨識情緒波動、認知偏差（如確認偏誤、損失厭惡、過度自信）、焦慮或衝動決策等現象。

**最重要的核心任務**：以助人者的身份，幫助用戶突破心理障礙、解開行動阻力。
請具體指出是什麼心理機制阻礙了用戶採取積極行動，並提供可立即執行的具體建議，
引導用戶從「知道」走向「做到」，展開積極且有建設性的行動。

請以繁體中文撰寫一份{title}，語氣溫暖、直接且具有行動導向。

對話紀錄內容：
---
{content}
---
"""

    try:
        with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=64000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            final_message = stream.get_final_message()

        result_text = next(
            (block.text for block in final_message.content if block.type == "text"),
            "Claude 未能生成任何回覆。"
        )
        return result_text

    except anthropic.AuthenticationError:
        return "Claude API 認證失敗，請確認 anthropic_api_key 設定正確。"
    except anthropic.APIError as e:
        return f"Claude API 錯誤: {e}"
    except Exception as e:
        return f"Claude 分析出錯: {e}"

# --- Main Logic ---
def main():
    parser = argparse.ArgumentParser(description="Daily Sailboat Bot")
    parser.add_argument(
        "-t", "--test",
        action="store_true",
        help="測試模式：提取當下到前一天（過去24小時）的對話紀錄進行分析"
    )
    args = parser.parse_args()

    conn = init_db()

    # 1. Fetch and store new messages
    fetch_updates(conn)

    # 2. Determine timeframe (UTC+8)
    tz_taiwan = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz_taiwan)

    today_01 = now.replace(hour=1, minute=0, second=0, microsecond=0)
    is_saturday = (now.weekday() == 5)

    if args.test:
        # 測試模式：過去 24 小時
        end_of_period = now
        start_of_period = now - datetime.timedelta(days=1)
        analysis_type = "daily_test"
        print("測試模式：分析過去24小時的對話紀錄")
    elif is_saturday:
        # 週報：上週日 01:00 ～ 今天（週六）01:00，共 7 天
        end_of_period = today_01
        start_of_period = today_01 - datetime.timedelta(days=6)
        analysis_type = "weekly"
    else:
        # 日報：昨天 01:00 ～ 今天 01:00
        end_of_period = today_01
        start_of_period = today_01 - datetime.timedelta(days=1)
        analysis_type = "daily"

    start_ts = int(start_of_period.timestamp())
    end_ts = int(end_of_period.timestamp())

    cursor = conn.cursor()
    cursor.execute("""
        SELECT date, full_name, text FROM messages
        WHERE chat_id = ? AND date >= ? AND date <= ?
        ORDER BY date ASC
    """, (TG_CHAT_ID, start_ts, end_ts))

    messages = cursor.fetchall()

    if not messages:
        print("No messages found for analysis.")
        return

    print(f"Found {len(messages)} messages for {'weekly' if is_saturday else 'daily'} analysis.")

    # 3. Format messages for Claude (including time)
    content_lines = []
    for m_date, m_user, m_text in messages:
        dt = datetime.datetime.fromtimestamp(m_date, datetime.timezone(datetime.timedelta(hours=8)))
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        content_lines.append(f"[{time_str}] {m_user}: {m_text}")

    formatted_content = "\n".join(content_lines)

    # 4. Analyze with Claude
    print(f"使用模型 {CLAUDE_MODEL} 進行分析...")
    analysis_result = analyze_with_claude(formatted_content, is_weekly=is_saturday)
    print(f"分析完成，結果長度：{len(analysis_result)} 字")
    print(f"--- 分析結果預覽（前200字）---\n{analysis_result[:200]}\n---")

    # 5. Save analysis to database
    save_analysis(conn, analysis_type, CLAUDE_MODEL, analysis_result, start_ts, end_ts)
    print(f"分析結果已儲存至資料庫（模型：{CLAUDE_MODEL}，類型：{analysis_type}）")

    # 6. Send back to Telegram
    send_message(analysis_result)

    conn.close()

if __name__ == "__main__":
    main()
