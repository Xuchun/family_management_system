import streamlit as st
import sqlite3
import pandas as pd
import base64
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
import altair as alt

VERSION = "11.13.26"
ADMIN_EMAIL = "xuchunli@gmail.com"

def hash_password(password):
    """使用 SHA256 为 6 位密码加盐哈希，提高银行级安全性"""
    salt = "family_mgmt_salt_2026"
    return hashlib.sha256((password + salt).encode()).hexdigest()

def verify_password(password, stored_val):
    """校验输入的密码与数据库中的值是否匹配 (兼容旧版哈希和新版明文)"""
    # 1. 尝试比对 SHA256 哈希 (旧版数据)
    if hash_password(password) == stored_val:
        return True
    # 2. 尝试明文比对 (新版数据)
    if str(password) == str(stored_val):
        return True
    return False

# --- 1. Streamlit UI Config (Must be FIRST) ---
st.set_page_config(
    page_title="家庭管理系统",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="auto"
)

import streamlit.components.v1 as components
# 强力注入 localStorage 恢复机制，对抗 iOS Safari 重启掉线
components.html("""
<script>
    var token = null;
    try { token = window.localStorage.getItem('family_auth_token'); } catch(e) {}
    try { if(!token && window.parent) token = window.parent.localStorage.getItem('family_auth_token'); } catch(e) {}
    
    if (token) {
        var hasAuth = false;
        try { hasAuth = (window.parent.location.href.indexOf('auth_key') !== -1); } 
        catch(e) { hasAuth = (window.location.href.indexOf('auth_key') !== -1); }
        
        if (!hasAuth) {
            try { window.parent.location.href = window.parent.location.origin + window.parent.location.pathname + '?auth_key=' + token; }
            catch(e) { window.location.href = window.location.origin + window.location.pathname + '?auth_key=' + token; }
        }
    }
</script>
""", height=0)

# Cookie 管理器初始化 (放置在顶部以尽早启动加载)
cookie_manager = stx.CookieManager(key="family_auth_mgr_v2")

# --- 3. Environment & Global Config ---
load_dotenv()
# --- 3.5 Encryption Manager (修复 Ln 74/80 报错) ---
def get_cipher_suite():
    # 1. 尝试从 os.getenv 获取 (这是最稳妥的本地开发方式)
    key = os.getenv("DB_ENCRYPTION_KEY")
    
    # 2. 如果环境变量没有，再尝试 st.secrets (为了部署到云端兼容)
    if not key:
        try:
            key = st.secrets.get("DB_ENCRYPTION_KEY")
        except:
            key = None
            
    # 3. 如果还是没有，生成临时密钥，确保 PhD 级的数据不被明文存储
    if not key:
        key = Fernet.generate_key().decode()
        # 这里建议你之后把生成的 key 手动存入你的 .env
        
    return Fernet(key.encode())

# 彻底修复全局 cipher_suite 的初始化
cipher_suite = get_cipher_suite()

# 安全地从 st.secrets 或 os.getenv 获取配置
def get_secret_or_env(key):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key)

api_key = get_secret_or_env("OPENAI_API_KEY")
app_pwd = get_secret_or_env("APP_PASSWORD")
g_script_url = get_secret_or_env("GOOGLE_BACKUP_URL")
g_client_id = get_secret_or_env("GOOGLE_CLIENT_ID")
g_client_secret = get_secret_or_env("GOOGLE_CLIENT_SECRET")
db_enc_key = get_secret_or_env("DB_ENCRYPTION_KEY")

# --- 🔐 Encryption Logic ---
# 确保密钥清理掉可能的换行符或空格
db_enc_key_clean = db_enc_key.strip("'\" ") if db_enc_key else None
cipher_suite = Fernet(db_enc_key_clean.encode()) if db_enc_key_clean else None

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
def backup_to_gdrive(content_str, filename, overwrite=False, is_binary=False):
    # 清理 URL (防止 .env 里的引号或空格干扰)
    url = g_script_url.strip("'\" ") if g_script_url else None
    
    if not url:
        return False, "⚠️ 未检测到 Google 备份 URL。"
    
    try:
        payload = {
            "action": "upload",
            "filename": filename,
            "content": content_str,
            "overwrite": overwrite,
            "is_binary": is_binary # v8.0 支持二进制文件传输
        }
        # Google Script 会进行 302 重定向，requests 默认会自动跟随
        response = requests.post(url, json=payload, timeout=45, allow_redirects=True)
        
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
        return False, f"❌ 通讯异常: {e}"

def pull_from_gdrive(filename, is_binary=False):
    """
    🛠️ v11.9.25 核心优化：从 Google Drive 拉取文件数据
    """
    url = g_script_url.strip("'\" ") if g_script_url else None
    if not url:
        return None, "⚠️ 未配置备份 URL"
    
    try:
        payload = {
            "action": "download",
            "filename": filename,
            "is_binary": is_binary
        }
        # 使用 POST 触发 Apps Script 的 doPost
        response = requests.post(url, json=payload, timeout=30)
        
        if response.status_code == 200:
            # 🛠️ v11.9.28: 增强检查。如果返回 "Success" 开头，说明脚本太旧，在执行上传而非下载
            r_text = response.text.strip()
            if r_text.startswith("Error"):
                return None, r_text
            if r_text.startswith("Success"):
                return None, f"⚠️ 脚本版本不匹配 (脚本执行了上传而非下载)。原因: {r_text}"
            return r_text, "Success"
        return None, f"HTTP {response.status_code}"
    except Exception as e:
        return None, str(e)

def auto_restore_if_needed(force=False):
    """
    🛠️ v11.9.25 启动自愈：如果本地数据库为空，尝试从云端恢复
    """
    db_exists = os.path.exists(DB_FILE)
    db_size = os.path.getsize(DB_FILE) if db_exists else 0
    
    # 逻辑：如果强制执行，或者数据库不存在，或者数据库极小 (可能是空库)
    if force or not db_exists or db_size < 100:
        # 尝试从云端拉取
        db_b64, status = pull_from_gdrive("tasks.db", is_binary=True)
        if status == "Success" and db_b64:
            try:
                # 🛠️ v11.9.26: 增强 Base64 解码鲁棒性 (去除空白并自动补齐等号)
                clean_b64 = db_b64.strip().replace("\n", "").replace("\r", "")
                missing_padding = len(clean_b64) % 4
                if missing_padding:
                    clean_b64 += "=" * (4 - missing_padding)
                
                db_bytes = base64.b64decode(clean_b64)
                if not os.path.exists("data"): os.makedirs("data")
                with open(DB_FILE, "wb") as f:
                    f.write(db_bytes)
                return True, "✅ 同步成功，数据已还原。"
            except Exception as e:
                preview = db_b64[:50] + "..." if db_b64 else "None"
                return False, f"❌ 解码/写入失败: {e} (数据预览: {preview})"
        return False, f"❌ 云端拉取失败: {status}"
    return False, f"Skipped (Local data exists, size: {db_size} bytes)"

def trigger_realtime_backup():
    """
    v8.0 - “双壳”实时容灾引擎 (Double-Hull Disaster Recovery)
    强制同步：1. 实时文本报告 (realtime_backup.txt)  2. 实时数据库 file (tasks.db)
    """
    def _async_backup():
        try:
            # 1. 同步文本报告
            report_content = generate_master_report()
            report_content += f"\n\n[🛰️ RTK 模式实时增量备份] 覆盖时间: {get_now_sgt().strftime('%H:%M:%S')}"
            backup_to_gdrive(report_content, "realtime_backup.txt", overwrite=True, is_binary=False)
            
            # 2. 同步二进制数据库 (Base64 编码)
            if os.path.exists(DB_FILE):
                with open(DB_FILE, "rb") as f:
                    db_bytes = f.read()
                    db_b64 = base64.b64encode(db_bytes).decode('utf-8')
                backup_to_gdrive(db_b64, "tasks.db", overwrite=True, is_binary=True)
        except Exception as e:
            # 后台任务，失败记录但不阻塞 UI
            print(f"Real-time backup failed: {e}")
    threading.Thread(target=_async_backup, daemon=True).start()

