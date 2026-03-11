import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import pytz
import os
from openai import OpenAI
from dotenv import load_dotenv
import extra_streamlit_components as stx
import json
import calendar_sync

# --- 1. Streamlit UI Config (Must be FIRST) ---
st.set_page_config(
    page_title="家庭事项管理系统",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="auto"
)

# --- 2. Cookie Management ---
# Cookie 管理器初始化 (不可使用 @st.cache_resource，因为它是 UI 组件)
cookie_manager = stx.CookieManager()

# --- 3. Environment & Global Config ---
load_dotenv()
try:
    api_key = st.secrets["OPENAI_API_KEY"] if "OPENAI_API_KEY" in st.secrets else os.getenv("OPENAI_API_KEY")
except Exception:
    api_key = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=api_key) if api_key else None
SGT = pytz.timezone('Asia/Singapore')

def get_now_sgt():
    return datetime.now(SGT)

# Ensure data directory exists
if not os.path.exists("data"):
    os.makedirs("data")
DB_FILE = "data/tasks.db"

# --- 4. Database Functions ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
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
        if 'google_event_id' not in columns:
            c.execute("ALTER TABLE tasks ADD COLUMN google_event_id TEXT")
    conn.commit()
    conn.close()

def extract_date_llm(task_text):
    if not client: return task_text, None, None
    now = get_now_sgt()
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"""你是家庭AI助手。今天是 {now.strftime('%Y-%m-%d')} ({now.strftime('%A')})。
                从用户文本中解析任务，并返回特定的格式：
                1. CLEAN_TASK: 任务内容（去除里面的‘明天’、‘下周’等时间词，使显示更简洁）。
                2. DATE: 截止日期时间 'YYYY-MM-DD HH:MM'。若未提到具体时间，默认为 23:59。务必根据‘今天’的日期准确推算‘明天’、‘后天’等的具体日期。
                3. RECUR: 循环模式 (Monday, Tuesday..., Everyday, Weekend) 或 None。
                
                返回格式示例：CLEAN_TASK: 内容 | DATE: YYYY-MM-DD HH:MM | RECUR: Pattern"""},
                {"role": "user", "content": task_text}
            ],
            temperature=0
        )
        res = response.choices[0].message.content.strip()
        
        # 使用更稳健的解析方式
        parts = {p.split(':', 1)[0].strip(): p.split(':', 1)[1].strip() for p in res.split('|') if ':' in p}
        
        c_task = parts.get("CLEAN_TASK", task_text)
        dt_str = parts.get("DATE", now.strftime("%Y-%m-%d 23:59"))
        recur_str = parts.get("RECUR", "None")
        
        # 验证日期格式
        datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        return c_task, dt_str, (recur_str if recur_str != "None" else None)
    except Exception as e:
        print(f"LLM 解析错误: {e}")
        return task_text, now.strftime("%Y-%m-%d 23:59"), None

def get_tasks():
    conn = sqlite3.connect(DB_FILE)
    query = "SELECT * FROM tasks ORDER BY completed ASC, CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END, due_date ASC, created_at ASC"
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def add_task(task_text, cal_email=None, sync_enabled=False):
    clean_task, due_datetime, recur_pattern = extract_date_llm(task_text)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (task, due_date, recurring_pattern, created_at) VALUES (?, ?, ?, ?)", 
              (clean_task, due_datetime, recur_pattern, get_now_sgt().strftime("%Y-%m-%d %H:%M:%S")))
    task_id = c.lastrowid
    conn.commit()
    conn.close()

    if sync_enabled and cal_email:
        event_id = calendar_sync.upsert_calendar_event(task_id, clean_task, due_datetime, None, cal_email)
        if event_id:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE tasks SET google_event_id = ? WHERE id = ?", (event_id, task_id))
            conn.commit()
            conn.close()

def update_task_status(task_id, completed, cal_email=None, sync_enabled=False):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT task, due_date, google_event_id FROM tasks WHERE id = ?", (task_id,))
    task_data = c.fetchone()
    c.execute("UPDATE tasks SET completed = ? WHERE id = ?", (completed, task_id))
    conn.commit()
    conn.close()

    if sync_enabled and cal_email and task_data:
        g_id = task_data[2]
        if completed:
            # 如果标记为完成，则从日历删除
            calendar_sync.delete_calendar_event(g_id, cal_email)
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE tasks SET google_event_id = NULL WHERE id = ?", (task_id,))
            conn.commit()
            conn.close()
        elif not completed:
            # 如果取消勾选，则重新同步到日历
            event_id = calendar_sync.upsert_calendar_event(task_id, task_data[0], task_data[1], None, cal_email)
            if event_id:
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("UPDATE tasks SET google_event_id = ? WHERE id = ?", (event_id, task_id))
                conn.commit()
                conn.close()

def delete_task(task_id, cal_email=None, sync_enabled=False):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT google_event_id FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    g_id = row[0] if row else None
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

    if sync_enabled and cal_email and g_id:
        calendar_sync.delete_calendar_event(g_id, cal_email)

