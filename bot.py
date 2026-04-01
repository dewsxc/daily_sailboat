import os
import sqlite3
import yaml
import requests
import datetime
import argparse
import time
from pathlib import Path

# --- Configuration ---
BASE_DIR = Path(__file__).parent
SECRET_FILE = BASE_DIR / "secret.yml"
DB_FILE = BASE_DIR / "chat_history.db"

MODEL_MAP = {
    "gemini":  "gemini-3.1-pro-preview",
    "sonnet":  "claude-sonnet-4-6",
    "claude":  "claude-sonnet-4-6",
    "opus":    "claude-opus-4-6",
}
DEFAULT_MODEL = "sonnet"

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

# --- Retry helper ---
_TG_NETWORK_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

def _retry(func, label, retryable_errors, max_retries=3):
    """Call func(), retrying on retryable_errors with exponential backoff."""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except retryable_errors as e:
            last_error = e
            wait = 2 ** attempt
            print(f"{label} 網路錯誤（第 {attempt}/{max_retries} 次）：{e}，{wait} 秒後重試...")
            time.sleep(wait)
    raise last_error

DAILY_PROMPT = """
你是一位富有同理心的心理諮商師與思緒整理者，具備深厚的心理學與交易心理知識。
以下是來自用戶今天的語音紀錄（按時間順序排列）。

**定位說明**：
這是一份「每日思緒整理」。由於使用者的紀錄可能只是當下的情緒抒發、碎唸或還在成形中的想法，
你的任務是「擔任一面鏡子」，客觀、溫暖地反映使用者今天的狀態，不需要急著給出結論或強烈的行動指引。

**任務要求**：
1. **內容精煉**：將語音紀錄內容進行結構化整理，以簡短不失原意的條列方式呈現。
2. **思緒與情緒反映**：
   - 辨識出使用者今天提到的關鍵事件、情緒狀態（如焦慮、興奮、迷茫等）以及當下的思考重點。
   - 不需要過度解讀，而是以「我聽到了你今天在關注...」的方式進行反映。
3. **保留空間**：不需要給出「具體可執行的行動建議」，而是提出 1~2 個溫和的「反向提問」或「延伸思考」，幫助使用者在接下來的思考中自行理清脈絡。

請以繁體中文撰寫一份{}，語氣溫暖、平靜且具有支持性。

對話紀錄內容：
---
{}
---
"""

WEEKLY_PROMPT = """
你是一位富有洞察力的心理諮商師與資深交易心理教練。
以下是來自用戶過去一週的語音紀錄（按時間順序排列）。

**定位說明**：
這是一份「每週深度分析」。透過一整週的紀錄，我們能看見單日紀錄中看不見的「規律」與「脈絡」。
你的任務是幫助用戶識別出其反覆出現的心理機制，並協助其打破慣性，採取行動。

**任務要求**：
1. **內容精煉**：將本週紀錄內容精煉，以簡短不失原意的條列方式呈現，幫助用戶快速回顧。
2. **深度脈絡分析**：區分「生活」與「交易」兩個面向，指出其中的規律：
   - **生活面**：辨識用戶的認知模式、長期的情緒趨勢與心理健康狀況。
   - **交易面**：辨識情緒波動的規律、反覆出現的認知偏差（如確認偏誤、損失厭惡、過度自信）等、以及決策中的心理陷阱。
3. **最重要的核心任務**：以助人者的身份，幫助用戶突破心理障礙、解開行動阻力。
   - 根據本週的脈絡，具體指出是什麼深層心理機制阻礙了用戶採取積極行動。
   - 提供「可立即執行」且「具體」的行動建議，引導用戶從「知道」走向「做到」。

請以繁體中文撰寫一份{}，語氣堅定、溫暖、直接且具有強烈的行動導向。

對話紀錄內容：
---
{}
---
"""

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
        response = _retry(
            lambda: requests.get(url, params=params, timeout=10),
            "Telegram getUpdates", _TG_NETWORK_ERRORS
        )
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
            resp = _retry(
                lambda: requests.post(url, json=payload, timeout=30),
                f"Telegram sendMessage {idx}", _TG_NETWORK_ERRORS
            )
            data = resp.json()
            if data.get("ok"):
                print(f"  訊息 {idx}/{len(chunks)} 傳送成功")
            else:
                print(f"  訊息 {idx}/{len(chunks)} 傳送失敗：{data.get('description', '未知錯誤')}")
                # Retry without parse_mode in case of HTML formatting error
                payload["parse_mode"] = ""
                resp2 = _retry(
                    lambda: requests.post(url, json=payload, timeout=30),
                    f"Telegram sendMessage {idx} (純文字)", _TG_NETWORK_ERRORS
                )
                data2 = resp2.json()
                if data2.get("ok"):
                    print(f"  訊息 {idx}/{len(chunks)} 重試（純文字）成功")
                else:
                    print(f"  訊息 {idx}/{len(chunks)} 重試失敗：{data2.get('description')}")
        except Exception as e:
            print(f"  訊息 {idx}/{len(chunks)} 傳送例外：{e}")