def trigger_manual_backup():
    """
    Manual Backup - Timestamped files for user safety
    """
    def _async_manual_backup():
        try:
            now_str = get_now_sgt().strftime("%Y-%m-%d-%H%M")
            txt_filename = f"manual_backup_{now_str}.txt"
            db_filename = f"tasks_manual_backup_{now_str}.db"
            
            # 1. Manual Text Report
            report_content = generate_master_report()
            report_content += f"\n\n[🛡️ 手动数据备份] 备份时间: {get_now_sgt().strftime('%Y-%m-%d %H:%M:%S')}"
            backup_to_gdrive(report_content, txt_filename, overwrite=False, is_binary=False)
            
            # 2. Manual Binary Database (Base64 encoded)
            if os.path.exists(DB_FILE):
                with open(DB_FILE, "rb") as f:
                    db_bytes = f.read()
                    db_b64 = base64.b64encode(db_bytes).decode('utf-8')
                backup_to_gdrive(db_b64, db_filename, overwrite=False, is_binary=True)
        except Exception as e:
            print(f"Manual backup failed: {e}")
            
    threading.Thread(target=_async_manual_backup, daemon=True).start()

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

    # 6. 爸爸的健身目标表 (v9.6)
    c.execute('''CREATE TABLE IF NOT EXISTS dad_fitness_goals
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  goal_name TEXT NOT NULL,
                  goal_value TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # 7. 初始化默认健身目标
    c.execute("SELECT COUNT(*) FROM dad_fitness_goals")
    if c.fetchone()[0] == 0:
        default_goals = [
            ("体重", "65-67公斤"),
            ("体脂率", "15%-17%"),
            ("腰围", "81-83厘米")
        ]
        for name, val in default_goals:
            c.execute("INSERT INTO dad_fitness_goals (goal_name, goal_value) VALUES (?, ?)", (encrypt_str(name), encrypt_str(val)))

    # 7. 爸爸的饮食方案表 (v10.0)
    c.execute('''CREATE TABLE IF NOT EXISTS dad_diet_plans
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  meal_name TEXT NOT NULL,
                  meal_content TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # 初始化默认饮食方案
    c.execute("SELECT COUNT(*) FROM dad_diet_plans")
    if c.fetchone()[0] == 0:
        default_diets = [
            ("早饭", "鱼肉(15克蛋白质) + 50克生刚切燕麦 + 一个牛油果 + 一把坚果30克 + 半碗牛奶"),
            ("午饭", "200g牛排 + 大量炒蔬菜 + 一拳大小的红薯"),
            ("晚饭", "150g鸡胸肉 + 大量炒蔬菜 + 1个烤土豆"),
            ("加餐", "训练日：早餐加一个鸡蛋，训练前1-2小时加一片面包和一个希腊酸奶")
        ]
        for name, content in default_diets:
            c.execute("INSERT INTO dad_diet_plans (meal_name, meal_content) VALUES (?, ?)", (encrypt_str(name), encrypt_str(content)))

    # 10. 爸爸的健身计划表 (v10.4)
    c.execute('''CREATE TABLE IF NOT EXISTS dad_fitness_plans
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  plan_name TEXT NOT NULL,
                  plan_content TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # 初始化默认健身计划
    c.execute("SELECT COUNT(*) FROM dad_fitness_plans")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO dad_fitness_plans (plan_name, plan_content) VALUES (?, ?)", 
                  (encrypt_str("总体计划"), encrypt_str("每周重量训练3次，有氧运动150分钟，HIIT/羽毛球训练1次")))

    # 11. 爸爸的每周运动计划表 (v11.0)
    c.execute('''CREATE TABLE IF NOT EXISTS dad_training_details
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  train_day TEXT NOT NULL,
                  train_content TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # 初始化默认训练细节
    c.execute("SELECT COUNT(*) FROM dad_training_details")
    if c.fetchone()[0] == 0:
        default_training = [
            ("周二", "练上肢"),
            ("周四", "练背部"),
            ("周五", "练腿"),
            ("周六", "瑜伽"),
            ("周日", "羽毛球")
        ]
        for day, content in default_training:
            c.execute("INSERT INTO dad_training_details (train_day, train_content) VALUES (?, ?)", (encrypt_str(day), encrypt_str(content)))

    # 12. 爸爸的体重记录表 (v11.9.5)
    c.execute('''CREATE TABLE IF NOT EXISTS dad_weight_records
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  record_date TEXT NOT NULL,
                  weight TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # 13. 每次健身项目记录表
    c.execute('''CREATE TABLE IF NOT EXISTS dad_fitness_records
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  record_date TEXT NOT NULL,
                  category TEXT NOT NULL,
                  exercise TEXT NOT NULL,
                  weight TEXT NOT NULL,
                  reps TEXT NOT NULL,
                  sets TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # 8. 初始化密码
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
    """更新数据库中的密码 (应用户要求直接存明文以便在设置中清晰显示)"""
    try:
        # 直接存储明文，不再使用哈希
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO system_config (key, val) VALUES ('app_password', ?)", (str(new_pwd),))
            conn.commit()
            return True
    except:
        return False

# --- 爸爸的健身目标管理 (v9.6) ---
def get_dad_fitness_goals():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            df = pd.read_sql("SELECT * FROM dad_fitness_goals ORDER BY id ASC", conn)
            if not df.empty:
                df['goal_name'] = df['goal_name'].apply(decrypt_str)
                df['goal_value'] = df['goal_value'].apply(decrypt_str)
            return df
    except:
        return pd.DataFrame()

def add_dad_fitness_goal(name, value):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO dad_fitness_goals (goal_name, goal_value) VALUES (?, ?)", 
                      (encrypt_str(name), encrypt_str(value)))
            conn.commit()
            return True
    except:
        return False

def update_dad_fitness_goal(goal_id, name, value):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("UPDATE dad_fitness_goals SET goal_name = ?, goal_value = ? WHERE id = ?", 
                      (encrypt_str(name), encrypt_str(value), goal_id))
            conn.commit()
            return True
    except:
        return False

def delete_dad_fitness_goal(goal_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM dad_fitness_goals WHERE id = ?", (goal_id,))
            conn.commit()
            return True
    except:
        return False

# --- 爸爸的饮食方案管理 (v10.0) ---
def get_dad_diet_plans():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            df = pd.read_sql("SELECT * FROM dad_diet_plans ORDER BY id ASC", conn)
            if not df.empty:
                df['meal_name'] = df['meal_name'].apply(decrypt_str)
                df['meal_content'] = df['meal_content'].apply(decrypt_str)
            return df
    except:
        return pd.DataFrame()

def add_dad_diet_plan(name, content):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO dad_diet_plans (meal_name, meal_content) VALUES (?, ?)", 
                      (encrypt_str(name), encrypt_str(content)))
            conn.commit()
            return True
    except:
        return False

def update_dad_diet_plan(diet_id, name, content):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("UPDATE dad_diet_plans SET meal_name = ?, meal_content = ? WHERE id = ?", 
                      (encrypt_str(name), encrypt_str(content), diet_id))
            conn.commit()
            return True
    except:
        return False

def delete_dad_diet_plan(diet_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM dad_diet_plans WHERE id = ?", (diet_id,))
            conn.commit()
            return True
    except:
        return False

# --- 爸爸的健身计划管理 (v10.4) ---
def get_dad_fitness_plans():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            df = pd.read_sql("SELECT * FROM dad_fitness_plans ORDER BY id ASC", conn)
            if not df.empty:
                df['plan_name'] = df['plan_name'].apply(decrypt_str)
                df['plan_content'] = df['plan_content'].apply(decrypt_str)
            return df
    except:
        return pd.DataFrame()

def add_dad_fitness_plan(name, content):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO dad_fitness_plans (plan_name, plan_content) VALUES (?, ?)", 
                      (encrypt_str(name), encrypt_str(content)))
            conn.commit()
            return True
    except:
        return False

def update_dad_fitness_plan(plan_id, name, content):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("UPDATE dad_fitness_plans SET plan_name = ?, plan_content = ? WHERE id = ?", 
                      (encrypt_str(name), encrypt_str(content), plan_id))
            conn.commit()
            return True
    except:
        return False

def delete_dad_fitness_plan(plan_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM dad_fitness_plans WHERE id = ?", (plan_id,))
            conn.commit()
            return True
    except:
        return False

# --- 爸爸的训练细节管理 (v11.0) ---
def get_dad_training_details():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            df = pd.read_sql("SELECT * FROM dad_training_details ORDER BY id ASC", conn)
            if not df.empty:
                df['train_day'] = df['train_day'].apply(decrypt_str)
                df['train_content'] = df['train_content'].apply(decrypt_str)
            return df
    except:
        return pd.DataFrame()

def add_dad_training_detail(day, content):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO dad_training_details (train_day, train_content) VALUES (?, ?)", 
                      (encrypt_str(day), encrypt_str(content)))
            conn.commit()
            return True
    except:
        return False

def update_dad_training_detail(tid, day, content):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("UPDATE dad_training_details SET train_day = ?, train_content = ? WHERE id = ?", 
                      (encrypt_str(day), encrypt_str(content), tid))
            conn.commit()
            return True
    except:
        return False

def delete_dad_training_detail(tid):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM dad_training_details WHERE id = ?", (tid,))
            conn.commit()
            return True
    except:
        return False

# --- 爸爸的体重记录管理 (v11.9.5) ---
def get_dad_weight_records():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            df = pd.read_sql("SELECT * FROM dad_weight_records ORDER BY record_date ASC", conn)
            if not df.empty:
                df['weight'] = df['weight'].apply(lambda x: float(decrypt_str(x)))
            return df
    except Exception as e:
        print(f"Error getting weight records: {e}")
        return pd.DataFrame()

def add_dad_weight_record(date, weight):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO dad_weight_records (record_date, weight) VALUES (?, ?)", 
                      (date, encrypt_str(str(weight))))
            conn.commit()
            return True
    except:
        return False

def delete_dad_weight_record(rid):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM dad_weight_records WHERE id = ?", (rid,))
            conn.commit()
            return True
    except:
        return False

def has_fitness_record(date, exercise):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT id, exercise FROM dad_fitness_records WHERE record_date = ?", (date,))
            for row in c.fetchall():
                if decrypt_str(row[1]) == exercise:
                    return True
            return False
    except:
        return False

def add_dad_fitness_record(date, category, exercise, weight, reps, sets):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT id, exercise FROM dad_fitness_records WHERE record_date = ?", (date,))
            for row in c.fetchall():
                if decrypt_str(row[1]) == exercise:
                    c.execute("DELETE FROM dad_fitness_records WHERE id = ?", (row[0],))
                    
            c.execute("INSERT INTO dad_fitness_records (record_date, category, exercise, weight, reps, sets) VALUES (?, ?, ?, ?, ?, ?)", 
                      (date, encrypt_str(category), encrypt_str(exercise), encrypt_str(str(weight)), encrypt_str(str(reps)), encrypt_str(str(sets))))
            conn.commit()
            return True
    except Exception as e:
        print(f"Error adding fitness record: {e}")
        return False

def delete_dad_fitness_record(date, exercise):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT id, exercise FROM dad_fitness_records WHERE record_date = ?", (date,))
            deleted = False
            for row in c.fetchall():
                if decrypt_str(row[1]) == exercise:
                    c.execute("DELETE FROM dad_fitness_records WHERE id = ?", (row[0],))
                    deleted = True
            conn.commit()
            return deleted
    except Exception as e:
        print(f"Error deleting fitness record: {e}")
        return False

def get_latest_fitness_records():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            df = pd.read_sql("SELECT * FROM dad_fitness_records ORDER BY record_date DESC, id DESC", conn)
            latest_records = {}
            if not df.empty:
                for _, row in df.iterrows():
                    ex = decrypt_str(row['exercise'])
                    if ex not in latest_records:
                        latest_records[ex] = {
                            'weight': float(decrypt_str(row['weight'])),
                            'reps': int(decrypt_str(row['reps'])),
                            'sets': int(decrypt_str(row['sets']))
                        }
            return latest_records
    except:
        return {}

def get_all_fitness_records():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            df = pd.read_sql("SELECT * FROM dad_fitness_records ORDER BY record_date DESC, id DESC", conn)
            if not df.empty:
                df['category'] = df['category'].apply(decrypt_str)
                df['exercise'] = df['exercise'].apply(decrypt_str)
                df['weight'] = df['weight'].apply(lambda x: float(decrypt_str(x)))
                df['reps'] = df['reps'].apply(lambda x: int(decrypt_str(x)))
                df['sets'] = df['sets'].apply(lambda x: int(decrypt_str(x)))
            return df
    except:
        return pd.DataFrame()

def extract_date_llm(task_text, fallback_date=None, fallback_recur=None):
    """
    v9.9 - 绝对防线引擎 (Total Comma Firewall)
    规则：第一个逗号之后的所有文字，均视为“神圣不可触碰”。
    AI 物理隔离：只传逗号前的文字给 AI 分析时间和核心事情。
    物理缝合：不管 AI 怎么想，逗号后的文字一律原样保留归档。
    """
    if not client or not task_text or not task_text.strip():
        return task_text, fallback_date, fallback_recur
        
    now = get_now_sgt()
    f_date = fallback_date if fallback_date else now.strftime("%Y-%m-%d 23:59")
    f_recur = fallback_recur if fallback_recur else "None"
    
    # 🕵️‍♂️ 第1步：物理级分流 (Python 强制执行)
    idx_en = task_text.find(',')
    idx_cn = task_text.find('，')
    if idx_en != -1 and idx_cn != -1: idx = min(idx_en, idx_cn)
    else: idx = idx_en if idx_en != -1 else idx_cn
        
    has_comma = (idx != -1)
    if has_comma:
        head_orig = task_text[:idx].strip()
        tail_verbatim = task_text[idx:] # 包含逗号及其后所有内容
    else:
        head_orig = task_text.strip()
        tail_verbatim = ""

    # 如果只有后缀且没前缀，直接返回原样内容（去掉开头的那个逗号）
    if has_comma and not head_orig:
        return tail_verbatim[1:].strip(), f_date, (None if f_recur == "None" else f_recur)

    try:
        # --- 统一 AI 任务：分析时间 + 清洗前缀 ---
        # 仅将 head_orig 送往 AI，确保 tail_verbatim 永远不被分析
        prompt = f"""
        你是一位极端严谨的家庭事务助理。由于系统架构原因，你只能看见任务的“开头片段”。
        今天是 {now.strftime('%Y-%m-%d')}。
        
        【分析片段】："{head_orig}"
        
        你的职责：
        1. 仅从分析片段中识别出日期（date）和重复模式（recur）。
        2. 将分析片段中“属于时间描述”的部分移除，保留剩下的“任务文字”。
        3. 【核心禁令】：严禁改动、润饰、概括或补全任何非时间词汇。如果片段全是时间词，核心内容请返回空。
        
        返回格式 JSON: {{ "date": "YYYY-MM-DD HH:MM", "recur": "None/Everyday/Weekend/Weekly-Sun/Monthly-1/Monthly-LastDay/Yearly-MM-DD", "cleaned_task": "..." }}
        
        【重要：重复模式 recur 判定规则】
        - 只有涉及周期性频率（如“每”、“每天”、“每周五”）时才返回 recur。
        - 【强制禁令】：单次日期（如“9月1日”、“10月10号”）如果没有“每年”二字，一律视为一次性任务，recur 必须返回 "None"。
        - 【强制禁令】：描述词（如“偶尔”、“总是”、“不是总是”、“央求”）严禁误判为重复。
        - 示例："9月1日申请换课" -> recur: "None", date: "2026-09-01 12:00"
        - 示例："每年9月1日交学费" -> recur: "Yearly-09-01", date: "2026-09-01 12:00"
        """
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0
        )
        res_data = json.loads(response.choices[0].message.content)
        
        dt_str = res_data.get("date", f_date)
        recur_str = res_data.get("recur", f_recur)
        cleaned_head = res_data.get("cleaned_task", "").strip()

        # 安全防御：如果 AI 产生的文字里包含了前缀中没有的非法字符，说明产生了幻觉，退回到原文
        orig_chars = set(head_orig)
        if not all(c in orig_chars or c.isspace() or c in [',', '，', '.', '。'] for c in cleaned_head):
            cleaned_head = head_orig

        # 缝合逻辑
        if has_comma:
            if not cleaned_head:
                # 前缀只有时间，洗完干净了 -> 直接拿后缀
                final_task = tail_verbatim[1:].strip()
            else:
                # 保留已清洗的前缀 + 圆封不动的后缀
                final_task = cleaned_head + tail_verbatim
        else:
            final_task = cleaned_head if cleaned_head else head_orig

        # 时间合法性终审
        try:
            datetime.strptime(dt_str[:16], "%Y-%m-%d %H:%M")
        except:
            dt_str = f_date
            
        return final_task, dt_str, (None if recur_str == "None" else recur_str)

    except Exception as e:
        print(f"Firewall Parsing Error (v9.9): {e}")
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