def update_task_text(task_id, new_text, cal_email=None, sync_enabled=False):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT due_date, google_event_id FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    c.execute("UPDATE tasks SET task = ? WHERE id = ?", (new_text, task_id))
    conn.commit()
    conn.close()

    if sync_enabled and cal_email and row:
        due_date, g_id = row
        new_g_id = calendar_sync.upsert_calendar_event(task_id, new_text, due_date, g_id, cal_email)
        if new_g_id and new_g_id != g_id:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE tasks SET google_event_id = ? WHERE id = ?", (new_g_id, task_id))
            conn.commit()
            conn.close()

# --- 5. UI Styling ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;700&display=swap');
    .stApp { background: linear-gradient(135deg, #fdfbfb 0%, #ebedee 100%); font-family: 'Outfit', sans-serif; }
    .main-header { color: #1e3a8a; text-align: center; font-size: 2rem !important; font-weight: 700; padding: 1rem 0; }
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
</style>
""", unsafe_allow_html=True)

# --- 6. Main App Structure ---
try:
    init_db()

    # --- 🔐 登录逻辑与持久化验证 ---
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False
    if "editing_task_id" not in st.session_state:
        st.session_state["editing_task_id"] = None

    # 1. 尝试从浏览器读取 Cookie (仅在尚未通过当前会话认证时)
    if not st.session_state["authenticated"]:
        auth_cookie = cookie_manager.get("family_system_auth")
        if auth_cookie == "authenticated":
            st.session_state["authenticated"] = True
            st.rerun()

    # 2. 如果当前未通过任何方式认证，则显示登录页面
    if not st.session_state["authenticated"]:
        st.markdown("<h2 style='text-align: center; color: #1e3a8a; margin-top: 50px;'>🏠 家庭系统登录</h2>", unsafe_allow_html=True)
        _, col_m, _ = st.columns([1, 2, 1])
        with col_m:
            pwd = st.text_input("请输入访问密码 (6位数字):", type="password", key="login_pwd")
            if pwd == "790228":
                # 【核心修复】先更新状态并下发写入指令，但不重刷页面
                st.session_state["authenticated"] = True
                cookie_manager.set("family_system_auth", "authenticated", expires_at=datetime.now() + timedelta(days=30))
                st.success("✅ 登录成功！正在为您开启系统...")
                # 此处不使用 st.stop()，让程序继续向下运行，从而渲染主界面
            elif pwd:
                st.error("🚫 密码错误")
            
            st.info("💡 提示：密码是6位数字。")
            st.warning("⚠️ 如果您是 Safari 浏览器用户：请确保已关闭‘阻止所有 Cookie’或‘阻止跨站追踪’设置，否则系统无法保持登录。")
        
        # 如果依然没通过认证（比如密码没输对），则阻断后续显示
        if not st.session_state["authenticated"]:
            st.stop()

    # --- 🛠️ 辅助 UI 函数 ---
    def hits_day(pattern, target_date):
        if not pattern: return False
        p = pattern.strip()
        if p == 'Everyday': return True
        if p == 'Weekend' and target_date.weekday() >= 5: return True
        return p == target_date.strftime('%A')

    def render_task(row, is_shadow=False, location="main"):
        key_id = f"{location}_c_{row['id']}" if not is_shadow else f"sh_{location}_{row['id']}_{row['due_date'][:10]}"
        del_id = f"{location}_d_{row['id']}"
        edit_id = f"{location}_e_{row['id']}"
        
        with st.container():
            st.markdown('<div class="task-container">', unsafe_allow_html=True)
            # Layout: checkbox, content area (text or input), action buttons
            c1, c2, c3 = st.columns([0.05, 0.75, 0.2])
            
            if not is_shadow:
                is_comp = c1.checkbox("", value=row['completed'], key=key_id)
                if is_comp != row['completed']:
                    update_task_status(row['id'], is_comp, cal_email, sync_enabled)
                    st.rerun()
            else:
                c1.markdown("🔄")
                
            # Handle inline edit
            if st.session_state.get("editing_task_id") == row['id']:
                new_text = c2.text_input("修改事项:", value=row['task'], key=f"inp_{location}_{row['id']}")
                save_col, can_col = c3.columns(2)
                if save_col.button("💾", key=f"save_{location}_{row['id']}", help="保存"):
                    update_task_text(row['id'], new_text, cal_email, sync_enabled)
                    st.session_state["editing_task_id"] = None
                    st.rerun()
                if can_col.button("🚫", key=f"can_{location}_{row['id']}", help="取消"):
                    st.session_state["editing_task_id"] = None
                    st.rerun()
            else:
                style = "todo-completed" if row['completed'] else ""
                recur_tag = f"<span class='recur-tag'>🔄 循环: {row['recurring_pattern']}</span>" if row['recurring_pattern'] else ""
                due_val = f"📅 预计: {row['due_date']}" if row['due_date'] else ""
                
                c2.markdown(f"<p class='todo-text {style}'>{row['task']}{recur_tag}</p><div class='todo-date'>{due_val}</div>", unsafe_allow_html=True)
                
                if not is_shadow:
                    edit_col, del_col = c3.columns(2)
                    if edit_col.button("✏️", key=edit_id, help="修改"):
                        st.session_state["editing_task_id"] = row['id']
                        st.rerun()
                    if del_col.button("🗑️", key=del_id, help="删除"):
                        delete_task(row['id'], cal_email, sync_enabled)
                        st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

    # Sidebar
    with st.sidebar:
        st.header("🏠 系统控制")
        
        st.markdown("### 📅 日历配置")
        cal_email = st.text_input("Google Email:", value="xuchunli@gmail.com")
        sync_enabled = st.checkbox("同步到 Google 日历", value=False)
        if sync_enabled:
            st.caption("请确保 root 目录下存在 service_account.json 或在 Secrets 中配置凭据。")

        if st.button("🔴 退出登录", use_container_width=True):
            del st.session_state["password_correct"]
            cookie_manager.delete("family_system_auth")
            st.rerun()
        st.info(f"📍 新加坡时间\n{get_now_sgt().strftime('%Y-%m-%d %H:%M')}")
        st.divider()
        new_task = st.text_input("➕ 新增事项:", placeholder="例如：每周二拿快递...")
        if st.button("立即添加", use_container_width=True):
            if new_task:
                with st.spinner("AI 解析中..."):
                    add_task(new_task, cal_email, sync_enabled)
                st.rerun()

    # Main Interface
    # --- 7. Data Preparation ---
    tasks_df = get_tasks()
    now = get_now_sgt()
    today_date = now.date()
    tomorrow_date = today_date + timedelta(days=1)
    end_of_week = today_date + timedelta(days=6 - today_date.weekday())
    
    # Initialize all lists to avoid NameErrors
    recurring_list, today_list, tomorrow_list, week_list, later_list = [], [], [], [], []
    shadow_today, shadow_tomorrow, shadow_week = [], [], []
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
                if due_dt <= today_date: today_list.append(row)
                elif due_dt == tomorrow_date: tomorrow_list.append(row)
                elif due_dt <= end_of_week: week_list.append(row)
                else: later_list.append(row)
            except: today_list.append(row)

        for item in recurring_list:
            if hits_day(item['recurring_pattern'], today_date): shadow_today.append(item)
            if hits_day(item['recurring_pattern'], tomorrow_date): shadow_tomorrow.append(item)
            
            curr = tomorrow_date + timedelta(days=1)
            while curr <= end_of_week:
                if hits_day(item['recurring_pattern'], curr): shadow_week.append((item, curr))
                curr += timedelta(days=1)

    # Main Interface
    st.markdown("<h1 class='main-header'>🏠 家庭事项管理中心</h1>", unsafe_allow_html=True)
    t1, t2, t3, t4 = st.tabs(["📝 待办事宜", "🔄 循环事项", "✅ 已完成事项", "📅 家庭日历"])

    with t1:
        if tasks_df.empty:
            st.info("目前没有任务。在侧边栏添加一个吧！")
        else:
            # --- Displays Tab 1 ---
            if today_list or shadow_today:
                st.markdown('<div class="section-header" style="color: #ef4444; border-bottom-color: #fecaca;">⚡ 今日急需处理</div>', unsafe_allow_html=True)
                for row in shadow_today: render_task(row, is_shadow=True, location="sh_today")
                for row in today_list: render_task(row, location="today")

            if tomorrow_list or shadow_tomorrow:
                st.markdown('<div class="section-header">🌙 明日处理事项</div>', unsafe_allow_html=True)
                for row in shadow_tomorrow: render_task(row, is_shadow=True, location="sh_tomorrow")
                for row in tomorrow_list: render_task(row, location="tomorrow")
            
            if week_list or shadow_week:
                st.markdown('<div class="section-header">🗓️ 本周剩余任务</div>', unsafe_allow_html=True)
                for item, d in shadow_week:
                    temp_row = item.copy()
                    temp_row['due_date'] = d.strftime("%Y-%m-%d 12:00")
                    render_task(temp_row, is_shadow=True, location="sh_week")
                for row in week_list: render_task(row, location="week")
                
            if later_list:
                st.markdown('<div class="section-header">⏳ 以后待办</div>', unsafe_allow_html=True)
                for row in later_list: render_task(row, location="later")

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
            for _, row in completed_tasks.iterrows():
                render_task(row, location="comp_tab")

    with t4:
        cal_url = f"https://calendar.google.com/calendar/embed?src={cal_email}&ctz=Asia%2FSingapore&hl=zh_CN&mode=AGENDA"
        st.components.v1.iframe(cal_url, height=700, scrolling=True)

    st.markdown("---")
    st.markdown(f"<p style='text-align: center; color: #888;'>最后更新: {get_now_sgt().strftime('%Y-%m-%d %H:%M')}</p>", unsafe_allow_html=True)

except Exception as e:
    st.error(f"❌ 系统发生错误: {e}")
    st.exception(e)