# --- Gemini API ---
def analyze_with_gemini(content, is_weekly=False, max_retries=3):
    import google.generativeai as genai
    import google.api_core.exceptions as google_exc
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-3.1-pro-preview')

    title = "每週交易心理與諮商總結" if is_weekly else "每日交易心理與諮商分析"
    prompt_template = WEEKLY_PROMPT if is_weekly else DAILY_PROMPT
    prompt = prompt_template.format(title, content)

    _GEMINI_NETWORK_ERRORS = (
        google_exc.ServiceUnavailable,
        google_exc.DeadlineExceeded,
        google_exc.InternalServerError,
    )

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = model.generate_content(prompt)
            if response.candidates:
                if response.candidates[0].content.parts:
                    return response.text
                else:
                    return f"Gemini 回傳內容為空。原因可能是安全過濾或模型拒絕回答。Finish Reason: {response.candidates[0].finish_reason}"
            else:
                return "Gemini 未能生成任何候選回覆。"
        except _GEMINI_NETWORK_ERRORS as e:
            last_error = e
            wait = 2 ** attempt
            print(f"Gemini 網路錯誤（第 {attempt}/{max_retries} 次）：{e}，{wait} 秒後重試...")
            time.sleep(wait)
        except Exception as e:
            return f"Gemini SDK 分析出錯: {e}"

    return f"Gemini 分析出錯（已重試 {max_retries} 次）: {last_error}"

# --- Claude API ---
def analyze_with_claude(content, is_weekly=False, model=None, max_retries=3):
    import anthropic
    if model is None:
        model = MODEL_MAP[DEFAULT_MODEL]
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    title = "每週交易心理與諮商總結" if is_weekly else "每日交易心理與諮商分析"
    prompt_template = WEEKLY_PROMPT if is_weekly else DAILY_PROMPT
    prompt = prompt_template.format(title, content)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            with client.messages.stream(
                model=model,
                max_tokens=64000,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}]
            ) as stream:
                final_message = stream.get_final_message()

            return next(
                (block.text for block in final_message.content if block.type == "text"),
                "Claude 未能生成任何回覆。"
            )

        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            last_error = e
            wait = 2 ** attempt
            print(f"Claude 網路錯誤（第 {attempt}/{max_retries} 次）：{e}，{wait} 秒後重試...")
            time.sleep(wait)
        except Exception as e:
            import httpx, httpcore
            if isinstance(e, (httpx.RemoteProtocolError, httpcore.RemoteProtocolError,
                               httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout)):
                last_error = e
                wait = 2 ** attempt
                print(f"Claude 網路錯誤（第 {attempt}/{max_retries} 次）：{e}，{wait} 秒後重試...")
                time.sleep(wait)
            elif isinstance(e, anthropic.AuthenticationError):
                return "Claude API 認證失敗，請確認 anthropic_api_key 設定正確。"
            elif isinstance(e, anthropic.APIStatusError) and e.status_code in (429, 529):
                last_error = e
                wait = 2 ** attempt
                print(f"Claude 服務過載（第 {attempt}/{max_retries} 次）：{e}，{wait} 秒後重試...")
                time.sleep(wait)
            elif isinstance(e, anthropic.APIError):
                return f"Claude API 錯誤: {e}"
            else:
                return f"Claude 分析出錯: {e}"

    return f"Claude 分析出錯（已重試 {max_retries} 次）: {last_error}"

# --- Main Logic ---
def main():
    parser = argparse.ArgumentParser(description="Daily Sailboat Bot")
    parser.add_argument(
        "-t", "--test",
        action="store_true",
        help="測試模式：提取當下到前一天（過去24小時）的對話紀錄進行分析"
    )
    parser.add_argument(
        "-m", "--model",
        choices=list(MODEL_MAP.keys()),
        default=DEFAULT_MODEL,
        help=f"選擇 AI 模型（預設：{DEFAULT_MODEL}）。選項：{', '.join(f'{k}={v}' for k, v in MODEL_MAP.items())}"
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
        # 測試模式：過去 24 小時，強制設為非週報
        end_of_period = now
        start_of_period = now - datetime.timedelta(days=1)
        analysis_type = "daily_test"
        is_saturday = False
        print("測試模式：分析過去24小時的對話紀錄")
    elif is_saturday:
        # 週報：上週六 01:00 ～ 今天（週六）01:00，共 7 天
        end_of_period = today_01
        start_of_period = today_01 - datetime.timedelta(days=7)
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

    # 4. Analyze
    model_id = MODEL_MAP[args.model]
    print(f"使用模型 {model_id} 進行分析...")
    if args.model == "gemini":
        analysis_result = analyze_with_gemini(formatted_content, is_weekly=is_saturday)
    else:
        analysis_result = analyze_with_claude(formatted_content, is_weekly=is_saturday, model=model_id)
    print(f"分析完成，結果長度：{len(analysis_result)} 字")

    # 5. Save analysis to database
    save_analysis(conn, analysis_type, model_id, analysis_result, start_ts, end_ts)
    print(f"分析結果已儲存至資料庫（模型：{model_id}，類型：{analysis_type}）")

    # 6. Send back to Telegram
    send_message(analysis_result)

    conn.close()

if __name__ == "__main__":
    main()
