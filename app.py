import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import pytz
import os
from openai import OpenAI
from dotenv import load_dotenv
import extra_streamlit_components as stx
import streamlit.components.v1 as components
import json
import requests
import time
import threading
import hashlib
from cryptography.fernet import Fernet

VERSION = "5.5"
ADMIN_EMAIL = "xuchunli@gmail.com"

def hash_password(password):
    """使用 SHA256 为 6 位密码加盐哈希，提高银行级安全性"""
    salt = "family_mgmt_salt_2026"
    return hashlib.sha256((password + salt).encode()).hexdigest()

def verify_password(password, hashed):
    """校验输入的密码与数据库中的哈希是否匹配"""
    return hash_password(password) == hashed

# --- 1. Streamlit UI Config (Must be FIRST) ---
st.set_page_config(
    page_title="家庭管理系统",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="auto"
)

# Cookie 管理器初始化 (放置在顶部以尽早启动加载)
cookie_manager = stx.CookieManager(key="family_auth_mgr_v2")

# --- 3. Environment & Global Config ---
load_dotenv()
try:
    api_key = st.secrets["OPENAI_API_KEY"] if "OPENAI_API_KEY" in st.secrets else os.getenv("OPENAI_API_KEY")
    app_pwd = st.secrets["APP_PASSWORD"] if "APP_PASSWORD" in st.secrets else os.getenv("APP_PASSWORD")
    g_script_url = st.secrets["GOOGLE_BACKUP_URL"] if "GOOGLE_BACKUP_URL" in st.secrets else os.getenv("GOOGLE_BACKUP_URL")
    g_client_id = st.secrets["GOOGLE_CLIENT_ID"] if "GOOGLE_CLIENT_ID" in st.secrets else os.getenv("GOOGLE_CLIENT_ID")
    g_client_secret = st.secrets["GOOGLE_CLIENT_SECRET"] if "GOOGLE_CLIENT_SECRET" in st.secrets else os.getenv("GOOGLE_CLIENT_SECRET")
    db_enc_key = st.secrets["DB_ENCRYPTION_KEY"] if "DB_ENCRYPTION_KEY" in st.secrets else os.getenv("DB_ENCRYPTION_KEY")
except Exception:
    api_key = os.getenv("OPENAI_API_KEY")
    app_pwd = os.getenv("APP_PASSWORD")
    g_script_url = os.getenv("GOOGLE_BACKUP_URL")
    g_client_id = os.getenv("GOOGLE_CLIENT_ID")
    g_client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    db_enc_key = os.getenv("DB_ENCRYPTION_KEY")

# --- 🔐 Encryption Logic ---
cipher_suite = Fernet(db_enc_key.encode()) if db_enc_key else None

def encrypt_str(plain_text):
    if not cipher_suite or not plain_text: return plain_text
    try:
        if isinstance(plain_text, (int, float)): plain_text = str(plain_text)
        return cipher_suite.encrypt(plain_text.encode()).decode()
    except: return plain_text

def decrypt_str(cipher_text):
    if not cipher_suite or not cipher_text: return cipher_text
    try:
        return cipher_suite.decrypt(cipher_text.encode()).decode()
    except:
        # If decryption fails, it might be plain text (for migration/fallback)
        return cipher_text

client = OpenAI(api_key=api_key) if api_key else None
SGT = pytz.timezone('Asia/Singapore')

def get_now_sgt():
    return datetime.now(SGT)

if not os.path.exists("data"):
    os.makedirs("data")
DB_FILE = "data/tasks.db"

# --- 4. Google Drive Backup Engine (via Apps Script Bridge) ---
def backup_to_gdrive(content_str, filename):
    # 清理 URL (防止 .env 里的引号或空格干扰)
    url = g_script_url.strip("'\" ") if g_script_url else None
    
    if not url:
        return False, "⚠️ 未检测到 Google 备份 URL。"
    
    try:
        payload = {
            "filename": filename,
            "content": content_str
        }
        # Google Script 会进行 302 重定向，requests 默认会自动跟随
        response = requests.post(url, json=payload, timeout=30, allow_redirects=True)
        
        if response.status_code == 200:
            if "Success" in response.text:
                return True, "✅ 云端备份成功！"
            else:
                return False, f"❌ 脚本返回错误: {response.text[:200]}"
        elif response.status_code == 404:
            return False, "❌ 备选失败: 404 (脚本 URL 无效或未发布)。请检查 URL 是否完全正确。"
        else:
            return False, f"❌ 备份失败: HTTP {response.status_code}"
    except Exception as e:
        return False, f"❌ 网络请求错误: {str(e)}"