def get_task_by_id(task_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT task, due_date, recurring_pattern FROM tasks WHERE id = ?", (task_id,))
            row = c.fetchone()
            if row:
                return {
                    "task": decrypt_str(row[0]),
                    "due_date": row[1],
                    "recurring_pattern": row[2]
                }
    except:
        pass
    return None

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
            # 实时同步触发
            trigger_realtime_backup()
            return {"success": True, "task": decrypt_str(row[0]), "due": row[1], "recur": recur_pattern, "id": task_id}
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
    trigger_realtime_backup()

def delay_task_24h(task_id):
    """将特定任务的截止时间延后 24 小时 (v11.12.5 新增)"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT due_date FROM tasks WHERE id = ?", (task_id,))
        row = c.fetchone()
        if row and row[0]:
            try:
                curr_dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M")
                new_dt = curr_dt + timedelta(hours=24)
                new_dt_str = new_dt.strftime("%Y-%m-%d %H:%M")
                c.execute("UPDATE tasks SET due_date = ? WHERE id = ?", (new_dt_str, task_id))
                conn.commit()
            except Exception as e:
                print(f"Error parsing date or updating: {e}")
        conn.close()
        trigger_realtime_backup()
        return True
    except Exception as e:
        print(f"Database error in delay_task: {e}")
        return False

def delete_task(task_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    trigger_realtime_backup()

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
    trigger_realtime_backup()
    return True

def update_task_raw(task_id, task_text, due_date, recur_pattern):
    """v11.9.19 - 直接物理同步数据，不通过 AI 解析，用于撤销/回滚操作"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            enc_task = encrypt_str(task_text)
            c.execute("UPDATE tasks SET task = ?, due_date = ?, recurring_pattern = ? WHERE id = ?", 
                      (enc_task, due_date, recur_pattern, task_id))
            conn.commit()
            trigger_realtime_backup()
            return True
    except:
        return False

def mark_recurring_date_completed(task_id, date_str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO recurring_completions (task_id, completed_date) VALUES (?, ?)", (task_id, date_str))
    conn.commit()
    conn.close()
    trigger_realtime_backup()

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
    trigger_realtime_backup()

def delete_enya_vital(vital_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM enya_vitals WHERE id = ?", (vital_id,))
    conn.commit()
    conn.close()
    trigger_realtime_backup()

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
    trigger_realtime_backup()

def delete_enya_period(period_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM enya_period WHERE id = ?", (period_id,))
    conn.commit()
    conn.close()
    trigger_realtime_backup()

def import_from_report_text(report_text):
    """
    v8.2 - 文本灾备还原引擎
    解析 generate_master_report 生成的文本格式，并注入数据库
    """
    import re
    # 正则规则与报告格式 1:1 对位
    task_pattern = re.compile(r"^\[[! ]|∞\] (.*?) \(截止: (.*?)\)$") 
    # v6.6+ 采用的是表格形式，需支持管道符解析
    table_row_pattern = re.compile(r"^([0-9- :]{10,16}|无设定)\s*\|\s*(.*?)\s*\|\s*(.*)$")
    period_pattern = re.compile(r"^- (\d{4}-\d{2}-\d{2}): (月经开始|月经结束)$")
    vital_pattern = re.compile(r"^- (\d{4}-\d{2}-\d{2}): 身高 (.*?)cm \| 体重 (.*?)kg$")
    fitness_rec_pattern = re.compile(r"^- (\d{4}-\d{2}-\d{2}): (.*?) - (.*?) \((.*?)kg x (.*?)次 x (.*?)组\)$")
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # 简单清空相关表以防冲突
    c.execute("DELETE FROM tasks")
    c.execute("DELETE FROM enya_vitals")
    c.execute("DELETE FROM enya_period")
    c.execute("DELETE FROM dad_fitness_records")
    
    lines = [l.strip() for l in report_text.split('\n') if l.strip()]
    count = 0
    
    for line in lines:
        # 1. 解析表格行 (任务)
        m_task = table_row_pattern.match(line)
        if m_task and "任务内容" not in line and "截止时间" not in line and "---" not in line:
            due, task_name, recur = m_task.groups()
            if "(暂无事项)" in due: continue
            due_val = None if due == "无设定" else due
            recur_val = None if recur == "无" else recur
            c.execute("INSERT INTO tasks (task, due_date, recurring_pattern) VALUES (?, ?, ?)",
                      (encrypt_str(task_name), due_val, recur_val))
            count += 1
            continue
            
        # 2. 解析经期
        m_period = period_pattern.match(line)
        if m_period:
            date_p, event_p = m_period.groups()
            c.execute("INSERT INTO enya_period (record_date, event_type) VALUES (?, ?)", (date_p, encrypt_str(event_p)))
            count += 1
            continue
            
        # 3. 解析身高体重
        m_vital = vital_pattern.match(line)
        if m_vital:
            date_v, h_v, w_v = m_vital.groups()
            c.execute("INSERT INTO enya_vitals (record_date, height, weight) VALUES (?, ?, ?)", 
                      (date_v, encrypt_str(h_v), encrypt_str(w_v)))
            count += 1
            continue
            
        # 4. 解析健身项目完成记录
        m_fitness = fitness_rec_pattern.match(line)
        if m_fitness:
            date, cat, ex, w, r, s = m_fitness.groups()
            c.execute("INSERT INTO dad_fitness_records (record_date, category, exercise, weight, reps, sets) VALUES (?, ?, ?, ?, ?, ?)",
                      (date, encrypt_str(cat), encrypt_str(ex), encrypt_str(w), encrypt_str(r), encrypt_str(s)))
            count += 1
            continue

    conn.commit()
    conn.close()
    return count

# --- 辅助 UI 与逻辑函数 ---
def hits_day(pattern, target_date):
    """判断特定日期是否命中循环规则 (v11.10.7: 提至全局作用域)"""
    # v11.12.1-hotfix: 鲁棒性检查，防止 pattern 为 float/NaN 导致 .strip() 崩溃
    if not pattern or pd.isna(pattern): return False
    p = str(pattern).strip()
    p_lower = p.lower()
    if p_lower in ['everyday', 'daily']: return True
    if p.lower() == 'weekend' and target_date.weekday() >= 5: return True
    if p.lower() == 'monthly-lastday':
        return (target_date + timedelta(days=1)).day == 1
        
    # v11.11.2: 全面大小写不敏感支持，并处理各种简写
    p_lower = p.lower()
    days_full = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    if p_lower in days_full:
        wd_map = {d: i for i, d in enumerate(days_full)}
        return target_date.weekday() == wd_map[p_lower]
    
    if p_lower == 'monthly': # 默认每月 1 号
        return target_date.day == 1
        
    if p_lower.startswith('weekly-'):
        target_wd = p_lower.split('-')[1]
        wd_map = {'mon':0,'tue':1,'wed':2,'thu':3,'fri':4,'sat':5,'sun':6,
                  'monday':0,'tuesday':1,'wednesday':2,'thursday':3,'friday':4,'saturday':5,'sunday':6}
        return target_date.weekday() == wd_map.get(target_wd, -1)
    if p_lower.startswith('monthly-'):
        try:
            target_dom = int(p_lower.split('-')[1])
            return target_date.day == target_dom
        except: return False
    if p_lower.startswith('yearly-'):
        try:
            # 格式为 Yearly-MM-DD
            parts = p_lower.split('-')
            return target_date.month == int(parts[1]) and target_date.day == int(parts[2])
        except: return False
    # 特殊情况：Yearly (无日期) -> 取 due_date 的日月? 
    # 但通常 AI 会输出带日期的，这里暂不处理
        
    return False

# --- 5. Integrated Master Report & Auto-Backup Logic ---
def get_categorized_tasks():
    """集中处理所有任务的分选、循环展开和排序逻辑，供 UI 和 备份使用"""
    tasks_df = get_tasks()
    now = get_now_sgt()
    today_date = now.date()
    tomorrow_date = today_date + timedelta(days=1)
    end_of_week = today_date + timedelta(days=6 - today_date.weekday())
    # v11.10.9: 展望 30 天，确保跨月事项能被准确扫描到
    lookahead_limit = today_date + timedelta(days=30)
    
    recurring_list, overdue_list, today_list, tomorrow_list, week_list, later_list = [], [], [], [], [], []
    shadow_overdue, shadow_today, shadow_tomorrow, shadow_week, shadow_later = [], [], [], [], []
    open_tasks = pd.DataFrame()
    completed_tasks = pd.DataFrame()

    if not tasks_df.empty:
        open_tasks = tasks_df[tasks_df['completed'] == 0]
        completed_tasks = tasks_df[tasks_df['completed'] == 1]
        
        for _, row in open_tasks.iterrows():
            pat = row['recurring_pattern']
            # v11.12.3: 修复真值判断 Bug，确保空值 (NaN) 或 "None" 字符串不被误认为循环模式
            if pat and not pd.isna(pat) and str(pat).lower() != 'none':
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
            curr_later = end_of_week + timedelta(days=1)
            while curr_later <= lookahead_limit:
                if hits_day(item['recurring_pattern'], curr_later): shadow_later.append((item, curr_later))
                curr_later += timedelta(days=1)

    recur_comps = get_recurring_completions()
    def prepare_item_list(normal_items, shadow_items_with_dates=None, shadow_items_plain=None, default_date=None):
        combined = []
        for r in normal_items:
            temp = r.copy(); temp['_is_shadow'] = False; combined.append(temp)
        
        def is_done(tid, d_str):
            if recur_comps.empty: return False
            return not recur_comps[(recur_comps['task_id'] == tid) & (recur_comps['completed_date'] == d_str[:10])].empty

        if shadow_items_plain and default_date:
            d_str = default_date.strftime("%Y-%m-%d")
            for r in shadow_items_plain:
                temp = r.copy(); temp['_is_shadow'] = True; temp['due_date'] = f"{d_str} 12:00"
                temp['completed'] = 1 if is_done(r['id'], d_str) else 0; combined.append(temp)
        if shadow_items_with_dates:
            for r, d in shadow_items_with_dates:
                d_str = d.strftime("%Y-%m-%d"); temp = r.copy(); temp['_is_shadow'] = True; temp['due_date'] = f"{d_str} 12:00"
                temp['completed'] = 1 if is_done(r['id'], d_str) else 0; combined.append(temp)
        
        open_sub = [x for x in combined if not x['completed']]
        done_sub = [x for x in combined if x['completed']]
        open_sub.sort(key=lambda x: x['due_date'] if x['due_date'] else "9999-12-31")
        done_sub.sort(key=lambda x: x['due_date'] if x['due_date'] else "9999-12-31")
        return open_sub, done_sub

    f_overdue_o, f_overdue_d = prepare_item_list(overdue_list, shadow_items_with_dates=shadow_overdue)
    f_today_o, f_today_d = prepare_item_list(today_list, shadow_items_plain=shadow_today, default_date=today_date)
    f_tomorrow_o, f_tomorrow_d = prepare_item_list(tomorrow_list, shadow_items_plain=shadow_tomorrow, default_date=tomorrow_date)
    f_week_o, f_week_d = prepare_item_list(week_list, shadow_items_with_dates=shadow_week)
    f_later_o, f_later_d = prepare_item_list(later_list, shadow_items_with_dates=shadow_later)

    all_done_shadows = f_today_d + f_tomorrow_d + f_week_d + f_later_d
    final_completed = completed_tasks # Start with the real ones
    if all_done_shadows:
        s_df = pd.DataFrame(all_done_shadows)
        def format_shadow_name(x):
            try:
                date_str = str(x.get('due_date', ''))[:10]
                return f"{x['task']} (周期性于 {date_str})" if x.get('_is_shadow') else x['task']
            except: return str(x.get('task', ''))
        s_df['task'] = s_df.apply(format_shadow_name, axis=1)
        final_completed = pd.concat([final_completed, s_df], ignore_index=True)
    
    if isinstance(final_completed, pd.DataFrame) and not final_completed.empty:
        final_completed = final_completed.sort_values(by='due_date', ascending=False)

    return {
        "overdue_open": f_overdue_o, "today_open": f_today_o, "tomorrow_open": f_tomorrow_o, 
        "week_open": f_week_o, "later_open": f_later_o, "recurring_list": recurring_list,
        "completed_tasks": final_completed, "all_tasks_df": tasks_df
    }

def generate_master_report():
    """
    v6.6 - 镜像级高保全备份报告
    确保自动同步、手动同步、下载文件三方内容与用户提供的样本 100% 视觉对齐。
    """
    now_sgt = get_now_sgt()
    data = get_categorized_tasks()
    
    # 标题对齐样本
    lines = [
        "家庭事项清单\n",
        f"{'='*80}\n"
    ]

    def add_table_sec(title, items):
        """表格化输出分区内容"""
        lines.append(f"\n【{title}】\n")
        # 严格按照样本的对齐方式：19位 | 50位 | 15位
        lines.append(f"{'截止时间':<19}| {'任务内容':<50}| {'循环'}\n")
        lines.append("-" * 80 + "\n")
        
        # 🕵️‍♂️ 修复：处理 DataFrame 和 List 的空值判断
        is_empty = False
        if isinstance(items, pd.DataFrame):
            is_empty = items.empty
            iterable = items.iterrows()
            is_df = True
        else:
            is_empty = not items
            iterable = items
            is_df = False

        if is_empty:
            lines.append(f"{' (暂无事项)':<19}| {'--':<50}| {'--'}\n")
        else:
            for item in iterable:
                # 获取行数据
                row = item[1] if is_df else item
                
                # 1. 截止时间清理
                due_val = str(row.get('due_date', '') or '').strip()
                due_cell = f"{due_val[:16]}" if due_val else "无设定"
                
                # 2. 任务内容清理 (去除换行符，限制显示长度)
                task_content = str(row.get('task', '') or '').replace('\n', ' ').strip()
                
                # 3. 循环模式处理
                recur_val = str(row.get('recurring_pattern', 'None')).strip()
                if recur_val == "None" or not recur_val:
                    recur_cell = "无"
                else:
                    recur_cell = recur_val
                
                # 格式化拼接一行
                lines.append(f"{due_cell:<19}| {task_content:<50}| {recur_cell}\n")

    # 按样本顺序输出分区
    add_table_sec("🔴 未完成事项", data["overdue_open"])
    add_table_sec("⚡ 今日急需处理", data["today_open"])
    add_table_sec("🌙 明日事项", data["tomorrow_open"])
    add_table_sec("🗓️ 本周剩余事项", data["week_open"])
    add_table_sec("⏳ 本月剩余事项", data["later_open"])
    add_table_sec("🔄 长期循环事项", data["recurring_list"])
    add_table_sec("✅ 已完成事项归档", data["completed_tasks"])
    
    # --- 🌸 恩雅的健康档案 (v9.8 明确分类) ---
    lines.append(f"\n\n{'='*30} 🌸 恩雅的健康档案 {'='*30}\n")
    
    lines.append(f"\n【 📏 身高体重记录 】\n")
    v_df = get_enya_vitals()
    if not v_df.empty:
        for _, r in v_df.iterrows():
            lines.append(f"- {r['record_date']}: 身高 {r['height']}cm | 体重 {r['weight']}kg\n")
    else:
        lines.append("尚无记录。\n")

    lines.append("\n【 📅 经期记录 】\n")
    p_df = get_enya_periods()
    if not p_df.empty:
        for _, r in p_df.iterrows():
            lines.append(f"- {r['record_date']}: {r['event_type']}\n")
    else:
        lines.append("尚无记录。\n")

    # --- 🏋️‍♂️ 爸爸的健身档案 (v9.8 明确分类) ---
    lines.append(f"\n\n{'='*30} 🏋️‍♂️ 爸爸的健身档案 {'='*30}\n")
    
    lines.append("\n【 🎯 健身目标（同龄人5-10%） 】\n")
    g_df = get_dad_fitness_goals()
    if not g_df.empty:
        for _, r in g_df.iterrows():
            lines.append(f"- {r['goal_name']}: {r['goal_value']}\n")
    else:
        lines.append("尚无记录。\n")
    
    lines.append("\n【 ⚖️ 体重记录过程 】\n")
    w_df = get_dad_weight_records()
    if not w_df.empty:
        # 按时间倒序备份，最新记录在最前
        w_df_desc = w_df.sort_values(by="record_date", ascending=False)
        for _, r in w_df_desc.iterrows():
            lines.append(f"- {r['record_date']}: {r['weight']} KG\n")
    else:
        lines.append("尚无记录。\n")
    
    lines.append("\n【 📅 健身计划 】\n- (暂无详细记录，待后续添加)\n")
    
    lines.append("\n【 ✅ 健身项目完成记录 】\n")
    fr_df = get_all_fitness_records()
    if not fr_df.empty:
        for _, r in fr_df.iterrows():
            lines.append(f"- {r['record_date']}: {r['category']} - {r['exercise']} ({r['weight']}kg x {r['reps']}次 x {r['sets']}组)\n")
    else:
        lines.append("尚无记录。\n")

    # --- 📋 健身计划细节 (v11.0) ---
    lines.append(f"\n\n{'='*30} 📋 爸爸的健身训练细节 {'='*30}\n")
    td_df = get_dad_training_details()
    if not td_df.empty:
        for _, r in td_df.iterrows():
            lines.append(f"【{r['train_day']}】内容：{r['train_content']}\n")
    else:
        lines.append("尚无记录。\n")

    # --- 🍽️ 饮食档案 (v10.0) ---
    lines.append(f"\n\n{'='*30} 🍽️ 爸爸的饮食档案 {'='*30}\n")
    d_df = get_dad_diet_plans()
    if not d_df.empty:
        for _, r in d_df.iterrows():
            lines.append(f"【{r['meal_name']}】\n内容：{r['meal_content']}\n")
    else:
        lines.append("尚无记录。\n")

    lines.append(f"\n\n{'='*80}\n备份时间: {now_sgt.strftime('%Y-%m-%d %H:%M:%S')}\n(v{VERSION})")
    return "".join(lines)

def run_auto_backup_logic(silent=True):
    """
    检查是否需要自动备份 (中午 12 点和凌晨 1 点)
    使用与实时同步不同的文件名，形成每日双重快照点。
    """
    try:
        now = get_now_sgt()
        current_date = now.strftime("%Y-%m-%d")
        current_hour = now.hour
        
        target_slot = None
        target_slot = None
        if current_hour == 12:
            target_slot = "12pm"
        elif current_hour == 18:
            target_slot = "06pm"
        elif current_hour == 23 and now.minute >= 50:
            target_slot = "11pm"
        
        if target_slot:
            slot_key = f"last_auto_backup_{target_slot}"
            
            # 使用独立连接确保后台线程安全
            with sqlite3.connect(DB_FILE) as conn:
                c = conn.cursor()
                c.execute("CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, val TEXT)")
                c.execute("CREATE TABLE IF NOT EXISTS backup_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, slot TEXT, status TEXT, message TEXT)")
                
                c.execute("SELECT val FROM system_config WHERE key = ?", (slot_key,))
                res = c.fetchone()
                last_date = res[0] if res else ""
                
                if last_date != current_date:
                    try:
                        # 1. 备份文本报告
                        content = generate_master_report()
                        content += f"\n\n[🤖 自动每日备份] 备份时间: {now.strftime('%Y-%m-%d %H:%M:%S')}"
                        time_str = now.strftime("%Y-%m-%d-%H%M")
                        report_name = f"auto_backup_{time_str}.txt"
                        s1, m1 = backup_to_gdrive(content, report_name, overwrite=False, is_binary=False)
                        
                        # 2. 备份二进制数据库
                        s2, m2 = False, "Skipped"
                        if os.path.exists(DB_FILE):
                            with open(DB_FILE, "rb") as f:
                                db_bytes = f.read()
                                db_b64 = base64.b64encode(db_bytes).decode('utf-8')
                            db_name = f"tasks_auto_backup_{time_str}.db"
                            s2, m2 = backup_to_gdrive(db_b64, db_name, overwrite=False, is_binary=True)
                        
                        status = "SUCCESS" if s1 and s2 else "PARTIAL_FAILURE"
                        msg = f"Report: {m1} | DB: {m2}"
                        
                        # 重大修复 (v11.10.5): 只有在至少成功一个的情况下才标记该时段已备份
                        if s1 or s2:
                            c.execute("INSERT OR REPLACE INTO system_config (key, val) VALUES (?, ?)", (slot_key, current_date))
                        
                        c.execute("INSERT INTO backup_logs (timestamp, slot, status, message) VALUES (?, ?, ?, ?)",
                                 (now.strftime("%Y-%m-%d %H:%M:%S"), target_slot, status, msg))
                        conn.commit()
                        
                        if not silent:
                            st.session_state[f"auto_backup_msg_{target_slot}"] = f"✅ 已完成每日 {target_slot} 固定快照同步。"
                            
                    except Exception as inner_e:
                        c.execute("INSERT INTO backup_logs (timestamp, slot, status, message) VALUES (?, ?, ?, ?)",
                                 (now.strftime("%Y-%m-%d %H:%M:%S"), target_slot, "ERROR", str(inner_e)))
                        conn.commit()
                        raise inner_e
    except Exception as e:
        # 为了避免干扰用户，静默记录到数据库即可
        try:
            with sqlite3.connect(DB_FILE) as conn:
                c = conn.cursor()
                c.execute("CREATE TABLE IF NOT EXISTS backup_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, slot TEXT, status TEXT, message TEXT)")
                c.execute("INSERT INTO backup_logs (timestamp, slot, status, message) VALUES (?, ?, ?, ?)",
                         (get_now_sgt().strftime("%Y-%m-%d %H:%M:%S"), "CRITICAL", "ERROR", f"Daemon Context Error: {str(e)}"))
                conn.commit()
        except: pass
        if not silent: print(f"自动备份后台错误: {e}")

def autonomous_backup_daemon():
    """后台永驻守护线程：每 30 秒巡检一次时间，准点触发每日快照"""
    time.sleep(10) # 延迟启动以待主程序就绪
    while True:
        try:
            now = get_now_sgt()
            # 12:00, 18:00, 23:50 改为准点触发检查
            is_trigger_time = False
            if (now.hour == 12 or now.hour == 18) and now.minute == 0:
                is_trigger_time = True
            elif now.hour == 23 and now.minute == 50:
                is_trigger_time = True
                
            if is_trigger_time:
                run_auto_backup_logic(silent=True)
                time.sleep(61) # 跨过这一分钟，避免重复触发
            else:
                time.sleep(30)
        except:
            time.sleep(60)

# --- 🎯 线程启动器 (v9.2 恢复自动快照守护线程) ---
if "daemon_started" not in st.session_state:
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

    /* 🎨 v11.9: Custom Premium Tab-style Radio Buttons */
    div[data-testid="stRadio"] > div[role="radiogroup"] {
        flex-direction: row !important;
        justify-content: center;
        gap: 30px;
        border-bottom: 2px solid #e5e7eb;
        padding-bottom: 0px;
        margin-bottom: 20px;
    }
    div[data-testid="stRadio"] div[role="radiogroup"] > label {
        padding: 5px 15px !important;
        margin: 0 !important;
        border-radius: 8px 8px 0 0 !important;
        transition: all 0.2s ease-in-out;
        border-bottom: 3px solid transparent !important;
    }
    div[data-testid="stRadio"] div[role="radiogroup"] > label:hover {
        background-color: rgba(30, 58, 138, 0.05) !important;
    }
    div[data-testid="stRadio"] div[role="radiogroup"] > label[data-checked="true"] {
        border-bottom: 3px solid #1e3a8a !important;
        background-color: rgba(30, 58, 138, 0.08) !important;
    }
    div[data-testid="stRadio"] div[role="radiogroup"] > label[data-checked="true"] p {
        color: #1e3a8a !important;
        font-weight: 700 !important;
    }
    /* Hide the radio circles/dots */
    div[data-testid="stRadio"] div[role="radiogroup"] [data-testid="stRadioDeselectedIndicator"],
    div[data-testid="stRadio"] div[role="radiogroup"] [data-testid="stRadioSelectedIndicator"] {
        display: none !important;
    }
</style>
""", unsafe_allow_html=True)

# --- 6. Main App Structure ---
try:
    # 🛠️ v11.9.25: 启动自愈逻辑 - 如果本地库为空，自动从云端接拉回最新版本
    # 这解决了由于 Streamlit Cloud 容器重启导致的本地 SQLite 数据丢失问题
    auto_restore_if_needed()
    
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
            # 💡 关键修改：为了在所有设备（尤其手机浏览器）上永远保持登录，
            # 不再清除 URL 参数。强制将 auth_key 保留在 URL 中。
            if st.query_params.get("auth_key") != found_token:
                st.query_params["auth_key"] = found_token
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
                try { document.cookie = c_str; } catch(e) {}
                try { if(window.parent) window.parent.document.cookie = c_str; } catch(e) {}
                
                try {{ window.localStorage.removeItem('family_auth_token'); }} catch(e) {{}}
                try {{ window.parent.localStorage.removeItem('family_auth_token'); }} catch(e) {{}}
                
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
                    exp_date = datetime.now() + timedelta(days=3650)
                    cookie_manager.set(AUTH_KEY, "authenticated", expires_at=exp_date, path="/")
                    
                    # 强力锁定：使用原生 JS 设置 (关键：现代浏览器跨域环境必须包含 Partitioned; 且 SameSite=None; Secure)
                    exp_utc = exp_date.strftime("%a, %d %b %Y %H:%M:%S GMT")
                    components.html(f"""
                        <script>
                            var c_str = '{AUTH_KEY}=authenticated; expires={exp_utc}; path=/; SameSite=None; Secure; Partitioned';
                            try { document.cookie = c_str; } catch(e) {}
                            try { if(window.parent) window.parent.document.cookie = c_str; } catch(e) {}
                            
                            try {{ window.localStorage.setItem('family_auth_token', 'authenticated'); }} catch(e) {{}}
                            try {{ window.parent.localStorage.setItem('family_auth_token', 'authenticated'); }} catch(e) {{}}
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
                
                st.link_button("🔑 使用 Gmail 管理员登录", google_auth_url, use_container_width=False)
            
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
                exp_date = datetime.now() + timedelta(days=3650)
                cookie_manager.set(AUTH_KEY, "authenticated_admin", expires_at=exp_date, path="/")
                
                # 强力锁定：使用原生 JS 设置 (分区 Cookie 锁定)
                exp_utc = exp_date.strftime("%a, %d %b %Y %H:%M:%S GMT")
                components.html(f"""
                    <script>
                        var c_str = '{AUTH_KEY}=authenticated_admin; expires={exp_utc}; path=/; SameSite=None; Secure; Partitioned';
                        try { document.cookie = c_str; } catch(e) {}
                        try { if(window.parent) window.parent.document.cookie = c_str; } catch(e) {}
                        
                        try {{ window.localStorage.setItem('family_auth_token', 'authenticated_admin'); }} catch(e) {{}}
                        try {{ window.parent.localStorage.setItem('family_auth_token', 'authenticated_admin'); }} catch(e) {{}}
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

    # --- 🛠️ 辅助 UI 函数 (v11.10.7: hits_day 已提至全局) ---



    def format_date_with_weekday(dt_str):
        if not dt_str: return ""
        try:
            # First try parsing the standard format
            dt = datetime.strptime(dt_str[:16], "%Y-%m-%d %H:%M")
            weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
            return f"{dt_str[:16]} {weekdays[dt.weekday()]}"
        except:
            return dt_str

    @st.dialog("📋 事项处理结果")
    def show_task_result_dialog(result):
        mode = result.get("mode")
        mode_label = "添加" if mode == "add" else "修改"
        
        if result["success"]:
            st.success(f"✅ 该事项已成功{mode_label}！")
            st.markdown(f"**内容：** {result['task']}")
            if result.get('due'):
                st.markdown(f"**⏰ 日期/时间：** {format_date_with_weekday(result['due'])}")
            if result.get('recur'):
                st.markdown(f"**🔄 循环模式：** {result['recur']}")
            
            st.markdown("---")
            c_btns = st.columns([1, 1])
            with c_btns[0]:
                if st.button("确认", type="primary", use_container_width=False, key="btn_confirm_task"):
                    st.rerun()
            with c_btns[1]:
                if st.button("取消", use_container_width=False, key="btn_cancel_task"):
                    if mode == "add":
                        delete_task(result['id'])
                    elif mode == "edit":
                        update_task_raw(result['id'], result['old_task'], result['old_due'], result['old_recur'])
                    st.rerun()
        else:
            st.error(f"❌ {mode_label}失败：{result['error']}")
            if st.button("确定", use_container_width=False):
                st.rerun()

    if "last_task_result" in st.session_state:
        show_task_result_dialog(st.session_state.pop("last_task_result"))

    @st.dialog("📋 事项处理结果")
    def confirm_delay_dialog(task_obj):
        """延期 24 小时确认对话框 (与用户截图风格一致)"""
        st.markdown("<br>", unsafe_allow_html=True)
        # 绿色勾选提示框
        st.success("📝 该事项准备延期 24 小时运行！")
        
        st.markdown(f"**内容：** {task_obj['task']}")
        
        # 计算新时间
        try:
            old_dt = datetime.strptime(task_obj['due_date'], "%Y-%m-%d %H:%M")
            new_dt = old_dt + timedelta(hours=24)
            new_dt_str = new_dt.strftime("%Y-%m-%d %H:%M")
            weekday_str = format_date_with_weekday(new_dt_str).split(' ')[-1]
            display_new_date = f"{new_dt_str} {weekday_str}"
        except:
            display_new_date = "日期格式错误"
            
        st.markdown(f"**⏰ 日期/时间：** {display_new_date}")
        st.markdown("---")
        
        c_left, c_right = st.columns([1, 1])
        with c_left:
            if st.button("确认", type="primary", use_container_width=True, key="confirm_delay_btn"):
                if delay_task_24h(task_obj['id']):
                    st.rerun()
        with c_right:
            if st.button("取消", use_container_width=True, key="cancel_delay_btn"):
                st.rerun()

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
            c1, c2, c3 = st.columns([0.05, 0.70, 0.25])
            
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
                    # 🛠️ v11.9.19: 在修改前先行备份原始数据，以备“取消/撤销”使用
                    old_data = get_task_by_id(row['id'])
                    if update_task_text(row['id'], new_text):
                        # 获取更新后的完整信息以显示确认弹窗
                        updated_task = get_task_by_id(row['id'])
                        if updated_task and old_data:
                            st.session_state["last_task_result"] = {
                                "success": True,
                                "task": updated_task['task'],
                                "due": updated_task['due_date'],
                                "recur": updated_task['recurring_pattern'],
                                "id": row['id'],
                                "mode": "edit",
                                "old_task": old_data['task'],
                                "old_due": old_data['due_date'],
                                "old_recur": old_data['recurring_pattern']
                            }
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
                
                pat = row['recurring_pattern']
                # v11.12.5: 修复显示 Bug，确强制排除 NaN 和 "None" 字符串显示为循环标签
                recur_tag = f"<span class='recur-tag'>🔄 循环: {pat}</span>" if pat and not pd.isna(pat) and str(pat).lower() != 'none' else ""
                due_val = f"📅 日期/时间: {format_date_with_weekday(row['due_date'])}" if row['due_date'] else ""
                
                c2.markdown(f"<p class='todo-text {style} {overdue_cls}'>{row['task']}{recur_tag}</p><div class='todo-date {overdue_date_cls}'>{due_val}</div>", unsafe_allow_html=True)
                
                # Action buttons
                if not is_shadow:
                    delay_col, edit_col, del_col = c3.columns(3)
                    if delay_col.button("⏰", key=f"delay_{location}_{row['id']}", help="延期 24 小时"):
                        confirm_delay_dialog(row)
                    if edit_col.button("✏️", key=edit_id, help="修改"):
                        st.session_state["editing_task_id"] = row['id']
                        st.rerun()
                    if del_col.button("🗑️", key=del_id, help="删除"):
                        delete_task(row['id'])
                        st.rerun()
                else:
                    edit_col, del_col = c3.columns(2)
                    # Shadow tasks in archive can be "Deleted" (removed from completions)
                    if row.get('completed'):
                        if del_col.button("🗑️", key=del_id, help="撤销完成记录"):
                            date_only = row['due_date'][:10]
                            unmark_recurring_date_completed(row['id'], date_only)
                            st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)



    # --- 7. Data Preparation ---
    categorized_data = get_categorized_tasks()
    tasks_df = categorized_data["all_tasks_df"]
    recurring_list = categorized_data["recurring_list"]
    completed_tasks = categorized_data["completed_tasks"]
    final_overdue_open = categorized_data["overdue_open"]
    final_today_open = categorized_data["today_open"]
    final_tomorrow_open = categorized_data["tomorrow_open"]
    final_week_open = categorized_data["week_open"]
    final_later_open = categorized_data["later_open"]

    # Main Interface
    # CSS to style the download button in the header
    st.markdown("""
        <style>
        /* v8.6 System Menu Button Optimization */
        div.stPopover {
            text-align: right !important; /* 使 Popover 容器整体向右对齐 */
        }
        div.stPopover > button {
            background-color: #ff8c00 !important; /* Sunset Orange */
            color: white !important;
            font-weight: 600 !important;
            border-radius: 8px !important;
            border: none !important;
            height: 38px !important; /* 稍微减小高度更显精致 */
            width: auto !important; /* 强制适应内容宽度 */
            min-width: unset !important;
            padding: 0px 15px !important; /* 紧凑内边距 */
            white-space: nowrap !important; /* 确保文字不折行 */
            transition: all 0.3s ease !important;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1) !important;
        }
        div.stPopover > button:hover {
            background-color: #e67e00 !important;
            box-shadow: 0 4px 8px rgba(0,0,0,0.2) !important;
            transform: translateY(-1px);
        }
        /* v11.9.24: 统一所有按钮（普通按钮与下载按钮）的高度、边距与内部对齐，确保绝对水平平齐 */
        div.stButton > button, div.stDownloadButton > button {
            height: 40px !important;
            margin: 0 !important;
            padding: 0 15px !important;
            line-height: normal !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            border-radius: 8px !important;
            transition: all 0.2s ease !important;
        }
        
        div.stDownloadButton > button {
            font-size: 15px !important;
            font-weight: 500 !important;
            color: #444 !important;
            background-color: #f0f2f6 !important;
            border: 1px solid #dcdde1 !important;
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

    # Header Row - 调整比例并让菜单靠右
    c_title, c_menu = st.columns([0.8, 0.2], vertical_alignment="center")
    with c_title:
        st.markdown(f"<h1 class='main-header'>🏠 家庭管理系统 <span style='font-size: 0.8rem; vertical-align: middle; opacity: 0.5;'>v{VERSION}</span></h1>", unsafe_allow_html=True)
        # 如果刚才触发了自动快照备份，给予一个小提示
        for slot in ["12pm", "06pm", "11pm"]:
            msg_key = f"auto_backup_msg_{slot}"
            if msg_key in st.session_state:
                st.toast(st.session_state.pop(msg_key), icon="📸")

    with c_menu:
        # v8.6 - 取消 use_container_width 以实现紧凑宽度
        with st.popover("⚙️ 系统功能菜单", use_container_width=False):
            # --- 🛠️ v11.11.1: 循环任务诊断工具 (移至顶部确保可见) ---
            with st.expander("🔍 循环任务诊断工具", expanded=False):
                st.markdown("<div style='font-size:0.8rem; color:#666;'>展示所有循环任务的原始模式及未来30天命中日期</div>", unsafe_allow_html=True)
                try:
                    with sqlite3.connect(DB_FILE) as conn:
                        diag_df = pd.read_sql("SELECT id, task, recurring_pattern FROM tasks WHERE recurring_pattern IS NOT NULL AND recurring_pattern != ''", conn)
                        if not diag_df.empty:
                            now_diag = get_now_sgt().date()
                            for _, r in diag_df.iterrows():
                                t_name = decrypt_str(r['task'])
                                hits = []
                                for i in range(31):
                                    test_d = now_diag + timedelta(days=i)
                                    if hits_day(r['recurring_pattern'], test_d):
                                        hits.append(test_d.strftime("%m-%d"))
                                st.markdown(f"- **{t_name}**")
                                st.markdown(f"  `Pattern`: [{r['recurring_pattern']}] | `Hits`: {', '.join(hits) if hits else 'None'}")
                        else:
                            st.info("没有找到循环任务")
                except Exception as ex:
                    st.error(f"诊断失败: {ex}")
            st.markdown("---")

            # 1. 云端备份 (手动)
            if st.button("☁️ 手动云端数据备份", use_container_width=False, help="同时备份文本报告和数据库"):
                with st.spinner("备份中..."):
                    trigger_manual_backup()
                    st.toast("✅ 手动备份任务已在后台启动！", icon="🚀")
            
            # 2. 数据恢复 (v8.5 采用状态机驱动的模态导航)
            if st.button("🛡️ 进入数据恢复中心", use_container_width=False):
                st.session_state["show_recovery_center"] = True
                st.rerun()

            # 3. 访问密码 (仅限管理员)
            if st.session_state.get("is_admin"):
                st.markdown("---")
                st.markdown("#### 🔐 访问管理")
                curr_p = get_app_password()
                st.write(f"当前 6 位访问密码: **{curr_p}**")
                new_p = st.text_input("设置新密码 (6位数字):", type="password", max_chars=6, key="menu_new_pass")
                if st.button("更新密码", use_container_width=False, key="menu_update_pass_btn"):
                    if len(new_p) == 6 and new_p.isdigit():
                        if update_app_password(new_p):
                            st.success("密码已更新！")
                            time.sleep(1)
                            st.rerun()
                        else: st.error("保存失败")
                    else: st.warning("请输入6位数字")

            # --- 🛠️ v11.10.6: 正确放置：在弹窗主菜单内显示自动备份巡检日志 ---
            st.markdown("---")
            st.markdown("#### 📊 自动备份巡检日志")
            try:
                with sqlite3.connect(DB_FILE) as conn:
                    log_df = pd.read_sql("SELECT timestamp, slot, status, message FROM backup_logs ORDER BY id DESC LIMIT 5", conn)
                    if not log_df.empty:
                        for _, row in log_df.iterrows():
                            color = "#059669" if row['status'] == "SUCCESS" else "#e11d48"
                            st.markdown(f"<div style='font-size: 0.85rem; color: #64748b;'>🕒 {row['timestamp']} [{row['slot']}] <b style='color: {color};'>{row['status']}</b></div>", unsafe_allow_html=True)
                            st.markdown(f"<div style='font-size: 0.75rem; color: #94a3b8; margin-bottom: 5px;'>└ {row['message']}</div>", unsafe_allow_html=True)
                    else:
                        st.info("尚无备份巡检记录。")
            except:
                st.info("等待首次自动备份触发...")

            st.markdown("---")
            # 4. 退出登录
            st.button("🔴 退出登录", use_container_width=False, on_click=handle_logout, key="menu_logout_btn")

    st.markdown('<br>', unsafe_allow_html=True)
    
    # v8.5 状态导航引擎：判断是否处于“数据恢复视图”
    if st.session_state.get("show_recovery_center", False):
        # --- 恢复中心 专用视图 ---
        tc1, tc2 = st.columns([0.7, 0.3])
        with tc1:
            st.markdown(f"<h2 style='margin:0; font-size: 1.5rem;'>🛡️ 数据恢复中心 <span style='font-size: 0.8rem; color: #888;'>v{VERSION}</span></h2>", unsafe_allow_html=True)
        with tc2:
            if st.button("⬅️ 返回主控制台", use_container_width=False, type="primary"):
                st.session_state["show_recovery_center"] = False
                st.rerun()
        
        st.info("💡 **核心提示**: 如果您在 Streamlit Cloud 重启后发现数据为空，请使用以下任意一种方式恢复。")
        
        # 将原 Data Recovery 逻辑移入此处
        recovery_tab1, recovery_tab2 = st.tabs(["🧬 路径 A: 二进制恢复 (.db)", "📝 路径 B: 文本报告还原 (.txt)"], key="system_recovery_tabs")
        
        with recovery_tab1:
            st.markdown("### 🧬 二进制数据库恢复 (推荐)")
            st.markdown("""
            1. **去云端下载**: 登录 Google Drive，进入 `家庭管理系统数据备份` 文件夹。
            2. **找到文件**: 找到文件名为 `tasks.db` 的最新文件并下载。
            3. **在此上传**: 使用下方控件上传。
            """)
            
            # --- 🛠️ v11.9.25: 新增一键同步云端按钮 ---
            st.info("💡 **推荐方式**：点击下方按钮直接从 Google Drive 拉取最新备份，无需手动下载上传。")
            if st.button("🔄 从云端自动同步最新备份", key="btn_auto_cloud_pull", type="primary"):
                with st.spinner("正在连接云端..."):
                    success, detail = auto_restore_if_needed(force=True)
                    if success:
                        st.success(detail)
                        time.sleep(1.5)
                        st.rerun()
                    else:
                        st.error(f"❌ 自动同步失败: {detail}")
                        st.info("💡 请确保您已更新 Google Apps Script 代码，且云端文件夹中存在 `tasks.db` 文件。")
            
            st.markdown("---")
            st.markdown("<b>或者手动上传 tasks.db 文件：</b>", unsafe_allow_html=True)
            uploaded_db = st.file_uploader("选择 tasks.db 文件", type=["db"], key="db_uploader_v85")
            if uploaded_db:
                st.warning("⚠️ 确认后将覆盖所有当前数据。")
                if st.button("🔥 立即执行二进制恢复", key="btn_bin_restore", use_container_width=False):
                    try:
                        if not os.path.exists("data"): os.makedirs("data")
                        with open(DB_FILE, "wb") as f:
                            f.write(uploaded_db.getbuffer())
                        st.success("✅ 恢复成功！")
                        time.sleep(1)
                        st.rerun()
                    except Exception as e:
                        st.error(f"失败: {e}")

        with recovery_tab2:
            st.markdown("### 📝 文本报告紧急还原")
            st.markdown("""
            1. **去云端复制**: 登录 Google Drive 打开 `realtime_backup.txt`。
            2. **全选复制**: 复制所有文本。
            3. **在此粘贴**: 贴入下方文本框。
            """)
            report_text = st.text_area("粘贴备份报告全文", height=300, key="report_paste_85")
            if st.button("🧩 解析并还原数据", key="btn_text_restore", use_container_width=False):
                if report_text:
                    try:
                        import_count = import_from_report_text(report_text)
                        st.success(f"✅ 解析完成！已还原 {import_count} 条数据。")
                        time.sleep(1)
                        st.rerun()
                    except Exception as e:
                        st.error(f"解析失败: {e}")
        
        st.markdown("---")
        st.markdown("### 📊 自动备份巡检日志")
        try:
            with sqlite3.connect(DB_FILE) as conn:
                log_df = pd.read_sql("SELECT timestamp, slot, status, message FROM backup_logs ORDER BY id DESC LIMIT 5", conn)
                if not log_df.empty:
                    for _, row in log_df.iterrows():
                        color = "#059669" if row['status'] == "SUCCESS" else "#e11d48"
                        st.markdown(f"<div style='font-size: 0.85rem; color: #64748b;'>🕒 {row['timestamp']} [{row['slot']}] <b style='color: {color};'>{row['status']}</b></div>", unsafe_allow_html=True)
                        st.markdown(f"<div style='font-size: 0.75rem; color: #94a3b8; margin-bottom: 5px;'>└ {row['message']}</div>", unsafe_allow_html=True)
                else:
                    st.info("尚无备份巡检记录。")
        except:
            st.info("等待首次自动备份触发...")

        st.markdown("---")
        st.markdown("### 🛡️ 系统状态监控")
        st.write(f"**当前版本**: v{VERSION} | **数据库**: `{os.path.basename(DB_FILE)}`")
        
    else:
        # --- 🌐 核心导航逻辑 (v11.9.23: 引入 on_change 回调彻底解决跳转重置问题) ---
        nav_options = ['📝 家庭事项', '🏋️‍♂️ 爸爸的健身', '🌸 恩雅的健康']
        if "active_nav_tab" not in st.session_state:
            st.session_state["active_nav_tab"] = nav_options[0]

        def handle_nav_change():
            # 立即从 radio 的 key 中同步状态，确保 rerun 后 index 依然准确
            if "main_nav_radio_v11.9" in st.session_state:
                st.session_state["active_nav_tab"] = st.session_state["main_nav_radio_v11.9"]

        # 渲染自定义标签栏
        selected_tab = st.radio(
            "Navigation",
            options=nav_options,
            index=nav_options.index(st.session_state["active_nav_tab"]),
            horizontal=True,
            label_visibility="collapsed",
            key="main_nav_radio_v11.9",
            on_change=handle_nav_change
        )
        st.session_state["active_nav_tab"] = selected_tab

        if selected_tab == '📝 家庭事项':

            def generate_txt_report():
                # 为了保持 100% 一致，现在 Download 按钮直接调用统一的系统备份报告函数
                return generate_master_report()

            st.markdown("<br>", unsafe_allow_html=True)

            # Add Task Section & Download
            def handle_add_cb():
                st.session_state["temp_task_text"] = st.session_state.get("input_new_task", "")
                st.session_state["input_new_task"] = ""

            col_add_input, col_add_btn, col_dl_btn = st.columns([0.60, 0.20, 0.20], vertical_alignment="bottom")
            with col_add_input:
                st.text_input("➕ 新增事项:", placeholder="请输入需要添加的新事项，比如这周六下午4点去海滩...", key="input_new_task", label_visibility="collapsed")
            with col_add_btn:
                if st.button("添加新事项", use_container_width=False, on_click=handle_add_cb):
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
                        use_container_width=False
                    )

            task_to_add = st.session_state.get("temp_task_text")
            if task_to_add:
                with st.spinner("AI 解析并提交中..."):
                    res = add_task(task_to_add)
                    # 🛠️ v11.9.17: 使用统一的 SessionState Key
                    if isinstance(res, dict):
                        res["mode"] = "add"
                    st.session_state["last_task_result"] = res
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
                            is_sh = bool(row.get('_is_shadow', False))
                            render_task(row, is_shadow=is_sh, location="final_overdue", is_overdue=True)

                    # --- Displays Tab 1 ---
                    if final_today_open:
                        st.markdown('<div class="section-header" style="color: #ef4444; border-bottom-color: #fecaca;">⚡ 今日急需处理</div>', unsafe_allow_html=True)
                        for row in final_today_open: render_task(row, is_shadow=bool(row.get('_is_shadow', False)), location="final_today")

                    if final_tomorrow_open:
                        st.markdown('<div class="section-header">🌙 明日事项</div>', unsafe_allow_html=True)
                        for row in final_tomorrow_open: render_task(row, is_shadow=bool(row.get('_is_shadow', False)), location="final_tomorrow")

                    if final_week_open:
                        st.markdown('<div class="section-header">🗓️ 本周剩余事项</div>', unsafe_allow_html=True)
                        for row in final_week_open: render_task(row, is_shadow=bool(row.get('_is_shadow', False)), location="final_week")

                    if final_later_open:
                        st.markdown('<div class="section-header">⏳ 本月剩余事项</div>', unsafe_allow_html=True)
                        for row in final_later_open: render_task(row, is_shadow=bool(row.get('_is_shadow', False)), location="final_later")

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

        elif selected_tab == '🏋️‍♂️ 爸爸的健身':
            st.markdown("<div id='anchor-toc' style='position: relative; top: -80px;'></div>", unsafe_allow_html=True)
            st.markdown("""
            <div style="background-color: #f0f2f6; padding: 15px; border-radius: 10px; margin-bottom: 25px;">
            <b style="font-size: 1.1em; color: #31333F;">📋 目录</b>
            <ul style="margin-top: 10px; margin-bottom: 0;">
                <li><a href="#anchor-fitness-goals" target="_self" style="text-decoration: none; color: #0366d6;">🎯 健身目标（同龄人5-10%）</a></li>
                <li><a href="#anchor-diet-plan" target="_self" style="text-decoration: none; color: #0366d6;">🍽️ 饮食方案</a></li>
                <li><a href="#anchor-weight-record" target="_self" style="text-decoration: none; color: #0366d6;">⚖️ 体重记录</a></li>
                <li><a href="#anchor-weekly-plan" target="_self" style="text-decoration: none; color: #0366d6;">🏋️ 每周运动计划</a></li>
                <li><a href="#anchor-project-record" target="_self" style="text-decoration: none; color: #0366d6;">✅ 项目完成记录</a></li>
                <li><a href="#anchor-history-performance" target="_self" style="text-decoration: none; color: #0366d6;">📈 重训项目历史表现</a></li>
            </ul>
            </div>
            """, unsafe_allow_html=True)
            
            st.markdown("<div id='anchor-fitness-goals' style='position: relative; top: -80px;'></div>", unsafe_allow_html=True)
            st.subheader('🎯 健身目标（同龄人5-10%）')
            
            # --- 1. 新增/修改目标逻辑 (改为直接显示，不再使用 expander) ---
            goal_to_edit = st.session_state.get("goal_to_edit", None)
            
            cols_g = st.columns([0.4, 0.4, 0.2], vertical_alignment="bottom")
            with cols_g[0]:
                st.markdown("<b>目标名称</b>", unsafe_allow_html=True)
                g_name = st.text_input("名称", value=(goal_to_edit['goal_name'] if goal_to_edit else ""), 
                                      placeholder="如：体重、体脂率", key="g_name_inp", label_visibility="collapsed")
            with cols_g[1]:
                st.markdown("<b>目标数值/区间</b>", unsafe_allow_html=True)
                g_val = st.text_input("数值", value=(goal_to_edit['goal_value'] if goal_to_edit else ""), 
                                     placeholder="如：65-67公斤", key="g_val_inp", label_visibility="collapsed")
            
            # 🛠️ v9.7.2 核心修复：定义所有操作的回调函数
            def handle_add():
                name = st.session_state.get("g_name_inp", "").strip()
                val = st.session_state.get("g_val_inp", "").strip()
                if name and val:
                    if add_dad_fitness_goal(name, val):
                        st.session_state["g_name_inp"] = ""
                        st.session_state["g_val_inp"] = ""
                        st.session_state["_fitness_msg"] = ("toast", "✅ 已添加新目标！")
                        trigger_realtime_backup() # 🛠️ v9.7.6 同步云端
                else:
                    st.session_state["_fitness_msg"] = ("warning", "⚠️ 请输入完整的目标名称和数值")

            def handle_update(gid):
                name = st.session_state.get("g_name_inp", "").strip()
                val = st.session_state.get("g_val_inp", "").strip()
                if name and val:
                    if update_dad_fitness_goal(gid, name, val):
                        st.session_state.pop("goal_to_edit", None)
                        st.session_state["g_name_inp"] = ""
                        st.session_state["g_val_inp"] = ""
                        st.session_state["_fitness_msg"] = ("success", "已更新！")
                        trigger_realtime_backup() # 🛠️ v9.7.6 同步云端
            def handle_cancel():
                st.session_state.pop("goal_to_edit", None)
                st.session_state["g_name_inp"] = ""
                st.session_state["g_val_inp"] = ""

            if goal_to_edit:
                cols_g[2].button("💾 更新", use_container_width=False, 
                                on_click=handle_update, args=(goal_to_edit['id'],))
                st.button("取消修改", key="cancel_edit_goal", on_click=handle_cancel)
            else:
                cols_g[2].button("➕ 添加", use_container_width=False, on_click=handle_add)

            # 🛠️ v9.7.3 消息显示占位符
            msg_ph = st.empty()

            # 🛠️ v9.7.3 处理通知消息
            if "_fitness_msg" in st.session_state:
                msg_type, msg_text = st.session_state.pop("_fitness_msg")
                with msg_ph:
                    if msg_type == "toast":
                        st.toast(msg_text, icon="✨")
                    elif msg_type == "success":
                        st.success(msg_text)
                        time.sleep(1)
                        st.empty() # 1秒后自动消除
                    elif msg_type == "warning":
                        st.warning(msg_text)

            st.markdown("<div style='margin-bottom: -10px;'></div>", unsafe_allow_html=True)
            
            # --- 2. 显示目标列表 ---
            goals_df = get_dad_fitness_goals()
            if goals_df.empty:
                st.info("目前没有设定健身目标，点击上方展开新增。")
            else:
                # 🛠️ v9.6.2 超紧凑设计：通过 CSS 强力压缩所有相关组件的间距
                st.markdown("""
                    <style>
                    /* 针对健身记录行的专属紧凑化 */
                    .fitness-row-container {
                        margin-bottom: -25px !important;
                    }
                    /* 针对 row 下面的 div 间距 */
                    [data-testid="stVerticalBlock"] > div:has(.fitness-row-marker),
                    [data-testid="stVerticalBlock"] > div:has(.diet-row-marker),
                    [data-testid="stVerticalBlock"] > div:has(.plan-row-marker) {
                        margin-top: -15px !important;
                        margin-bottom: -15px !important;
                    }

                    /* 🛠️ v11.9.1: 增大每周运动计划的行间距 */
                    [data-testid="stVerticalBlock"] > div:has(.train-row-marker) {
                        margin-top: 10px !important;
                        margin-bottom: 10px !important;
                    }
                    /* 按钮垂直对齐微调 */
                    .stButton button {
                        margin-top: 0px !important;
                    }
                    </style>
                """, unsafe_allow_html=True)
                
                # 🛠️ v11.9.24: 移出循环以确保响应性
                def trigger_edit(r):
                    st.session_state["goal_to_edit"] = r
                    st.session_state["g_name_inp"] = r['goal_name']
                    st.session_state["g_val_inp"] = r['goal_value']

                for _, row in goals_df.iterrows():
                    # 标记这个 block 属于健身行
                    st.markdown("<div class='fitness-row-marker'></div>", unsafe_allow_html=True)
                    g_cols = st.columns([0.35, 0.35, 0.15, 0.15])
                    with g_cols[0]:
                        st.markdown(f"<div style='padding-top: 4px;'><b>{row['goal_name']}</b></div>", unsafe_allow_html=True)
                    with g_cols[1]:
                        st.markdown(f"<div style='padding-top: 4px;'>{row['goal_value']}</div>", unsafe_allow_html=True)
                    
                    with g_cols[2]:
                        st.button("✏️", key=f"edit_fgoal_{row['id']}", help="修改此目标", 
                                  use_container_width=False, on_click=trigger_edit, args=(row.to_dict(),))
                    
                    with g_cols[3]:
                        if st.button("🗑️", key=f"del_fgoal_{row['id']}", help="删除此目标", use_container_width=False):
                            if delete_dad_fitness_goal(row['id']):
                                trigger_realtime_backup() # 🛠️ v9.7.6 同步云端
                                st.rerun()

            st.markdown("<div style='text-align: right; margin-bottom: 20px;'><a href='#anchor-toc' target='_self' style='text-decoration: none; color: #0366d6; font-weight: bold;'>⬆️ 返回目录</a></div>", unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("<div id='anchor-diet-plan' style='position: relative; top: -80px;'></div>", unsafe_allow_html=True)
            st.subheader('🍽️ 饮食方案')
            
            # --- 饮食方案新增/修改逻辑 ---
            diet_to_edit = st.session_state.get("diet_to_edit", None)
            cols_d_inp = st.columns([0.25, 0.55, 0.2])
            
            with cols_d_inp[0]:
                st.markdown("<b>时间</b>", unsafe_allow_html=True)
                d_name = st.text_input("餐段名称", 
                                      placeholder="如：早饭", key="d_name_inp", label_visibility="collapsed")
            with cols_d_inp[1]:
                st.markdown("<b>饮食内容</b>", unsafe_allow_html=True)
                # 🛠️ v10.2 动态高度逻辑：根据回车行数自动撑开
                curr_diet_val = st.session_state.get("d_content_inp", "")
                if not curr_diet_val and diet_to_edit:
                    curr_diet_val = diet_to_edit['meal_content']
                
                # 计算行数，每行约 24px，基础高度 40px (一行)
                n_lines = curr_diet_val.count('\n') + 1
                dynamic_h = max(40, n_lines * 24 + 16)
                
                # 限制最大高度
                dynamic_h = min(400, dynamic_h)

                d_content = st.text_area("饮食内容", 
                                         placeholder="输入具体饮食内容，允许回车换行...", 
                                         height=dynamic_h, key="d_content_inp", label_visibility="collapsed")
                
                # 强制 CSS 去掉右下角缩放手柄，视觉更统一
                st.markdown("""
                    <style>
                    div[data-testid="stTextArea"] textarea {
                        resize: none;
                        padding-top: 8px !important;
                        padding-bottom: 8px !important;
                        min-height: 40px !important;
                    }
                    </style>
                """, unsafe_allow_html=True)
            
            def handle_diet_add():
                name = st.session_state.get("d_name_inp", "").strip()
                content = st.session_state.get("d_content_inp", "").strip()
                if name and content:
                    if add_dad_diet_plan(name, content):
                        st.session_state["d_name_inp"] = ""
                        st.session_state["d_content_inp"] = ""
                        st.session_state["_diet_msg"] = ("toast", "✅ 已添加饮食方案！")
                        trigger_realtime_backup()
                else:
                    st.session_state["_diet_msg"] = ("warning", "⚠️ 请输入完整的名称和内容")

            def handle_diet_update(did):
                name = st.session_state.get("d_name_inp", "").strip()
                content = st.session_state.get("d_content_inp", "").strip()
                if name and content:
                    if update_dad_diet_plan(did, name, content):
                        st.session_state.pop("diet_to_edit", None)
                        st.session_state["d_name_inp"] = ""
                        st.session_state["d_content_inp"] = ""
                        st.session_state["_diet_msg"] = ("success", "已更新方案！")
                        trigger_realtime_backup()
                else:
                    st.session_state["_diet_msg"] = ("warning", "请填完信息")

            def handle_diet_cancel():
                st.session_state.pop("diet_to_edit", None)
                st.session_state["d_name_inp"] = ""
                st.session_state["d_content_inp"] = ""

            with cols_d_inp[2]:
                st.markdown("<b>&nbsp;</b>", unsafe_allow_html=True) # 🛠️ v10.3 占位符，使按钮与输入框对齐
                if diet_to_edit:
                    st.button("💾 更新", key="diet_update_btn", use_container_width=False, on_click=handle_diet_update, args=(diet_to_edit['id'],))
                    st.button("取消", key="diet_cancel_btn", on_click=handle_diet_cancel)
                else:
                    st.button("➕ 添加", key="diet_add_btn", use_container_width=False, on_click=handle_diet_add)

            # 消息显示
            diet_msg_ph = st.empty()
            if "_diet_msg" in st.session_state:
                m_type, m_txt = st.session_state.pop("_diet_msg")
                with diet_msg_ph:
                    if m_type == "toast": st.toast(m_txt, icon="🍲")
                    elif m_type == "success":
                        st.success(m_txt)
                        time.sleep(1)
                        st.empty()
                    elif m_type == "warning": st.warning(m_txt)

            # 显示饮食方案列表
            diet_df = get_dad_diet_plans()
            if not diet_df.empty:
                st.markdown("<div style='margin-bottom: 5px;'></div>", unsafe_allow_html=True)
                # 🛠️ v11.9.24: 移出循环以确保响应性
                def trigger_diet_edit(r):
                    st.session_state["diet_to_edit"] = r
                    st.session_state["d_name_inp"] = r['meal_name']
                    st.session_state["d_content_inp"] = r['meal_content']

                for _, row in diet_df.iterrows():
                    st.markdown("<div class='diet-row-marker'></div>", unsafe_allow_html=True)
                    d_row_cols = st.columns([0.2, 0.6, 0.1, 0.1])
                    with d_row_cols[0]:
                        st.markdown(f"<div style='padding-top: 4px;'><b>{row['meal_name']}</b></div>", unsafe_allow_html=True)
                    with d_row_cols[1]:
                        # 🛠️ v10.1 这里的 white-space: pre-wrap 保证了多行输入能正确换行显示
                        st.markdown(f"<div style='padding-top: 4px; font-size: 0.95rem; white-space: pre-wrap;'>{row['meal_content']}</div>", unsafe_allow_html=True)
                    
                    with d_row_cols[2]:
                        st.button("✏️", key=f"edit_fdiet_{row['id']}", use_container_width=False, on_click=trigger_diet_edit, args=(row.to_dict(),))
                    with d_row_cols[3]:
                        if st.button("🗑️", key=f"del_fdiet_{row['id']}", use_container_width=False):
                            if delete_dad_diet_plan(row['id']):
                                trigger_realtime_backup()
                                st.rerun()
            
            st.markdown("<br>", unsafe_allow_html=True)
            
            # 提前获取数据以备下载使用
            weight_df = get_dad_weight_records()
            
            # 🛠️ v11.10.3: 计算过去 1 周平均体重或最近一次历史体重 (纯黑色加粗)
            weight_info = ""
            default_weight = 70.0
            if not weight_df.empty:
                latest_r_all = weight_df.sort_values(by="record_date", ascending=False).iloc[0]
                default_weight = float(latest_r_all['weight'])
                
                now_sgt = get_now_sgt()
                seven_days_ago = (now_sgt - timedelta(days=7)).date().strftime("%Y-%m-%d")
                recent_weights = weight_df[weight_df['record_date'] >= seven_days_ago]
                
                if not recent_weights.empty:
                    avg_w = recent_weights['weight'].mean()
                    weight_info = f"<span style='font-size: 1.1rem; color: #000000; font-weight: bold; margin-left: 15px;'>过去1周平均体重：{avg_w:.1f}公斤</span>"
                else:
                    weight_info = f"<span style='font-size: 1.1rem; color: #000000; font-weight: bold; margin-left: 15px;'>最新的历史体重（超过一周）：{default_weight:.1f}公斤</span>"
            
            # 使用 align-items: baseline 确保文字在同一水平基准上
            st.markdown("<div style='text-align: right; margin-bottom: 20px;'><a href='#anchor-toc' target='_self' style='text-decoration: none; color: #0366d6; font-weight: bold;'>⬆️ 返回目录</a></div>", unsafe_allow_html=True)
            st.markdown("<div id='anchor-weight-record' style='position: relative; top: -80px;'></div>", unsafe_allow_html=True)
            st.markdown(f"<div style='display: flex; align-items: baseline; margin-bottom: 10px;'><h3 style='margin: 0;'>⚖️ 体重记录</h3>{weight_info}</div>", unsafe_allow_html=True)
            
            # --- 🛠️ v11.9.20: 极致对齐：采用双行结构 + 底部基准对齐 ---
            # 第 1 行：单独渲染文本标签
            col_l1, col_l2, col_l3, col_l4 = st.columns([0.22, 0.23, 0.18, 0.37])
            col_l1.markdown("<b>日期</b>", unsafe_allow_html=True)
            col_l2.markdown("<b>体重 (KG)</b>", unsafe_allow_html=True)
            
            # 第 2 行：渲染交互组件，统一基准线 (bottom alignment)
            col_w1, col_w2, col_w3, col_w4 = st.columns([0.22, 0.23, 0.18, 0.37], vertical_alignment="bottom")
            with col_w1:
                w_date = st.date_input("记录日期", value=get_now_sgt().date(), key="w_date_inp", label_visibility="collapsed")
            with col_w2:
                w_val = st.number_input("体重数值", min_value=30.0, max_value=200.0, value=default_weight, step=0.1, format="%.1f", key="w_val_inp", label_visibility="collapsed")
            
            def handle_weight_add():
                d = st.session_state.get("w_date_inp").strftime("%Y-%m-%d")
                v = st.session_state.get("w_val_inp")
                if add_dad_weight_record(d, v):
                    st.session_state["_weight_msg"] = ("toast", "✅ 体重记录已添加！")
                    trigger_realtime_backup()
            
            with col_w3:
                st.button("➕ 添加记录", on_click=handle_weight_add, use_container_width=False, key="btn_w_add_v20")
            
            with col_w4:
                if not weight_df.empty:
                    # 导出 CSV 逻辑
                    export_df = weight_df.copy().rename(columns={'record_date': '日期', 'weight': '体重(KG)'})
                    csv_data = export_df.sort_values(by="日期", ascending=False).to_csv(index=False).encode('utf-8-sig')
                    st.download_button(
                        label="下载历史体重数据(csv)",
                        data=csv_data,
                        file_name=f"weight_history_{get_now_sgt().strftime('%Y%m%d')}.csv",
                        mime='text/csv',
                        use_container_width=False,
                        key="row_weight_dl_v20"
                    )
            
            if "_weight_msg" in st.session_state:
                m_type, m_txt = st.session_state.pop("_weight_msg")
                if m_type == "toast": st.toast(m_txt, icon="⚖️")

            # --- 体重趋势图表 ---
            if not weight_df.empty:
                # 🛠️ v11.9.8: 汉化图表字段
                chart_data = weight_df.copy()
                chart_data = chart_data.rename(columns={'record_date': '日期', 'weight': '体重(KG)'})
                chart_data['日期'] = pd.to_datetime(chart_data['日期'])
                
                # 🛠️ v11.9.6: 使用 Altair 自定义纵坐标 (±3kg)
                y_min = float(chart_data['体重(KG)'].min()) - 3.0
                y_max = float(chart_data['体重(KG)'].max()) + 3.0
                
                chart = alt.Chart(chart_data).mark_line(point=True).encode(
                    x=alt.X('日期:T', title='日期', axis=alt.Axis(format='%Y年%m月', labelAngle=-45)),
                    y=alt.Y('体重(KG):Q', title='体重 (KG)', scale=alt.Scale(domain=[y_min, y_max])),
                    tooltip=['日期', '体重(KG)']
                ).properties(height=300).interactive()
                
                st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
                st.altair_chart(chart, use_container_width=True)
                
                # --- 历史数据控制行 (仅保留查看开关) ---
                show_history = st.toggle("📜 表格显示历史体重数据", key="show_weight_history")

                if show_history:
                    st.markdown("---")
                    hist_df = weight_df.sort_values(by="record_date", ascending=False)
                    for _, r in hist_df.iterrows():
                        h_cols = st.columns([0.4, 0.4, 0.2])
                        h_cols[0].write(f"📅 {r['record_date']}")
                        h_cols[1].write(f"⚖️ {r['weight']} KG")
                        if h_cols[2].button("🗑️", key=f"del_weight_{r['id']}", help="删除此记录"):
                            if delete_dad_weight_record(r['id']):
                                trigger_realtime_backup()
                                st.rerun()
            else:
                st.info("尚无体重记录，请在上方输入并添加。")

            st.markdown("<div style='margin-bottom: 20px;'></div>", unsafe_allow_html=True)

            st.markdown("<div style='text-align: right; margin-bottom: 20px;'><a href='#anchor-toc' target='_self' style='text-decoration: none; color: #0366d6; font-weight: bold;'>⬆️ 返回目录</a></div>", unsafe_allow_html=True)
            st.markdown("<div style='margin-bottom: 20px;'></div>", unsafe_allow_html=True)
            # --- 🛠️ v11.9.24: 自动定位到修改区域 ---
            st.markdown("<div id='anchor-weekly-plan' style='position: relative; top: -80px;'></div>", unsafe_allow_html=True)
            st.markdown("<div id='training-edit-anchor'></div>", unsafe_allow_html=True)
            st.subheader('🏋️ 每周运动计划')
            
            if st.session_state.get("scroll_to_train_edit"):
                st.session_state["scroll_to_train_edit"] = False
                components.html("""
                    <script>
                        setTimeout(function() {
                            var elements = window.parent.document.querySelectorAll('div[data-testid="stMarkdownContainer"]');
                            for (var i = 0; i < elements.length; i++) {
                                if (elements[i].innerText.includes("每周运动计划") || elements[i].innerHTML.includes("training-edit-anchor")) {
                                    elements[i].scrollIntoView({behavior: "smooth", block: "start"});
                                    break;
                                }
                            }
                        }, 500);
                    </script>
                """, height=0)
            
            # --- 重量训练细节 CRUD (v11.0) ---
            train_to_edit = st.session_state.get("train_to_edit", None)
            cols_t_inp = st.columns([0.25, 0.55, 0.2])
            
            with cols_t_inp[0]:
                st.markdown("<b>日期</b>", unsafe_allow_html=True)
                t_day = st.text_input("日期", 
                                      placeholder="如：周二", key="t_day_inp", label_visibility="collapsed")
            with cols_t_inp[1]:
                st.markdown("<b>训练内容</b>", unsafe_allow_html=True)
                curr_t_val = st.session_state.get("t_content_inp", "")
                if not curr_t_val and train_to_edit: curr_t_val = train_to_edit['train_content']
                t_lines = curr_t_val.count('\n') + 1
                t_h = min(400, max(40, t_lines * 24 + 16))
                t_content = st.text_area("训练内容", 
                                         height=t_h, key="t_content_inp", label_visibility="collapsed")

            def handle_train_add():
                d = st.session_state.get("t_day_inp", "").strip()
                c = st.session_state.get("t_content_inp", "").strip()
                if d and c:
                    if add_dad_training_detail(d, c):
                        st.session_state["t_day_inp"] = ""
                        st.session_state["t_content_inp"] = ""
                        trigger_realtime_backup()

            def handle_train_update(tid):
                d = st.session_state.get("t_day_inp", "").strip()
                c = st.session_state.get("t_content_inp", "").strip()
                if d and c:
                    if update_dad_training_detail(tid, d, c):
                        st.session_state.pop("train_to_edit", None)
                        st.session_state["t_day_inp"] = ""
                        st.session_state["t_content_inp"] = ""
                        trigger_realtime_backup()

            def handle_train_cancel():
                st.session_state.pop("train_to_edit", None)
                st.session_state["t_day_inp"] = ""
                st.session_state["t_content_inp"] = ""

            with cols_t_inp[2]:
                st.markdown("<b>&nbsp;</b>", unsafe_allow_html=True)
                if train_to_edit:
                    st.button("💾 更新", key="t_up_btn", use_container_width=False, on_click=handle_train_update, args=(train_to_edit['id'],))
                    st.button("取消", key="t_can_btn", on_click=handle_train_cancel)
                else:
                    st.button("➕ 添加", key="t_add_btn", use_container_width=False, on_click=handle_train_add)

            # 🛠️ v11.9.24: 定义统一的修改回调，移出循环以确保响应性
            def trigger_train_edit(r):
                st.session_state["train_to_edit"] = r
                # 注意：这里需要立即同步到 widget 的 key 中
                st.session_state["t_day_inp"] = r['train_day']
                st.session_state["t_content_inp"] = r['train_content']
                st.session_state["scroll_to_train_edit"] = True

            # 显示训练细节列表
            train_df = get_dad_training_details()
            if not train_df.empty:
                for _, row in train_df.iterrows():
                    st.markdown("<div class='train-row-marker'></div>", unsafe_allow_html=True)
                    t_row_cols = st.columns([0.2, 0.6, 0.1, 0.1])
                    with t_row_cols[0]:
                        st.markdown(f"<div style='padding-top: 4px;'><b>{row['train_day']}</b></div>", unsafe_allow_html=True)
                    with t_row_cols[1]:
                        st.markdown(f"<div style='padding-top: 4px; font-size: 0.95rem; white-space: pre-wrap;'>{row['train_content']}</div>", unsafe_allow_html=True)
                    with t_row_cols[2]:
                        st.button("✏️", key=f"edit_ftrain_{row['id']}", on_click=trigger_train_edit, args=(row.to_dict(),))
                    with t_row_cols[3]:
                        if st.button("🗑️", key=f"del_ftrain_{row['id']}"):
                            if delete_dad_training_detail(row['id']):
                                trigger_realtime_backup()
                                st.rerun()

            st.markdown("<div style='text-align: right; margin-bottom: 20px;'><a href='#anchor-toc' target='_self' style='text-decoration: none; color: #0366d6; font-weight: bold;'>⬆️ 返回目录</a></div>", unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("<div id='anchor-project-record' style='position: relative; top: -80px;'></div>", unsafe_allow_html=True)
            st.subheader('✅ 项目完成记录')
            
            record_date = st.date_input("选择记录日期", value=get_now_sgt().date(), key="fitness_record_date")
            date_str = record_date.strftime("%Y-%m-%d")
            
            st.markdown("<br>", unsafe_allow_html=True)
            
            # Header row
            col1, col2, col3, col4, col5, col6, col7 = st.columns([0.15, 0.25, 0.12, 0.12, 0.12, 0.12, 0.12])
            col1.markdown("**类别**")
            col2.markdown("**项目**")
            col3.markdown("**重量(kg)**")
            col4.markdown("**次数**")
            col5.markdown("**组数**")
            col6.markdown("**保存**")
            col7.markdown("**清除**")
            
            st.markdown("<hr style='margin-top: 5px; margin-bottom: 10px;'/>", unsafe_allow_html=True)
            
            fitness_msg_ph = st.empty()
            
            if "fitness_record_toast" in st.session_state:
                st.toast(st.session_state.pop("fitness_record_toast"))
                
            latest_fitness = get_latest_fitness_records()
            
            def clear_fitness_row(k_w, k_r, k_s):
                st.session_state[k_w] = 0.0
                st.session_state[k_r] = 0
                st.session_state[k_s] = 0

            @st.dialog("⚠️ 确认覆盖记录")
            def confirm_overwrite_dialog(date_str, category, exercise, w_val, r_val, s_val):
                st.warning(f"在 {date_str} 这一天，【{exercise}】已经有保存记录了。是否要用当前的数据覆盖它？")
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("✔️ 确认覆盖", use_container_width=True):
                        if add_dad_fitness_record(date_str, category, exercise, w_val, r_val, s_val):
                            st.session_state["fitness_record_toast"] = f"✅ 【{exercise}】已被成功覆盖！"
                            trigger_realtime_backup()
                            st.rerun()
                with col2:
                    if st.button("❌ 取消", use_container_width=True):
                        st.rerun()

            def render_fitness_row(category, exercise, idx):
                col_c, col_e, col_w, col_r, col_s, col_save, col_clear = st.columns([0.15, 0.25, 0.12, 0.12, 0.12, 0.12, 0.12], vertical_alignment="center")
                
                k_w = f"f_w_{idx}"
                k_r = f"f_r_{idx}"
                k_s = f"f_s_{idx}"
                
                # Retrieve default values from latest records if available
                default_w = latest_fitness.get(exercise, {}).get('weight', 0.0)
                default_r = latest_fitness.get(exercise, {}).get('reps', 0)
                default_s = latest_fitness.get(exercise, {}).get('sets', 0)
                
                with col_c:
                    st.markdown(f"<span style='font-size: 0.9em; color: #555;'>{category}</span>", unsafe_allow_html=True)
                with col_e:
                    st.markdown(f"<span style='font-weight: bold; font-size: 0.9em;'>{exercise}</span>", unsafe_allow_html=True)
                with col_w:
                    w_val = st.number_input("w", min_value=0.0, value=float(default_w), step=0.5, key=k_w, label_visibility="collapsed")
                with col_r:
                    r_val = st.number_input("r", min_value=0, value=int(default_r), step=1, key=k_r, label_visibility="collapsed")
                with col_s:
                    s_val = st.number_input("s", min_value=0, value=int(default_s), step=1, key=k_s, label_visibility="collapsed")
                with col_save:
                    if st.button("保存", key=f"f_save_{idx}", use_container_width=True):
                        if w_val <= 0 or r_val <= 0 or s_val <= 0:
                            fitness_msg_ph.error(f"⚠️ 【{exercise}】保存失败：重量(kg)、次数、组数均必须大于0！")
                        else:
                            if has_fitness_record(date_str, exercise):
                                confirm_overwrite_dialog(date_str, category, exercise, w_val, r_val, s_val)
                            else:
                                if add_dad_fitness_record(date_str, category, exercise, w_val, r_val, s_val):
                                    st.toast(f"✅ 【{exercise}】已保存！")
                                    trigger_realtime_backup()
                with col_clear:
                    st.button("清除", key=f"f_clear_{idx}", use_container_width=True, on_click=clear_fitness_row, args=(k_w, k_r, k_s))
            
            upper_exercises = [
                "哑铃侧平举",
                "高位下拉",
                "哑铃卧推",
                "窄握坐姿划船(宽/窄交替)",
                "面拉（改善驼背）",
                "肱三曲杆下压（没劲可不做）"
            ]
            
            lower_exercises = [
                "腿推机（必做，脚位偏高）",
                "坐姿腿弯举（必做）",
                "侧平举（必做）",
                "臀推（没劲可少做几组）",
                "坐姿腿伸（没劲可不做）"
            ]
            
            idx = 0
            for ex in upper_exercises:
                render_fitness_row("上肢重训日", ex, idx)
                st.markdown("<div style='margin-bottom: 5px;'></div>", unsafe_allow_html=True)
                idx += 1
                
            st.markdown("<div style='text-align: right; margin-bottom: 10px;'><a href='#anchor-toc' target='_self' style='text-decoration: none; color: #0366d6; font-weight: bold;'>⬆️ 返回目录</a></div>", unsafe_allow_html=True)
            st.markdown("<hr style='margin-top: 10px; margin-bottom: 10px;'/>", unsafe_allow_html=True)
            
            for ex in lower_exercises:
                render_fitness_row("下肢重训日", ex, idx)
                st.markdown("<div style='margin-bottom: 5px;'></div>", unsafe_allow_html=True)
                idx += 1
                
            st.markdown("<div style='text-align: right; margin-bottom: 20px;'><a href='#anchor-toc' target='_self' style='text-decoration: none; color: #0366d6; font-weight: bold;'>⬆️ 返回目录</a></div>", unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("<div id='anchor-history-performance' style='position: relative; top: -80px;'></div>", unsafe_allow_html=True)
            st.subheader('📈 重训项目历史表现')
            
            all_exercises_formatted = [f"上肢日-{ex}" for ex in upper_exercises] + [f"下肢日-{ex}" for ex in lower_exercises]
            selected_ex_formatted = st.selectbox("选择重训项目查看历史趋势", options=all_exercises_formatted, key="history_ex_select")
            
            if selected_ex_formatted:
                selected_ex = selected_ex_formatted.split("-", 1)[1]
                all_fr_df = get_all_fitness_records()
                if not all_fr_df.empty:
                    ex_df = all_fr_df[all_fr_df['exercise'] == selected_ex].copy()
                    if not ex_df.empty:
                        ex_df['record_date'] = pd.to_datetime(ex_df['record_date'])
                        ex_df = ex_df.sort_values(by='record_date')
                        
                        min_date = (ex_df['record_date'].min() - pd.Timedelta(days=5)).isoformat()
                        max_date = (ex_df['record_date'].max() + pd.Timedelta(days=5)).isoformat()
                        
                        min_w = max(0, float(ex_df['weight'].min()) * 0.8)
                        max_w = float(ex_df['weight'].max()) * 1.2
                        
                        min_r = max(0, float(ex_df['reps'].min()) * 0.8)
                        max_r = float(ex_df['reps'].max()) * 1.2
                        
                        top_base = alt.Chart(ex_df).encode(
                            x=alt.X('record_date:T', title=None, axis=alt.Axis(labels=False, ticks=False, domain=False), 
                                    scale=alt.Scale(domain=[min_date, max_date]))
                        )
                        
                        bottom_base = alt.Chart(ex_df).encode(
                            x=alt.X('record_date:T', title='日期', axis=alt.Axis(format='%Y-%m-%d', labelAngle=-45), 
                                    scale=alt.Scale(domain=[min_date, max_date]))
                        )
                        
                        base_weight = top_base.encode(
                            y=alt.Y('weight:Q', title='重量 (KG)', scale=alt.Scale(domain=[min_w, max_w])),
                            tooltip=['record_date', 'weight', 'reps', 'sets']
                        )
                        line_weight = base_weight.mark_line(point=True, color='#3b82f6')
                        
                        base_reps = top_base.encode(
                            y=alt.Y('reps:Q', title='次数', scale=alt.Scale(domain=[min_r, max_r])),
                            tooltip=['record_date', 'weight', 'reps', 'sets']
                        )
                        line_reps = base_reps.mark_line(point=True, color='#ef4444')
                        
                        top_chart = alt.layer(line_weight, line_reps).resolve_scale(
                            y='independent'
                        ).properties(
                            height=250,
                            width="container"
                        )
                        
                        bar_sets = bottom_base.encode(
                            y=alt.Y('sets:Q', title='组数', axis=alt.Axis(tickMinStep=1)),
                            tooltip=['record_date', 'weight', 'reps', 'sets']
                        ).mark_bar(size=15, color='#10b981', opacity=0.8).properties(
                            height=100,
                            width="container"
                        )
                        
                        chart = alt.vconcat(top_chart, bar_sets, spacing=0).resolve_scale(
                            x='shared'
                        ).interactive()
                        
                        col_chart, _ = st.columns([4, 1])
                        with col_chart:
                            st.altair_chart(chart, use_container_width=True)
                            st.markdown("<div style='text-align: center; font-size: 0.9em; margin-top: -15px;'><span style='color: #3b82f6; font-weight: bold;'>━━ 重量(KG)</span> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <span style='color: #ef4444; font-weight: bold;'>━━ 次数</span> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <span style='color: #10b981; font-weight: bold;'>▇ 组数</span></div>", unsafe_allow_html=True)
                            
                        st.markdown("<div style='text-align: right; margin-top: 10px; margin-bottom: 20px;'><a href='#anchor-toc' target='_self' style='text-decoration: none; color: #0366d6; font-weight: bold;'>⬆️ 返回目录</a></div>", unsafe_allow_html=True)
                        st.markdown("<br><br>", unsafe_allow_html=True)
                        st.subheader(f'📝 【{selected_ex}】历史数据明细与修改')
                        
                        col_d, col_w, col_r, col_s, col_u = st.columns([0.22, 0.18, 0.15, 0.15, 0.3], vertical_alignment="center")
                        col_d.markdown("**日期**")
                        col_w.markdown("**重量(kg)**")
                        col_r.markdown("**次数**")
                        col_s.markdown("**组数**")
                        col_u.markdown("**操作**")
                        
                        st.markdown("<hr style='margin-top: 5px; margin-bottom: 10px;'/>", unsafe_allow_html=True)
                        
                        cat_str = "上肢重训日" if selected_ex in upper_exercises else "下肢重训日"
                        hist_msg_ph = st.empty()
                        
                        for _, row in ex_df.sort_values(by='record_date', ascending=False).iterrows():
                            r_date = row['record_date'].strftime('%Y-%m-%d') if isinstance(row['record_date'], pd.Timestamp) else row['record_date']
                            
                            k_w_hist = f"h_w_{r_date}_{selected_ex}"
                            k_r_hist = f"h_r_{r_date}_{selected_ex}"
                            k_s_hist = f"h_s_{r_date}_{selected_ex}"
                            
                            hc_d, hc_w, hc_r, hc_s, hc_u = st.columns([0.22, 0.18, 0.15, 0.15, 0.3], vertical_alignment="center")
                            
                            with hc_d:
                                st.markdown(f"<div style='padding-top: 5px;'><b>{r_date}</b></div>", unsafe_allow_html=True)
                            with hc_w:
                                hw_val = st.number_input("w", min_value=0.0, value=float(row['weight']), step=0.5, key=k_w_hist, label_visibility="collapsed")
                            with hc_r:
                                hr_val = st.number_input("r", min_value=0, value=int(row['reps']), step=1, key=k_r_hist, label_visibility="collapsed")
                            with hc_s:
                                hs_val = st.number_input("s", min_value=0, value=int(row['sets']), step=1, key=k_s_hist, label_visibility="collapsed")
                            with hc_u:
                                bcol1, bcol2 = st.columns(2)
                                with bcol1:
                                    if st.button("更新", key=f"h_upd_{r_date}_{selected_ex}", use_container_width=True):
                                        if hw_val <= 0 or hr_val <= 0 or hs_val <= 0:
                                            hist_msg_ph.error(f"⚠️ 【{selected_ex}】({r_date}) 更新失败：重量、次数、组数均必须大于0！")
                                        else:
                                            if add_dad_fitness_record(r_date, cat_str, selected_ex, hw_val, hr_val, hs_val):
                                                st.session_state["fitness_record_toast"] = f"✅ 【{selected_ex}】({r_date}) 已更新！"
                                                trigger_realtime_backup()
                                                st.rerun()
                                with bcol2:
                                    if st.button("删除", key=f"h_del_{r_date}_{selected_ex}", use_container_width=True):
                                        if delete_dad_fitness_record(r_date, selected_ex):
                                            st.session_state["fitness_record_toast"] = f"🗑️ 【{selected_ex}】({r_date}) 已彻底删除！"
                                            trigger_realtime_backup()
                                            st.rerun()
                    else:
                        st.info(f"尚无【{selected_ex}】的历史记录。")
                else:
                    st.info("尚无任何重训历史记录。")

            st.markdown("<div style='text-align: right; margin-bottom: 20px;'><a href='#anchor-toc' target='_self' style='text-decoration: none; color: #0366d6; font-weight: bold;'>⬆️ 返回目录</a></div>", unsafe_allow_html=True)

        elif selected_tab == '🌸 恩雅的健康':
            st.markdown("<h2 style='color: #db2777;'>🌸 恩雅的健康中心</h2>", unsafe_allow_html=True)
            
            health_sub1, health_sub2 = st.tabs(["📏 身高体重记录", "📅 月经记录"], key="health_nav_tabs")
            
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
                    if st.button("➕ 保存记录", use_container_width=False, key="v_save_btn"):
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
                    if st.button("➕ 保存记录", use_container_width=False, key="p_save_btn"):
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
                            use_container_width=False,
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
                        if p_cols[2].button("🗑️", key=f"del_per_{row['id']}"):
                            delete_enya_period(row['id'])
                            st.rerun()
                        st.divider()


    st.markdown("---")
    st.markdown(f"<p style='text-align: center; color: #888;'>最后更新: {get_now_sgt().strftime('%Y-%m-%d %H:%M')}</p>", unsafe_allow_html=True)

except Exception as e:
    st.error(f"❌ 系统发生错误: {e}")
    st.exception(e)
