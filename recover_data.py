import sqlite3
import os
import re
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

# --- ⚙️ Configuration ---
DB_FILE = "data/tasks.db"
DB_ENC_KEY = os.getenv("DB_ENCRYPTION_KEY")
db_enc_key_clean = DB_ENC_KEY.strip("'\" ") if DB_ENC_KEY else None
cipher_suite = Fernet(db_enc_key_clean.encode()) if db_enc_key_clean else None

def encrypt_str(plain_text):
    if not cipher_suite or not plain_text: return plain_text
    return cipher_suite.encrypt(str(plain_text).encode()).decode()

backup_text = """
🏠 家庭管理系统 - 完整全量备份报告
生成时间: 2026-03-16 10:41:10
==================================================

【 📝 家庭事项清单 】
--- 🔴 未完成事项 ---
[!] 夸恩雅几次，她需要爸爸的肯定，来提高自信心和安全感 (截止: 2026-03-12 00:00)
[!] 放下手机，看着恩雅，听她说话，重复她的关键内容，说出她的感受，不急着教育和纠正 (截止: 2026-03-12 00:00)

--- ⚡ 今日急需处理 ---
[ ] 开发爸爸的健身模块 (截止: 2026-03-16 10:00)
[ ] 改程序确保自动备份和手动备份的数据格式还有数据内容一致 (截止: 2026-03-16 11:00)
[ ] 跟田牧师确定晚上跟他喝茶的地点 (截止: 2026-03-16 15:00)
[ ] 询问艳欣姐姐体检咋样？ (截止: 2026-03-16 16:00)

--- 🌙 明日事项 ---
[ ] 去政府网站注册coco绝育 (截止: 2026-03-17 09:00)
[ ] 安排西安旅游，和恩雅在西安拍艺术照 (截止: 2026-03-17 11:00)
[ ] 打电话询问学习骑马 (截止: 2026-03-17 11:00)
[ ] 查找恩雅的表演兴趣班 (截止: 2026-03-17 15:00)
[ ] 安排带恩雅学校假期出去玩 (截止: 2026-03-17 15:00)
[ ] 提醒恩雅打鼓补课 (截止: 2026-03-17 18:00)
[ ] 询问恩雅月经是否结束，如果结束就在家庭管理系统备案 (截止: 2026-03-17 19:00)

--- 🗓️ 本周剩余事项 ---
[ ] 告诉恩雅妈妈5月底之后就不可以周末来我们家了，因为：妈妈说月经的事情，很不尊重人，让爸爸很难受， (截止: 2026-03-18 21:00)
[ ] 安排带恩雅出去玩 (截止: 2026-03-18 23:59)
[ ] 带恩雅出去玩 (截止: 2026-03-18 23:59)
[ ] 小组长来家 (截止: 2026-03-19 19:00)
[ ] 送恩雅去心理咨询 (截止: 2026-03-20 12:00)
[ ] 告诉恩雅Mue Mue这周日休息 (截止: 2026-03-20 17:00)
[ ] 打羽毛球，工商小学，(Level 4 Court 4)，Coach Roydon (截止: 2026-03-22 15:00)

--- ⏳ 本月剩余事项 ---
[ ] 检查并签名活动文件夹、作文文件夹及补充练习，提醒孩子将资料带回学校 (截止: 2026-03-24 08:00)

--- 🔄 循环事项 ---
[∞] 开始准备恩雅的生日礼物、派对（恩雅希望11岁生日派对） (模式: Yearly)
[∞] 定瑜伽课程 (模式: Wednesday)
[∞] 送恩雅去学跆拳道 (模式: Saturday)
[∞] 给Mue Mue转薪水600新币 (模式: Monthly-LastDay)
[∞] 给张洁转生活费2000新币 (模式: Monthly)
[∞] 开始考虑准备王靖涵的生日礼物 (模式: Yearly-06-01)

--- ✅ 已完成事项 (最近50条) ---
无

【 📏 恩雅的身高体重记录 】
尚无记录。

【 📅 恩雅的经期记录 】
2026-03-12: 月经开始


==================================================
备份结束
"""

def recover():
    # Ensure data dir exists
    if not os.path.exists("data"):
        os.makedirs("data")
    
    # Wipe old DB for a clean restore
    if os.path.exists(DB_FILE):
        os.rename(DB_FILE, DB_FILE + ".bak")
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Create tables
    c.execute('''CREATE TABLE IF NOT EXISTS tasks
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task TEXT NOT NULL,
                  completed BOOLEAN NOT NULL DEFAULT 0,
                  due_date TEXT,
                  recurring_pattern TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS enya_vitals
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  record_date TEXT NOT NULL,
                  height TEXT,
                  weight TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS enya_period
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  record_date TEXT NOT NULL,
                  event_type TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS recurring_completions
                 (task_id INTEGER,
                  completed_date TEXT,
                  UNIQUE(task_id, completed_date))''')

    # Parse Tasks
    # Regex for standard tasks: [ ] or [!]
    task_pattern = re.compile(r"^\[[! ]\] (.*?) \(截止: (.*?)\)$")
    # Regex for recurring tasks: [∞]
    recur_pattern_re = re.compile(r"^\[∞\] (.*?) \(模式: (.*?)\)$")
    # Regex for period: YYYY-MM-DD: Event
    period_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2}): (.*)$")
    # Regex for vitals: YYYY-MM-DD: 身高 XXcm | 体重 XXkg
    vital_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2}): 身高 (.*?)cm \| 体重 (.*?)kg$")

    for line in backup_text.split('\n'):
        line = line.strip()
        if not line: continue
        
        # 1. Normal Tasks
        match = task_pattern.match(line)
        if match:
            text, due = match.groups()
            c.execute("INSERT INTO tasks (task, due_date, recurring_pattern, completed) VALUES (?, ?, ?, ?)",
                      (encrypt_str(text), due, None, 0))
            print(f"Restored Task: {text}")
            continue
            
        # 2. Recurring Tasks
        match = recur_pattern_re.match(line)
        if match:
            text, mode = match.groups()
            c.execute("INSERT INTO tasks (task, due_date, recurring_pattern, completed) VALUES (?, ?, ?, ?)",
                      (encrypt_str(text), None, mode, 0))
            print(f"Restored Recurring: {text}")
            continue
            
        # 3. Period
        match = period_pattern.match(line)
        if match:
            date, event = match.groups()
            # If it's a vital, skip it here (it will be caught by vital_pattern)
            if "身高" in event:
                continue
            c.execute("INSERT INTO enya_period (record_date, event_type) VALUES (?, ?)",
                      (date, encrypt_str(event)))
            print(f"Restored Period: {date} {event}")
            continue
            
        # 4. Vitals
        match = vital_pattern.match(line)
        if match:
            date, h, w = match.groups()
            c.execute("INSERT INTO enya_vitals (record_date, height, weight) VALUES (?, ?, ?)",
                      (date, encrypt_str(h), encrypt_str(w)))
            print(f"Restored Vital: {date}")
            continue

    conn.commit()
    conn.close()
    print("\n✅ Data recovery to 'data/tasks.db' completed successfully.")

if __name__ == "__main__":
    recover()