# --- 4. Database Functions ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # 1. 任务表
    c.execute("PRAGMA table_info(tasks)")
    columns = [col[1] for col in c.fetchall()]
    if not columns:
        c.execute('''CREATE TABLE tasks
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      task TEXT NOT NULL,
                      completed BOOLEAN NOT NULL DEFAULT 0,
                      due_date TEXT,
                      recurring_pattern TEXT,
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    else:
        if 'due_date' not in columns:
            c.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT")
        if 'recurring_pattern' not in columns:
            c.execute("ALTER TABLE tasks ADD COLUMN recurring_pattern TEXT")
    
    # 2. 周期性任务完成记录表
    c.execute('''CREATE TABLE IF NOT EXISTS recurring_completions
                 (task_id INTEGER,
                  completed_date TEXT,
                  PRIMARY KEY (task_id, completed_date))''')

    # 3. 身高体重记录表
    c.execute('''CREATE TABLE IF NOT EXISTS enya_vitals
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  record_date TEXT NOT NULL,
                  height TEXT,
                  weight TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
                  
    # 4. 经期记录表
    c.execute('''CREATE TABLE IF NOT EXISTS enya_period
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  record_date TEXT NOT NULL,
                  event_type TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # 5. 系统设置表
    c.execute('''CREATE TABLE IF NOT EXISTS system_config
                 (key TEXT PRIMARY KEY,
                  val TEXT)''')
    
    # 6. 初始化密码
    c.execute("SELECT val FROM system_config WHERE key = 'app_password'")
    if not c.fetchone() and app_pwd:
        hashed_init = hash_password(str(app_pwd))
        c.execute("INSERT INTO system_config (key, val) VALUES ('app_password', ?)", (hashed_init,))

    # --- 🛡️ 数据落盘加密迁移 (Version 5.0 Migration) ---
    # 检查是否已经迁移过
    c.execute("SELECT val FROM system_config WHERE key = 'db_encrypted_v5'")
    if not c.fetchone():
        # 迁移 tasks 表
        c.execute("SELECT id, task FROM tasks")
        for tid, t_text in c.fetchall():
            if t_text and not t_text.startswith('gAAAAA'): # Fernet tokens start with gAAAAA
                enc = encrypt_str(t_text)
                c.execute("UPDATE tasks SET task = ? WHERE id = ?", (enc, tid))
        
        # 迁移 enya_vitals 表
        c.execute("SELECT id, height, weight FROM enya_vitals")
        for vid, h, w in c.fetchall():
            # Height/Weight might be stored as REAL/FLOAT in old db, but we need TEXT for encryption
            enc_h = encrypt_str(str(h)) if h else None
            enc_w = encrypt_str(str(w)) if w else None
            c.execute("UPDATE enya_vitals SET height = ?, weight = ? WHERE id = ?", (enc_h, enc_w, vid))
            
        # 迁移 enya_period 表
        c.execute("SELECT id, event_type FROM enya_period")
        for pid, et in c.fetchall():
            if et and not et.startswith('gAAAAA'):
                enc_et = encrypt_str(et)
                c.execute("UPDATE enya_period SET event_type = ? WHERE id = ?", (enc_et, pid))
        
        c.execute("INSERT OR REPLACE INTO system_config (key, val) VALUES ('db_encrypted_v5', 'true')")
        conn.commit()

    conn.commit()
    conn.close()

def get_app_password():
    """从数据库获取当前 6 位访问密码"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT val FROM system_config WHERE key = 'app_password'")
            res = c.fetchone()
            if res:
                return res[0]
    except:
        pass
    return app_pwd # 备选方案：返回环境变量里的值

def update_app_password(new_pwd):
    """更新数据库中的密码（以哈希形式存储，杜绝明文）"""
    try:
        hashed = hash_password(str(new_pwd))
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO system_config (key, val) VALUES ('app_password', ?)", (hashed,))
            conn.commit()
            return True
    except:
        return False

def extract_date_llm(task_text, fallback_date=None, fallback_recur=None):
    if not client: return task_text, fallback_date, fallback_recur
    now = get_now_sgt()
    f_date = fallback_date if fallback_date else now.strftime("%Y-%m-%d 23:59")
    f_recur = fallback_recur if fallback_recur else "None"
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"""你是家庭AI助手。今天是 {now.strftime('%Y-%m-%d')} ({now.strftime('%A')})。
                你的职能：解析时间意图，并清理任务描述。
                
                ⚠️ 极其重要严格指令：
                1. CLEAN_TASK: 请从原始文本中**彻底移除**所有时间词汇（例如：“今晚6点”、“明天”、“下周二”、“后天中午”等）。
                2. 严禁改动：除了删除时间词，绝对不允许修改、简化、润色、总结或翻译用户的任何其他文字。用户输入的长难句必须高保全。
                3. DATE: 截止日期时间格式 'YYYY-MM-DD HH:MM'。若文本中未提到新的时间意图，请务必返回原始值：{f_date}。
                4. RECUR: 循环模式(Monday, Everyday, Weekend, Monthly-15等)或 None。若文本中未提到新的循环意图，请务必返回原始值：{f_recur}。
                
                示例输入：“今晚6点去超市买菜，明天记得带伞”
                期望输出：CLEAN_TASK: 去超市买菜，记得带伞 | DATE: {now.strftime('%Y-%m-%d')} 18:00 | RECUR: None
                
                请按照以下格式返回：CLEAN_TASK: 内容 | DATE: YYYY-MM-DD HH:MM | RECUR: Pattern"""},
                {"role": "user", "content": task_text}
            ],
            temperature=0
        )
        res = response.choices[0].message.content.strip()
        
        # 解析返回结果
        parts = {}
        for p in res.split('|'):
            if ':' in p:
                k, v = p.split(':', 1)
                parts[k.strip()] = v.strip()
        
        c_task = parts.get("CLEAN_TASK", task_text)
        dt_str = parts.get("DATE", f_date)
        recur_str = parts.get("RECUR", f_recur)
        
        # 验证日期格式
        try:
            datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except:
            dt_str = f_date
            
        return c_task, dt_str, (None if recur_str == "None" else recur_str)
    except Exception as e:
        print(f"LLM 解析错误: {e}")
        return task_text, f_date, (None if f_recur == "None" else f_recur)

def get_tasks():
    conn = sqlite3.connect(DB_FILE)
    query = "SELECT * FROM tasks ORDER BY completed ASC, CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END, due_date ASC, created_at ASC"
    df = pd.read_sql_query(query, conn)
    conn.close()
    # 解密任务内容
    if not df.empty:
        df['task'] = df['task'].apply(decrypt_str)
    return df

def add_task(task_text):
    try:
        clean_task, due_datetime, recur_pattern = extract_date_llm(task_text)
        # 加密内容
        enc_task = encrypt_str(clean_task)
        now_str = get_now_sgt().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO tasks (task, due_date, recurring_pattern, created_at) VALUES (?, ?, ?, ?)", 
                  (enc_task, due_datetime, recur_pattern, now_str))
        task_id = c.lastrowid
        conn.commit()
        
        # Verify insertion
        c.execute("SELECT task, due_date FROM tasks WHERE id = ?", (task_id,))
        row = c.fetchone()
        conn.close()
        
        if row:
            return {"success": True, "task": decrypt_str(row[0]), "due": row[1], "recur": recur_pattern}
        else:
            return {"success": False, "error": "数据库验证插入失败。"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def update_task_status(task_id, completed):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE tasks SET completed = ? WHERE id = ?", (completed, task_id))
    conn.commit()
    conn.close()

def delete_task(task_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

def update_task_text(task_id, new_text):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Fetch current timing to use as fallback
    c.execute("SELECT due_date, recurring_pattern FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    f_date, f_recur = (row[0], row[1]) if row else (None, None)
    
    # AI re-evaluation - but only for date/recur
    clean_text, due_datetime, recur_pattern = extract_date_llm(new_text, f_date, f_recur)
    
    # 加密新内容
    enc_text = encrypt_str(clean_text)
    
    c.execute("UPDATE tasks SET task = ?, due_date = ?, recurring_pattern = ? WHERE id = ?", 
              (enc_text, due_datetime, recur_pattern, task_id))
    conn.commit()
    conn.close()

def mark_recurring_date_completed(task_id, date_str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO recurring_completions (task_id, completed_date) VALUES (?, ?)", (task_id, date_str))
    conn.commit()
    conn.close()

def unmark_recurring_date_completed(task_id, date_str):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM recurring_completions WHERE task_id = ? AND completed_date = ?", (task_id, date_str))
        conn.commit()

def get_recurring_completions():
    try:
        conn = sqlite3.connect(DB_FILE)
        df = pd.read_sql_query("SELECT * FROM recurring_completions", conn)
        conn.close()
        return df
    except:
        return pd.DataFrame(columns=['task_id', 'completed_date'])

# --- 恩雅的健康相关函数 ---
def get_enya_vitals():
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM enya_vitals ORDER BY record_date DESC", conn)
    conn.close()
    if not df.empty:
        df['height'] = df['height'].apply(decrypt_str)
        df['weight'] = df['weight'].apply(decrypt_str)
    return df

def add_enya_vital(date_str, height, weight):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 加密健康数据
    enc_h = encrypt_str(str(height))
    enc_w = encrypt_str(str(weight))
    c.execute("INSERT INTO enya_vitals (record_date, height, weight) VALUES (?, ?, ?)", (date_str, enc_h, enc_w))
    conn.commit()
    conn.close()

def delete_enya_vital(vital_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM enya_vitals WHERE id = ?", (vital_id,))
    conn.commit()
    conn.close()

def get_enya_periods():
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM enya_period ORDER BY record_date DESC", conn)
    conn.close()
    if not df.empty:
        df['event_type'] = df['event_type'].apply(decrypt_str)
    return df

def add_enya_period(date_str, event_type):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 加密
    enc_et = encrypt_str(event_type)
    c.execute("INSERT INTO enya_period (record_date, event_type) VALUES (?, ?)", (date_str, enc_et))
    conn.commit()
    conn.close()

def delete_enya_period(period_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM enya_period WHERE id = ?", (period_id,))
    conn.commit()
    conn.close()

# --- 5. Integrated Master Report & Auto-Backup Logic ---
def generate_master_report():
    """聚合所有模块数据生成完整报告"""
    now_sgt = get_now_sgt()
    full_lines = [
        f"🏠 家庭管理系统 - 完整全量备份报告\n",
        f"生成时间: {now_sgt.strftime('%Y-%m-%d %H:%M:%S')}\n",
        f"{'='*50}\n\n"
    ]
    
    # 1. 任务数据 (高详分类备份)
    full_lines.append("【 📝 家庭事项清单 】\n")
    try:
        from datetime import timedelta
        df = get_tasks()
        if not df.empty:
            now_dt = get_now_sgt()
            today_date = now_dt.date()
            tomorrow_date = today_date + timedelta(days=1)
            end_of_week = today_date + timedelta(days=6 - today_date.weekday())
            next_month = today_date.replace(day=28) + timedelta(days=4)
            end_of_month = next_month - timedelta(days=next_month.day)

            # 提取所有未完成且非循环的任务
            pending_all = df[(df['completed'] == 0) & ((df['recurring_pattern'].isna()) | (df['recurring_pattern'] == ""))]
            
            # 分类逻辑
            overdue, today, tomorrow, week, later = [], [], [], [], []
            for _, r in pending_all.iterrows():
                if not r['due_date']:
                    today.append(r)
                    continue
                try:
                    due_dt = datetime.strptime(r['due_date'], "%Y-%m-%d %H:%M").date()
                    if due_dt < today_date: overdue.append(r)
                    elif due_dt == today_date: today.append(r)
                    elif due_dt == tomorrow_date: tomorrow.append(r)
                    elif due_dt <= end_of_week: week.append(r)
                    elif due_dt <= end_of_month: later.append(r)
                    else: later.append(r) # 超过一月的也暂时放入
                except:
                    today.append(r)

            def add_sub_section(title, items, icon="[ ]"):
                full_lines.append(f"--- {title} ---\n")
                if items:
                    for r in items:
                        due_str = f" (截止: {r['due_date'][:16]})" if r['due_date'] else ""
                        full_lines.append(f"{icon} {r['task']}{due_str}\n")
                else:
                    full_lines.append("无\n")
                full_lines.append("\n")

            add_sub_section("🔴 未完成事项", overdue, "[!]")
            add_sub_section("⚡ 今日急需处理", today, "[ ]")
            add_sub_section("🌙 明日事项", tomorrow, "[ ]")
            add_sub_section("🗓️ 本周剩余事项", week, "[ ]")
            add_sub_section("⏳ 本月剩余事项", later, "[ ]")

            # 循环事项
            recurring = df[(df['completed'] == 0) & (df['recurring_pattern'].notna()) & (df['recurring_pattern'] != "")]
            full_lines.append("--- 🔄 循环事项 ---\n")
            if not recurring.empty:
                for _, r in recurring.iterrows():
                    full_lines.append(f"[∞] {r['task']} (模式: {r['recurring_pattern']})\n")
            else:
                full_lines.append("无\n")
            full_lines.append("\n")

            # 已完成事项
            done = df[df['completed'] == 1]
            full_lines.append("--- ✅ 已完成事项 (最近50条) ---\n")
            if not done.empty:
                done_sorted = done.sort_values(by='created_at', ascending=False).head(50)
                for _, r in done_sorted.iterrows():
                    full_lines.append(f"[√] {r['task']}\n")
            else:
                full_lines.append("无\n")
        else:
            full_lines.append("尚无任务数据。\n")
    except Exception as e:
        full_lines.append(f"任务分类备份失败: {e}\n")
    
    # 2. 恩雅的健康 - 身高体重
    full_lines.append("\n【 📏 恩雅的身高体重记录 】\n")
    try:
        v_df = get_enya_vitals()
        if not v_df.empty:
            for _, r in v_df.iterrows():
                full_lines.append(f"{r['record_date']}: 身高 {r['height']}cm | 体重 {r['weight']}kg\n")
        else:
            full_lines.append("尚无记录。\n")
    except Exception as e:
        full_lines.append(f"健康数据提取失败: {e}\n")

    # 3. 恩雅的健康 - 经期
    full_lines.append("\n【 📅 恩雅的经期记录 】\n")
    try:
        p_df = get_enya_periods()
        if not p_df.empty:
            for _, r in p_df.iterrows():
                full_lines.append(f"{r['record_date']}: {r['event_type']}\n")
        else:
            full_lines.append("尚无记录。\n")
    except Exception as e:
        full_lines.append(f"经期记录提取失败: {e}\n")

    full_lines.append(f"\n\n{'='*50}\n备份结束")
    return "".join(full_lines)

def run_auto_backup_logic(silent=True):
    """
    检查是否需要自动备份 (中午 12 点和凌晨 1 点)
    现在支持：Lazy Trigger (用户访问) 和 Background Daemon (自动执行)
    """
    try:
        now = get_now_sgt()
        current_date = now.strftime("%Y-%m-%d")
        current_hour = now.hour
        
        target_slot = None
        if current_hour == 1:
            target_slot = "01am"
        elif current_hour == 12:
            target_slot = "12pm"
        
        if target_slot:
            slot_key = f"last_auto_backup_{target_slot}"
            
            # 独立连接数据库，确保线程安全
            with sqlite3.connect(DB_FILE) as conn:
                c = conn.cursor()
                c.execute("CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, val TEXT)")
                c.execute("SELECT val FROM system_config WHERE key = ?", (slot_key,))
                res = c.fetchone()
                last_date = res[0] if res else ""
                
                if last_date != current_date:
                    content = generate_master_report()
                    timestamp = now.strftime("%Y%m%d_%H%M")
                    filename = f"Family_Backup_{timestamp}.txt"
                    success, msg = backup_to_gdrive(content, filename)
                    
                    if success:
                        c.execute("INSERT OR REPLACE INTO system_config (key, val) VALUES (?, ?)", (slot_key, current_date))
                        conn.commit()
                        if not silent:
                            st.session_state[f"auto_backup_msg_{target_slot}"] = f"系统已自动完成 {target_slot} 云端同步。"
    except Exception as e:
        if not silent:
            print(f"自动备份后台错误: {e}")

def autonomous_backup_daemon():
    """后台永驻守护线程：每 30 秒巡检一次时间"""
    # 稍微延迟启动，等待主进程稳定
    time.sleep(10)
    while True:
        try:
            now = get_now_sgt()
            # 只有在整点的分钟内才尝试触发
            if (now.hour == 1 or now.hour == 12) and now.minute == 0:
                run_auto_backup_logic(silent=True)
                time.sleep(61) # 跨过这一分钟
            else:
                time.sleep(30)
        except:
            time.sleep(60)

# --- 🎯 线程启动器 (确保全域唯一) ---
import threading
if "daemon_started" not in st.session_state:
    # 在有些环境下 session_state 会重置，我们通过 Python 全局变量做二次锁定
    if not any(t.name == "FamilyBackupDaemon" for t in threading.enumerate()):
        daemon = threading.Thread(target=autonomous_backup_daemon, name="FamilyBackupDaemon", daemon=True)
        daemon.start()
        st.session_state["daemon_started"] = True


# --- 5. UI Styling ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;700&display=swap');
    .stApp { background: linear-gradient(135deg, #fdfbfb 0%, #ebedee 100%); font-family: 'Outfit', sans-serif; }
    .main-header { 
        color: #1e3a8a; 
        font-size: 2rem !important; 
        font-weight: 700; 
        padding: 1rem 0; 
        text-align: center;
    }
    .section-header { 
        color: #1e3a8a; font-weight: 700; padding: 1.5rem 0 0.5rem 0; font-size: 1.2rem;
        border-bottom: 2px solid #e5e7eb; margin-bottom: 0.5rem;
    }
    .task-container { background: white; padding: 0.8rem 1rem; border-bottom: 1px solid #eee; transition: background 0.2s; }
    .task-container:hover { background-color: #f9fafb; }
    .todo-text { font-size: 1.1rem !important; color: #1f2937; margin: 0; }
    .todo-date { font-size: 0.85rem; color: #6366f1; font-weight: 600; margin-top: 4px; }
    .todo-completed { text-decoration: line-through; opacity: 0.4; }
    .recur-tag {
        background: #e0e7ff; color: #4338ca; font-size: 0.75rem; padding: 2px 8px;
        border-radius: 12px; font-weight: 600; margin-left: 8px;
    }
    .overdue-header { 
        color: #ef4444 !important; font-weight: 700; padding: 1.5rem 0 0.5rem 0; font-size: 1.2rem;
        border-bottom: 2px solid #fecaca; margin-bottom: 0.5rem;
    }
    .overdue-text { color: #ef4444 !important; }
    .overdue-date { color: #f87171 !important; }
    
    /* First-level (main module) tabs style to match section-header */
    div[data-testid="stTabs"] button[data-baseweb="tab"] p {
        font-size: 1.2rem !important;
        font-weight: 700 !important;
        color: #1e3a8a !important;
    }
    
    /* Second-level (nested) sub-tabs styling reset */
    div[data-testid="stTabs"] div[data-testid="stTabs"] button[data-baseweb="tab"] p {
        font-size: 1rem !important;
        font-weight: 600 !important;
        color: #4b5563 !important;
    }
</style>
""", unsafe_allow_html=True)

# --- 6. Main App Structure ---
try:
    init_db()
    # 执行自动备份逻辑 (Lazy Load + Daemon 状态同步)
    run_auto_backup_logic(silent=False)

    # --- 🔐 登录逻辑与持久化验证 ---
    # 定义全局唯一认证键名
    AUTH_KEY = "family_auth_token"
    
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    # 1. 深度持久化验证 (Native + Session + Fallback + URL)
    native_cookies = st.context.cookies
    q_params = st.query_params
    
    def resolve_token():
        # 获取各种可能的来源
        c_val = native_cookies.get(AUTH_KEY)
        
        c_comp = None
        try: c_comp = cookie_manager.get(AUTH_KEY)
        except: pass
        
        url_token = q_params.get("auth_key")
        
        # 优先级排序：
        # 1. URL 令牌（最直接的握手证明）
        if url_token in ["authenticated", "authenticated_admin"]:
            return url_token
            
        # 2. 浏览器原生持久化 (Header Cookies)
        if c_val in ["authenticated", "authenticated_admin"]:
            return c_val
            
        # 3. 插件持久化 (Component Cookies)
        if c_comp in ["authenticated", "authenticated_admin"]:
            return c_comp

        # 4. 如果以上都没有，且存在注销标记，则彻底判定为未登录
        if c_val == "LOGGED_OUT" or c_comp == "LOGGED_OUT":
            return None
            
        return None

    # 初始化重试状态
    if "auth_retry_count" not in st.session_state:
        st.session_state["auth_retry_count"] = 0

    # 认证逻辑
    if not st.session_state.get("authenticated") and not st.session_state.get("manual_logout"):
        found_token = resolve_token()
        if found_token:
            st.session_state["authenticated"] = True
            st.session_state["is_admin"] = (found_token == "authenticated_admin")
            # 💡 关键：只有在 native cookies 已经同步的情况下才清除 URL 参数
            # 否则刷新会丢失唯一的凭证
            if native_cookies.get(AUTH_KEY) in ["authenticated", "authenticated_admin"]:
                if "auth_key" in st.query_params:
                    st.query_params.clear()
            st.rerun()
        elif st.session_state["auth_retry_count"] < 12: # 增加重试次数以应对慢速加载
            st.session_state["auth_retry_count"] += 1
            with st.container():
                st.markdown(f"<h1 class='main-header' style='margin-top: 100px; opacity:0.5;'>🏠 家庭管理系统 <span style='font-size: 0.8rem;'>v{VERSION}</span></h1>", unsafe_allow_html=True)
                st.markdown("<div style='text-align:center; color:#9ca3af;'>🛡️ 正在安全恢复您的加密会话...</div>", unsafe_allow_html=True)
                time.sleep(0.5)
                st.rerun()

    # 2. 处理登出请求
    if st.session_state.get("logout_requested"):
        # 安全断开逻辑：先清后端，再发指令清前端
        st.session_state["authenticated"] = False
        st.session_state["is_admin"] = False
        st.session_state["logout_requested"] = False
        st.session_state["manual_logout"] = True
        st.query_params.clear() # 关键：物理删除 URL 钥匙

        # 设置注销 Cookie (持久化 1 天)
        cookie_manager.set(AUTH_KEY, "LOGGED_OUT", expires_at=datetime.now() + timedelta(days=1), path="/")
        
        # 执行强力 JS 清理并重定向到干净首页
        components.html(f"""
            <script>
                var c_str = '{AUTH_KEY}=LOGGED_OUT; path=/; max-age=86400; SameSite=None; Secure; Partitioned';
                document.cookie = c_str;
                if(window.parent) window.parent.document.cookie = c_str;
                
                // 强制父级窗口也刷新到不带任何参数的 URL
                var home = window.location.origin + window.location.pathname;
                if(window.parent) window.parent.location.href = home;
                else window.location.href = home;
            </script>
        """, height=0)
        
        time.sleep(0.3)
        st.stop() # 绝对阻断后续任何内容的渲染

    # 3. 渲染登录界面 (仅在仍未通过验证时)
    login_placeholder = st.empty()
    if not st.session_state["authenticated"]:
        with login_placeholder.container():
            st.markdown(f"<h1 class='main-header' style='margin-top: 50px;'>🏠 家庭管理系统 <span style='font-size: 0.8rem; vertical-align: middle; opacity: 0.5;'>v{VERSION}</span></h1>", unsafe_allow_html=True)
            _, col_m, _ = st.columns([1, 2, 1])
            with col_m:
                st.markdown("<br>", unsafe_allow_html=True)
                pwd = st.text_input("请输入 6 位访问密码:", type="password", key="login_pwd")
                current_hashed_pwd = get_app_password()
                
                # 银行级验证：比对哈希
                if pwd and verify_password(pwd, current_hashed_pwd):
                    st.session_state["authenticated"] = True
                    st.session_state["manual_logout"] = False
                    st.session_state["is_admin"] = False
                    # 设置各种持久化
                    st.query_params["auth_key"] = "authenticated"
                    exp_date = datetime.now() + timedelta(days=30)
                    cookie_manager.set(AUTH_KEY, "authenticated", expires_at=exp_date, path="/")
                    
                    # 强力锁定：使用原生 JS 设置 (关键：现代浏览器跨域环境必须包含 Partitioned; 且 SameSite=None; Secure)
                    exp_utc = exp_date.strftime("%a, %d %b %Y %H:%M:%S GMT")
                    components.html(f"""
                        <script>
                            var c_str = '{AUTH_KEY}=authenticated; expires={exp_utc}; path=/; SameSite=None; Secure; Partitioned';
                            document.cookie = c_str;
                            if(window.parent) window.parent.document.cookie = c_str;
                        </script>
                    """, height=0)
                    st.success("✅ 登录成功！")
                    st.rerun()
                elif pwd:
                    # 🛡️ 安全加固：防止暴力破解。人为增加 1.5 秒延迟，让自动化脚本试错成本增加数万倍。
                    time.sleep(1.5)
                    st.error("🚫 密码错误")
                
                # --- 🔑 Google 管理员登录路径 ---
                
                # 简单实现：由于 streamlit 无法直接处理回调，我们直接构造 Google OAuth URL
                # 这里的 redirect_uri 必须和您在控制台填的一模一样
                google_auth_url = (
                    "https://accounts.google.com/o/oauth2/v2/auth?"
                    f"client_id=555528544138-944b5qordf8gcmp9r0l1um4jj2nbcn4e.apps.googleusercontent.com&"
                    "response_type=code&"
                    "scope=openid%20email%20profile&"
                    f"redirect_uri=https://familymanagementsystem-62a6cbu5jurgnvzngezutj.streamlit.app/&"
                    "state=family_admin_reset"
                )
                
                st.link_button("🔑 使用 Gmail 管理员登录", google_auth_url, use_container_width=True)
            
            # --- 🛡️ 捕捉 Google 回调逻辑 ---
            q_params = st.query_params
            if "code" in q_params and q_params.get("state") == "family_admin_reset":
                # 🛡️ 银行级安全加固：真正的 Token 校验与邮箱白名单过滤
                code = q_params.get("code")
                redirect_uri = "https://familymanagementsystem-62a6cbu5jurgnvzngezutj.streamlit.app/"
                
                st.toast("🔍 正在与 Google 交换加密令牌...", icon="🔄")
                
                try:
                    # 1. 交换 Access Token
                    token_url = "https://oauth2.googleapis.com/token"
                    data = {
                        "code": code,
                        "client_id": g_client_id,
                        "client_secret": g_client_secret,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code"
                    }
                    token_res = requests.post(token_url, data=data).json()
                    access_token = token_res.get("access_token")
                    
                    if access_token:
                        # 2. 获取用户信息
                        user_info_res = requests.get("https://www.googleapis.com/oauth2/v2/userinfo", 
                                                   headers={"Authorization": f"Bearer {access_token}"}).json()
                        user_email = user_info_res.get("email")
                        
                        # 3. 银行级白名单检查：只有管理员本人才能进入
                        if user_email == ADMIN_EMAIL:
                            st.session_state["authenticated"] = True
                            st.session_state["is_admin"] = True
                            st.session_state["manual_logout"] = False
                            st.success(f"✅ 管理员 {user_email} 验证通过")
                        else:
                            st.error(f"🚫 访问拒绝：{user_email} 并不在管理员白名单中。")
                            time.sleep(3)
                            st.stop()
                    else:
                        st.error("❌ 令牌交换失败，请重试登录。")
                        st.stop()
                except Exception as e:
                    st.error(f"⚠️ 安全验证出错: {str(e)}")
                    st.stop()
                
                # 设置管理员持久化 Cookie
                exp_date = datetime.now() + timedelta(days=30)
                cookie_manager.set(AUTH_KEY, "authenticated_admin", expires_at=exp_date, path="/")
                
                # 强力锁定：使用原生 JS 设置 (分区 Cookie 锁定)
                exp_utc = exp_date.strftime("%a, %d %b %Y %H:%M:%S GMT")
                components.html(f"""
                    <script>
                        var c_str = '{AUTH_KEY}=authenticated_admin; expires={exp_utc}; path=/; SameSite=None; Secure; Partitioned';
                        document.cookie = c_str;
                        if(window.parent) window.parent.document.cookie = c_str;
                    </script>
                """, height=0)
                
                # 重要：清除地址栏的 code 参数，并锁定认证钥匙
                st.query_params.clear()
                st.query_params["auth_key"] = "authenticated_admin"
                
                time.sleep(0.5)
                st.rerun()
            
            st.stop()
            
    # 一旦认证成功，如果原本显示了登录界面，现在将其清空
    if st.session_state["authenticated"]:
        login_placeholder.empty()
    else:
        st.stop() # 绝对阻断

    if "editing_task_id" not in st.session_state:
        st.session_state["editing_task_id"] = None

    # --- 🛠️ 辅助 UI 函数 ---
    def hits_day(pattern, target_date):
        if not pattern: return False
        p = pattern.strip()
        if p == 'Everyday': return True
        if p == 'Weekend' and target_date.weekday() >= 5: return True
        if p == 'Monthly-LastDay':
            # 如果明天的号数是 1，那就说明今天是本月最后一天
            return (target_date + timedelta(days=1)).day == 1
        if p.startswith('Monthly-'):
            try:
                target_day = int(p.split('-')[1])
                return target_date.day == target_day
            except:
                pass
        return p == target_date.strftime('%A')



    def format_date_with_weekday(dt_str):
        if not dt_str: return ""
        try:
            # First try parsing the standard format
            dt = datetime.strptime(dt_str[:16], "%Y-%m-%d %H:%M")
            weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
            return f"{dt_str[:16]} {weekdays[dt.weekday()]}"
        except:
            return dt_str

    @st.dialog("📋 事项添加结果")
    def show_add_dialog(result):
        if result["success"]:
            st.success("✅ 该事项已成功入库！")
            st.markdown(f"**内容：** {result['task']}")
            if result['due']:
                st.markdown(f"**⏰ 日期/时间：** {format_date_with_weekday(result['due'])}")
            if result['recur']:
                st.markdown(f"**🔄 循环模式：** {result['recur']}")
        else:
            st.error(f"❌ 添加失败：{result['error']}")
        
        if st.button("确定", use_container_width=True):
            st.rerun()

    if "last_add_result" in st.session_state:
        show_add_dialog(st.session_state.pop("last_add_result"))

    def render_task(row, is_shadow=False, location="main", is_overdue=False):
        key_id = f"{location}_c_{row['id']}" if not is_shadow else f"sh_{location}_{row['id']}_{row['due_date'][:10]}"
        if not is_shadow:
            del_id = f"{location}_d_{row['id']}"
            edit_id = f"{location}_e_{row['id']}"
        else:
            # For shadow tasks, we must include the date to ensure unique keys in the archive
            date_slug = row['due_date'][:10]
            del_id = f"sh_{location}_d_{row['id']}_{date_slug}"
            edit_id = f"sh_{location}_e_{row['id']}_{date_slug}"
        
        with st.container():
            st.markdown('<div class="task-container">', unsafe_allow_html=True)
            # Layout: checkbox, content area (text or input), action buttons
            c1, c2, c3 = st.columns([0.05, 0.75, 0.2])
            
            if not is_shadow:
                is_comp = c1.checkbox("", value=row['completed'], key=key_id)
                if is_comp != row['completed']:
                    update_task_status(row['id'], is_comp)
                    st.rerun()
            else:
                # Shadow task checkbox
                is_comp = c1.checkbox("", value=row.get('completed', 0), key=key_id)
                if is_comp != row.get('completed', 0):
                    # For shadow tasks, we use the date from 'due_date' (YYYY-MM-DD HH:MM)
                    date_only = row['due_date'][:10]
                    if is_comp:
                        mark_recurring_date_completed(row['id'], date_only)
                    else:
                        unmark_recurring_date_completed(row['id'], date_only)
                    st.rerun()
                
            # Handle inline edit (Only for non-shadow master tasks)
            if not is_shadow and st.session_state.get("editing_task_id") == row['id']:
                new_text = c2.text_input("修改事项:", value=row['task'], key=f"inp_{location}_{row['id']}")
                save_col, can_col = c3.columns(2)
                if save_col.button("💾", key=f"save_{location}_{row['id']}", help="保存"):
                    update_task_text(row['id'], new_text)
                    st.session_state["editing_task_id"] = None
                    st.rerun()
                if can_col.button("🚫", key=f"can_{location}_{row['id']}", help="取消"):
                    st.session_state["editing_task_id"] = None
                    st.rerun()
            else:
                style = "todo-completed" if row['completed'] else ""
                # Red text for overdue
                overdue_cls = "overdue-text" if is_overdue else ""
                overdue_date_cls = "overdue-date" if is_overdue else ""
                
                recur_tag = f"<span class='recur-tag'>🔄 循环: {row['recurring_pattern']}</span>" if row['recurring_pattern'] else ""
                due_val = f"📅 日期/时间: {format_date_with_weekday(row['due_date'])}" if row['due_date'] else ""
                
                c2.markdown(f"<p class='todo-text {style} {overdue_cls}'>{row['task']}{recur_tag}</p><div class='todo-date {overdue_date_cls}'>{due_val}</div>", unsafe_allow_html=True)
                
                # Action buttons
                edit_col, del_col = c3.columns(2)
                if not is_shadow:
                    if edit_col.button("✏️", key=edit_id, help="修改"):
                        st.session_state["editing_task_id"] = row['id']
                        st.rerun()
                    if del_col.button("🗑️", key=del_id, help="删除"):
                        delete_task(row['id'])
                        st.rerun()
                else:
                    # Shadow tasks in archive can be "Deleted" (removed from completions)
                    if row.get('completed'):
                        if del_col.button("🗑️", key=del_id, help="撤销完成记录"):
                            date_only = row['due_date'][:10]
                            unmark_recurring_date_completed(row['id'], date_only)
                            st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)



    # --- 7. Data Preparation ---
    tasks_df = get_tasks()
    now = get_now_sgt()
    today_date = now.date()
    tomorrow_date = today_date + timedelta(days=1)
    end_of_week = today_date + timedelta(days=6 - today_date.weekday())
    
    # Calculate end of current month
    next_month = today_date.replace(day=28) + timedelta(days=4)
    end_of_month = next_month - timedelta(days=next_month.day)
    
    # Initialize all lists to avoid NameErrors
    recurring_list, overdue_list, today_list, tomorrow_list, week_list, later_list = [], [], [], [], [], []
    shadow_overdue, shadow_today, shadow_tomorrow, shadow_week, shadow_later = [], [], [], [], []
    open_tasks = pd.DataFrame()
    completed_tasks = pd.DataFrame()

    if not tasks_df.empty:
        open_tasks = tasks_df[tasks_df['completed'] == 0]
        completed_tasks = tasks_df[tasks_df['completed'] == 1]
        
        for _, row in open_tasks.iterrows():
            if row['recurring_pattern']:
                recurring_list.append(row)
                continue
            if not row['due_date']:
                today_list.append(row)
                continue
            try:
                due_dt = datetime.strptime(row['due_date'], "%Y-%m-%d %H:%M").date()
                if due_dt < today_date: overdue_list.append(row)
                elif due_dt == today_date: today_list.append(row)
                elif due_dt == tomorrow_date: tomorrow_list.append(row)
                elif due_dt <= end_of_week: week_list.append(row)
                else: later_list.append(row)
            except: today_list.append(row)

        for item in recurring_list:
            # Check last 7 days for missed recurring items
            past_ptr = today_date - timedelta(days=7)
            while past_ptr < today_date:
                if hits_day(item['recurring_pattern'], past_ptr):
                    shadow_overdue.append((item, past_ptr))
                past_ptr += timedelta(days=1)

            if hits_day(item['recurring_pattern'], today_date): shadow_today.append(item)
            if hits_day(item['recurring_pattern'], tomorrow_date): shadow_tomorrow.append(item)
            
            curr = tomorrow_date + timedelta(days=1)
            while curr <= end_of_week:
                if hits_day(item['recurring_pattern'], curr): shadow_week.append((item, curr))
                curr += timedelta(days=1)
                
            # Scan beyond the current week up to the end of the month
            curr_later = end_of_week + timedelta(days=1)
            # Make sure we don't go backwards if end_of_week is already in the next month
            if curr_later <= end_of_month:
                while curr_later <= end_of_month:
                    if hits_day(item['recurring_pattern'], curr_later): shadow_later.append((item, curr_later))
                    curr_later += timedelta(days=1)

    # --- 8. Combine and Sort ---
    # Fetch recurring completions
    recur_comps = get_recurring_completions()
    
    def prepare_sorted_list(normal_items, shadow_items_with_dates=None, shadow_items_plain=None, default_date=None):
        combined = []
        for r in normal_items:
            temp = r.copy()
            temp['_is_shadow'] = False
            combined.append(temp)
        
        # Helper to check if a specific task/date is completed
        def is_done(tid, d_str):
            if recur_comps.empty: return False
            return not recur_comps[(recur_comps['task_id'] == tid) & (recur_comps['completed_date'] == d_str[:10])].empty

        if shadow_items_plain and default_date:
            d_str = default_date.strftime("%Y-%m-%d")
            for r in shadow_items_plain:
                temp = r.copy()
                temp['_is_shadow'] = True
                temp['due_date'] = f"{d_str} 12:00"
                temp['completed'] = 1 if is_done(r['id'], d_str) else 0
                combined.append(temp)
        
        if shadow_items_with_dates:
            for r, d in shadow_items_with_dates:
                d_str = d.strftime("%Y-%m-%d")
                temp = r.copy()
                temp['_is_shadow'] = True
                temp['due_date'] = f"{d_str} 12:00"
                temp['completed'] = 1 if is_done(r['id'], d_str) else 0
                combined.append(temp)
        
        # Separate open and completed within each list
        open_list = [x for x in combined if not x['completed']]
        done_list = [x for x in combined if x['completed']]
        
        # Sort each
        open_list.sort(key=lambda x: x['due_date'] if x['due_date'] else "9999-12-31")
        done_list.sort(key=lambda x: x['due_date'] if x['due_date'] else "9999-12-31")
        
        return open_list, done_list

    final_overdue_open, final_overdue_done = prepare_sorted_list(overdue_list, shadow_items_with_dates=shadow_overdue)
    final_today_open, final_today_done = prepare_sorted_list(today_list, shadow_items_plain=shadow_today, default_date=today_date)
    final_tomorrow_open, final_tomorrow_done = prepare_sorted_list(tomorrow_list, shadow_items_plain=shadow_tomorrow, default_date=tomorrow_date)
    final_week_open, final_week_done = prepare_sorted_list(week_list, shadow_items_with_dates=shadow_week)
    final_later_open, final_later_done = prepare_sorted_list(later_list, shadow_items_with_dates=shadow_later)

    # Compile all completed shadow tasks for the t3 tab
    all_completed_shadows = final_today_done + final_tomorrow_done + final_week_done + final_later_done
    if all_completed_shadows:
        shadows_df = pd.DataFrame(all_completed_shadows)
        # Update display titles for completed shadow tasks to include individual dates
        shadows_df['task'] = shadows_df.apply(lambda x: f"{x['task']} (周期性于 {x['due_date'][:10]})" if x['_is_shadow'] else x['task'], axis=1)
        completed_tasks = pd.concat([completed_tasks, shadows_df], ignore_index=True)
        # Final sort for completed archive
        completed_tasks.sort_values(by='due_date', ascending=False, inplace=True)

    # Main Interface
    # CSS to style the download button in the header
    st.markdown("""
        <style>
        div.stDownloadButton > button {
            font-size: 15px !important;
            font-weight: 500 !important;
            color: #444 !important;
            background-color: #f0f2f6 !important;
            border: 1px solid #dcdde1 !important;
            padding: 6px 12px !important;
            height: 42px !important;
            margin-top: 30px !important; /* Align with H1 baselineish */
            border-radius: 8px !important;
            transition: all 0.2s ease !important;
        }
        div.stDownloadButton > button:hover {
            color: #ef4444 !important;
            border-color: #ef4444 !important;
            background-color: #ffffff !important;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1) !important;
        }
        </style>
    """, unsafe_allow_html=True)

    def handle_logout():
        st.session_state["logout_requested"] = True
        st.session_state["authenticated"] = False
        st.session_state["manual_logout"] = True

    # Header Row
    c_logout, c_title, c_sync = st.columns([0.12, 0.54, 0.34], vertical_alignment="center")
    with c_logout:
        st.button("🔴 退出登录", use_container_width=True, on_click=handle_logout)
    with c_title:
        st.markdown(f"<h1 class='main-header'>🏠 家庭管理系统 <span style='font-size: 0.8rem; vertical-align: middle; opacity: 0.5;'>v{VERSION}</span></h1>", unsafe_allow_html=True)
        # 如果刚才触发了自动备份，给予一个小提示
        for slot in ["01am", "12pm"]:
            msg_key = f"auto_backup_msg_{slot}"
            if msg_key in st.session_state:
                st.toast(st.session_state.pop(msg_key), icon="🤖")
    with c_sync:
        col_admin, col_manual = st.columns([0.5, 0.5])
        with col_admin:
            # 只有通过 Gmail 登录的管理员才能看到盾牌图标
            if st.session_state.get("is_admin"):
                with st.popover("🔐 修改密码", use_container_width=True, help="系统安全设置"):
                    st.markdown("### 🔐 访问管理")
                    curr_p = get_app_password()
                    st.write(f"当前 6 位访问密码: **{curr_p}**")
                    new_p = st.text_input("设置新密码 (6位数字):", type="password", max_chars=6)
                    if st.button("更新密码", use_container_width=True):
                        if len(new_p) == 6 and new_p.isdigit():
                            if update_app_password(new_p):
                                st.success("密码已更新！")
                                time.sleep(1)
                                st.rerun()
                            else:
                                st.error("保存失败")
                        else:
                            st.warning("请输入6位数字")
            else:
                st.empty() # 非管理员不显示
        
        with col_manual:
            if st.button("☁️ 云端同步", use_container_width=True, help="立即备份所有数据到云端", key="manual_sync_header"):
                with st.spinner("同步中..."):
                    content = generate_master_report()
                    timestamp = get_now_sgt().strftime("%Y%m%d_%H%M")
                    success, msg = backup_to_gdrive(content, f"Family_Backup_{timestamp}.txt")
                    if success:
                        st.toast(msg, icon="✅")
                    else:
                        st.error(msg)

    st.markdown('<br>', unsafe_allow_html=True)
    top_tab1, top_tab2, top_tab3, top_tab4 = st.tabs(['📝 家庭事项', '💰 家庭财务', '🏋️‍♂️ 爸爸的健身', '🌸 恩雅的健康'])

    with top_tab1:

            def generate_txt_report():
                lines = ["家庭事项清单\n", "=" * 80 + "\n\n"]

                def add_section(title, task_list):
                    # Ensure we handle DataFrame vs List of Dicts properly
                    if isinstance(task_list, pd.DataFrame):
                        if task_list.empty: return
                        iterable = [row.to_dict() for _, row in task_list.iterrows()]
                    else:
                        if not task_list: return
                        iterable = task_list

                    lines.append(f"【{title}】\n")
                    lines.append(f"{'截止时间':<18} | {'任务内容':<45} | {'循环':<10}\n")
                    lines.append("-" * 80 + "\n")

                    for row in iterable:
                        _due_raw = str(row.get('due_date', ''))
                        due = _due_raw[:16] if pd.notna(row.get('due_date')) and row.get('due_date') else "未设置"
                        task = str(row.get('task', '')).replace('\n', ' ')
                        recur = str(row.get('recurring_pattern', '')) if pd.notna(row.get('recurring_pattern')) and row.get('recurring_pattern') else "无"
                        lines.append(f"{due:<18} | {task:<45} | {recur:<10}\n")
                    lines.append("\n")

                add_section("🔴 未完成事项", final_overdue_open)
                add_section("⚡ 今日急需处理", final_today_open)
                add_section("🌙 明日事项", final_tomorrow_open)
                add_section("🗓️ 本周剩余事项", final_week_open)
                add_section("⏳ 本月剩余事项", final_later_open)
                add_section("🔄 长期循环事项", recurring_list)
                add_section("✅ 已完成事项归档", completed_tasks)

                return "".join(lines) if len(lines) > 2 else "没有任务数据。"

            st.markdown("<br>", unsafe_allow_html=True)

            # Add Task Section & Download
            def handle_add_cb():
                st.session_state["temp_task_text"] = st.session_state.get("input_new_task", "")
                st.session_state["input_new_task"] = ""

            col_add_input, col_add_btn, col_dl_btn = st.columns([0.60, 0.20, 0.20], vertical_alignment="bottom")
            with col_add_input:
                st.text_input("➕ 新增事项:", placeholder="请输入需要添加的新事项，比如这周六下午4点去海滩...", key="input_new_task", label_visibility="collapsed")
            with col_add_btn:
                if st.button("添加新事项", use_container_width=True, on_click=handle_add_cb):
                    pass
            with col_dl_btn:
                if not tasks_df.empty:
                    txt_content = generate_txt_report()
                    st.download_button(
                        label="📥 下载事项清单",
                        data=txt_content,
                        file_name=f"家庭事项清单_{get_now_sgt().strftime('%m%d_%H%M')}.txt",
                        mime="text/plain",
                        key="dl_btn_header_v1",
                        use_container_width=True
                    )

            task_to_add = st.session_state.get("temp_task_text")
            if task_to_add:
                with st.spinner("AI 解析并提交中..."):
                    res = add_task(task_to_add)
                    st.session_state["last_add_result"] = res
                    st.session_state["temp_task_text"] = None
                    st.rerun()

            st.markdown("<br>", unsafe_allow_html=True)

            t1, t2, t3 = st.tabs(["📝 待办事项", "🔄 循环事项", "✅ 已完成事项"], key="task_tabs_main")

            with t1:
                if tasks_df.empty:
                    st.info("目前没有任务。在侧边栏添加一个吧！")
                else:

                    if final_overdue_open:
                        st.markdown('<div class="overdue-header">🔴 未完成事项</div>', unsafe_allow_html=True)
                        for row in final_overdue_open: 
                            # 确定是否是 shadow task
                            is_sh = row.get('_is_shadow', False)
                            render_task(row, is_shadow=is_sh, location="final_overdue", is_overdue=True)

                    # --- Displays Tab 1 ---
                    if final_today_open:
                        st.markdown('<div class="section-header" style="color: #ef4444; border-bottom-color: #fecaca;">⚡ 今日急需处理</div>', unsafe_allow_html=True)
                        for row in final_today_open: render_task(row, is_shadow=row['_is_shadow'], location="final_today")

                    if final_tomorrow_open:
                        st.markdown('<div class="section-header">🌙 明日事项</div>', unsafe_allow_html=True)
                        for row in final_tomorrow_open: render_task(row, is_shadow=row['_is_shadow'], location="final_tomorrow")

                    if final_week_open:
                        st.markdown('<div class="section-header">🗓️ 本周剩余事项</div>', unsafe_allow_html=True)
                        for row in final_week_open: render_task(row, is_shadow=row['_is_shadow'], location="final_week")

                    if final_later_open:
                        st.markdown('<div class="section-header">⏳ 本月剩余事项</div>', unsafe_allow_html=True)
                        for row in final_later_open: render_task(row, is_shadow=row['_is_shadow'], location="final_later")

            with t2:
                st.markdown('<div class="section-header">🔄 长期循环事项</div>', unsafe_allow_html=True)
                if not recurring_list:
                    st.info("目前没有循环事项。您可以添加如“每周二购买零食”来创建。")
                else:
                    for row in recurring_list:
                        render_task(row, location="recur_tab")

            with t3:
                st.markdown('<div class="section-header">✅ 已完成事项归档</div>', unsafe_allow_html=True)
                if completed_tasks.empty:
                    st.info("目前没有已完成的事项。")
                else:
                    # 💡 强制填充 NaN，确保 is_shade 为准确的布尔值
                    if '_is_shadow' in completed_tasks.columns:
                        completed_tasks['_is_shadow'] = completed_tasks['_is_shadow'].fillna(False)
                    
                    for _, row in completed_tasks.iterrows():
                        is_shade = bool(row.get('_is_shadow', False))
                        render_task(row, is_shadow=is_shade, location="comp_tab")

    with top_tab2:
        st.subheader('💵 当前家庭财务一览')
        st.info('内容可以先为空，我后面会继续加入。')
        st.subheader('📈 投资一览表')
        st.info('内容可以先为空，我后面会继续加入。')

    with top_tab3:
        st.subheader('🎯 爸爸的健身目标')
        st.info('内容可以先为空，我后面会继续加入。')
        st.subheader('📅 健身计划')
        st.info('内容可以先为空，我后面会继续加入。')
        st.subheader('✅ 每次健身项目完成记录')
        st.info('内容可以先为空，我后面会继续加入。')

    with top_tab4:
        st.markdown("<h2 style='color: #db2777;'>🌸 恩雅的健康中心</h2>", unsafe_allow_html=True)
        
        health_sub1, health_sub2 = st.tabs(["📏 身高体重记录", "📅 月经记录"])
        
        with health_sub1:
            st.markdown("### 📊 新增记录")
            cols_v = st.columns([0.3, 0.2, 0.2, 0.3], vertical_alignment="bottom")
            with cols_v[0]:
                v_date = st.date_input("日期", value=get_now_sgt().date(), key="v_date_inp")
            with cols_v[1]:
                v_height = st.number_input("身高 (cm)", min_value=0.0, step=0.1, key="v_height_inp")
            with cols_v[2]:
                v_weight = st.number_input("体重 (kg)", min_value=0.0, step=0.1, key="v_weight_inp")
            with cols_v[3]:
                if st.button("➕ 保存记录", use_container_width=True, key="v_save_btn"):
                    add_enya_vital(v_date.strftime("%Y-%m-%d"), v_height, v_weight)
                    st.success("记录已保存！")
                    st.rerun()

            st.markdown("---")
            st.markdown("### 📜 历史记录")
            vitals_df = get_enya_vitals()
            if vitals_df.empty:
                st.info("尚无身高体重记录。")
            else:
                # 使用 DataFrame 显示表格，带删除按钮
                for idx, row in vitals_df.iterrows():
                    v_cols = st.columns([0.25, 0.25, 0.25, 0.25])
                    v_cols[0].write(row['record_date'])
                    v_cols[1].write(f"{row['height']} cm")
                    v_cols[2].write(f"{row['weight']} kg")
                    if v_cols[3].button("🗑️", key=f"del_v_{row['id']}"):
                        delete_enya_vital(row['id'])
                        st.rerun()
                    st.divider()

        with health_sub2:
            st.markdown("### 🩸 新增经期记录")
            cols_p = st.columns([0.3, 0.4, 0.3], vertical_alignment="bottom")
            with cols_p[0]:
                p_date = st.date_input("日期", value=get_now_sgt().date(), key="p_date_inp")
            with cols_p[1]:
                p_type = st.selectbox("事件内容", ["月经开始", "月经结束"], key="p_type_inp")
            with cols_p[2]:
                if st.button("➕ 保存记录", use_container_width=True, key="p_save_btn"):
                    add_enya_period(p_date.strftime("%Y-%m-%d"), p_type)
                    st.success("记录已保存！")
                    st.rerun()

            def generate_period_report(df):
                if df.empty: return "尚无经期记录。"
                lines = ["🌸 恩雅的经期健康记录报告\n", "=" * 40 + "\n\n"]
                lines.append(f"{'日期':<15} | {'事件内容':<20}\n")
                lines.append("-" * 40 + "\n")
                for _, row in df.iterrows():
                    lines.append(f"{row['record_date']:<15} | {row['event_type']:<20}\n")
                lines.append("\n" + "=" * 40 + "\n")
                lines.append(f"导出时间: {get_now_sgt().strftime('%Y-%m-%d %H:%M')}")
                return "".join(lines)

            st.markdown("---")
            periods_df = get_enya_periods()
            p_head_col1, p_head_col2 = st.columns([0.7, 0.3], vertical_alignment="center")
            with p_head_col1:
                st.markdown("### 📜 经期历史记录")
            with p_head_col2:
                if not periods_df.empty:
                    p_txt = generate_period_report(periods_df)
                    st.download_button(
                        label="📥 下载经期记录",
                        data=p_txt,
                        file_name=f"恩雅经期记录_{get_now_sgt().strftime('%m%d')}.txt",
                        mime="text/plain",
                        use_container_width=True,
                        key="dl_period_btn"
                    )

            if periods_df.empty:
                st.info("尚无月经记录。")
            else:
                for idx, row in periods_df.iterrows():
                    p_cols = st.columns([0.3, 0.4, 0.3])
                    p_cols[0].write(row['record_date'])
                    p_color = "#e11d48" if row['event_type'] == "月经开始" else "#059669"
                    p_cols[1].markdown(f"**<span style='color: {p_color};'>{row['event_type']}</span>**", unsafe_allow_html=True)
                    if p_cols[2].button("🗑️", key=f"del_p_{row['id']}"):
                        delete_enya_period(row['id'])
                        st.rerun()
                    st.divider()



    st.markdown("---")
    st.markdown(f"<p style='text-align: center; color: #888;'>最后更新: {get_now_sgt().strftime('%Y-%m-%d %H:%M')}</p>", unsafe_allow_html=True)

except Exception as e:
    st.error(f"❌ 系统发生错误: {e}")
    st.exception(e)
