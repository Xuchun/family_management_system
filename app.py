import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import pytz
import os
from openai import OpenAI
from dotenv import load_dotenv
import extra_streamlit_components as stx

# --- 1. Streamlit UI Config (Must be FIRST) ---
st.set_page_config(
    page_title="家庭管理系统",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="auto"
)

# --- 2. Cookie Management ---
# Cookie 管理器初始化 (不可使用 @st.cache_resource，因为它是 UI 组件)
cookie_manager = stx.CookieManager(key="auth_cookie_manager")

# --- 3. Environment & Global Config ---
load_dotenv()
try:
    api_key = st.secrets["OPENAI_API_KEY"] if "OPENAI_API_KEY" in st.secrets else os.getenv("OPENAI_API_KEY")
    app_pwd = st.secrets["APP_PASSWORD"] if "APP_PASSWORD" in st.secrets else os.getenv("APP_PASSWORD")
except Exception:
    api_key = os.getenv("OPENAI_API_KEY")
    app_pwd = os.getenv("APP_PASSWORD")

client = OpenAI(api_key=api_key) if api_key else None
SGT = pytz.timezone('Asia/Singapore')

def get_now_sgt():
    return datetime.now(SGT)

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
    
    # New table for individual recurring task completions
    c.execute('''CREATE TABLE IF NOT EXISTS recurring_completions
                 (task_id INTEGER, 
                  completed_date TEXT,
                  PRIMARY KEY (task_id, completed_date))''')
    conn.commit()
    conn.close()

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
                从用户文本中解析任务，并返回特定的格式：
                1. CLEAN_TASK: 任务内容（去除里面的‘明天’、‘下周’等时间词，但请保留工资、费用等重要信息，使显示更简洁）。
                2. DATE: 截止日期时间 'YYYY-MM-DD HH:MM'。若文本中提到新的日期/时间意图，请准确转换。若完全未提到日期意图，请务必返回原始日期：{f_date}。
                3. RECUR: 循环模式 (例如 Monday, Tuesday..., Everyday, Weekend, Monthly-15, Monthly-LastDay 等) 或 None。若未提到新的循环意图，请返回：{f_recur}。
                
                返回格式示例：CLEAN_TASK: 内容 | DATE: YYYY-MM-DD HH:MM | RECUR: Pattern"""},
                {"role": "user", "content": task_text}
            ],
            temperature=0
        )
        res = response.choices[0].message.content.strip()
        
        # 使用更稳健的解析方式
        parts = {p.split(':', 1)[0].strip(): p.split(':', 1)[1].strip() for p in res.split('|') if ':' in p}
        
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
    return df

def add_task(task_text):
    try:
        clean_task, due_datetime, recur_pattern = extract_date_llm(task_text)
        now_str = get_now_sgt().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO tasks (task, due_date, recurring_pattern, created_at) VALUES (?, ?, ?, ?)", 
                  (clean_task, due_datetime, recur_pattern, now_str))
        task_id = c.lastrowid
        conn.commit()
        
        # Verify insertion
        c.execute("SELECT task, due_date FROM tasks WHERE id = ?", (task_id,))
        row = c.fetchone()
        conn.close()
        
        if row:
            return {"success": True, "task": row[0], "due": row[1], "recur": recur_pattern}
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
    
    # AI re-evaluation
    clean_task, due_datetime, recur_pattern = extract_date_llm(new_text, f_date, f_recur)
    
    c.execute("UPDATE tasks SET task = ?, due_date = ?, recurring_pattern = ? WHERE id = ?", 
              (clean_task, due_datetime, recur_pattern, task_id))
    conn.commit()
    conn.close()

def mark_recurring_date_completed(task_id, date_str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO recurring_completions (task_id, completed_date) VALUES (?, ?)", (task_id, date_str))
    conn.commit()
    conn.close()

def unmark_recurring_date_completed(task_id, date_str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM recurring_completions WHERE task_id = ? AND completed_date = ?", (task_id, date_str))
    conn.commit()
    conn.close()

def get_recurring_completions():
    try:
        conn = sqlite3.connect(DB_FILE)
        df = pd.read_sql_query("SELECT * FROM recurring_completions", conn)
        conn.close()
        return df
    except:
        return pd.DataFrame(columns=['task_id', 'completed_date'])

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

    # --- 🔐 登录逻辑与持久化验证 ---
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False
    if "editing_task_id" not in st.session_state:
        st.session_state["editing_task_id"] = None

    if "logout_requested" not in st.session_state:
        st.session_state["logout_requested"] = False

    just_logged_out = False
    # 1. 拦截登出请求并优先处理
    if st.session_state["logout_requested"]:
        # 核心漏洞修复：extra_streamlit_components 的 delete() 方法由于缺少 path=/ 参数，
        # 会在部分浏览器或场景下静默失败。强制使用 set() 并附带过去的时间戳进行底层物理覆写。
        cookie_manager.set("family_system_auth", "", expires_at=datetime.now() - timedelta(days=365))
        st.session_state["authenticated"] = False
        st.session_state["logout_requested"] = False
        just_logged_out = True

    # 2. 尝试从浏览器读取 Cookie (仅在尚未认证且不在刚刚登出的周期内时)
    if not st.session_state["authenticated"] and not just_logged_out:
        auth_cookie = cookie_manager.get("family_system_auth")
        if auth_cookie == "authenticated":
            st.session_state["authenticated"] = True
            st.rerun()

    # 2. 如果当前未通过任何方式认证，则显示登录页面
    login_placeholder = st.empty()
    if not st.session_state["authenticated"]:
        with login_placeholder.container():
            st.markdown("<h2 style='text-align: center; color: #1e3a8a; margin-top: 50px;'>🏠 家庭系统登录</h2>", unsafe_allow_html=True)
            _, col_m, _ = st.columns([1, 2, 1])
            with col_m:
                pwd = st.text_input("请输入访问密码 (6位数字):", type="password", key="login_pwd")
                if pwd == app_pwd:
                    st.session_state["authenticated"] = True
                    cookie_manager.set("family_system_auth", "authenticated", expires_at=datetime.now() + timedelta(days=30))
                    st.success("✅ 登录成功！正在为您开启系统...")
                elif pwd:
                    st.error("🚫 密码错误")
                
                st.info("💡 提示：密码是6位数字。")
                st.warning("⚠️ 如果您是 Safari 浏览器用户：请确保已关闭‘阻止所有 Cookie’或‘阻止跨站追踪’设置，否则系统无法保持登录。")
            
    # 一旦认证成功，如果原本显示了登录界面，现在将其清空
    if st.session_state["authenticated"]:
        login_placeholder.empty()
    else:
        # 否则阻断后续显示
        if just_logged_out:
            import streamlit.components.v1 as components
            components.html(
                "<script>window.parent.document.cookie = 'family_system_auth=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';</script>",
                height=0
            )
        st.stop()

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
                
            # Handle inline edit
            if st.session_state.get("editing_task_id") == row['id']:
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
                recur_tag = f"<span class='recur-tag'>🔄 循环: {row['recurring_pattern']}</span>" if row['recurring_pattern'] else ""
                due_val = f"📅 日期/时间: {format_date_with_weekday(row['due_date'])}" if row['due_date'] else ""
                
                c2.markdown(f"<p class='todo-text {style}'>{row['task']}{recur_tag}</p><div class='todo-date'>{due_val}</div>", unsafe_allow_html=True)
                
                if not is_shadow:
                    edit_col, del_col = c3.columns(2)
                    if edit_col.button("✏️", key=edit_id, help="修改"):
                        st.session_state["editing_task_id"] = row['id']
                        st.rerun()
                    if del_col.button("🗑️", key=del_id, help="删除"):
                        delete_task(row['id'])
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
    recurring_list, today_list, tomorrow_list, week_list, later_list = [], [], [], [], []
    shadow_today, shadow_tomorrow, shadow_week, shadow_later = [], [], [], []
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

    # Header Row
    c_logout, c_title, c_empty = st.columns([0.15, 0.70, 0.15], vertical_alignment="center")
    with c_logout:
        st.button("🔴 退出登录", use_container_width=True, on_click=handle_logout)
    with c_title:
        st.markdown("<h1 class='main-header'>🏠 家庭管理系统</h1>", unsafe_allow_html=True)
    with c_empty:
        pass

    st.markdown('<br>', unsafe_allow_html=True)
    top_tab1, top_tab2, top_tab3 = st.tabs(['📝 家庭事项', '💪 我的健身', '💰 家庭财务'])

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

            col_add_input, col_add_btn, col_dl_btn = st.columns([0.50, 0.20, 0.30], vertical_alignment="bottom")
            with col_add_input:
                st.text_input("➕ 新增事项:", placeholder="请输入需要添加的代办事项，比如这周六下午4点去海滩...", key="input_new_task", label_visibility="collapsed")
            with col_add_btn:
                if st.button("立即添加", use_container_width=True, on_click=handle_add_cb):
                    pass
            with col_dl_btn:
                if not tasks_df.empty:
                    txt_content = generate_txt_report()
                    st.download_button(
                        label="📥 下载待办事项清单",
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

            t1, t2, t3 = st.tabs(["📝 待办事项", "🔄 循环事项", "✅ 已完成事项"])

            with t1:
                if tasks_df.empty:
                    st.info("目前没有任务。在侧边栏添加一个吧！")
                else:

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
                    for _, row in completed_tasks.iterrows():
                        is_shade = row.get('_is_shadow', False)
                        render_task(row, is_shadow=is_shade, location="comp_tab")

    with top_tab2:
        st.subheader('🎯 我的健身目标')
        st.info('内容可以先为空，我后面会继续加入。')
        st.subheader('📅 健身计划')
        st.info('内容可以先为空，我后面会继续加入。')
        st.subheader('✅ 每次健身项目完成记录')
        st.info('内容可以先为空，我后面会继续加入。')

    with top_tab3:
        st.subheader('💵 当前家庭财务一览')
        st.info('内容可以先为空，我后面会继续加入。')
        st.subheader('📈 投资一览表')
        st.info('内容可以先为空，我后面会继续加入。')


    with top_tab2:
        st.subheader('🎯 我的健身目标')
        st.info('内容可以先为空，我后面会继续加入。')
        st.subheader('📅 健身计划')
        st.info('内容可以先为空，我后面会继续加入。')
        st.subheader('✅ 每次健身项目完成记录')
        st.info('内容可以先为空，我后面会继续加入。')

    with top_tab3:
        st.subheader('💵 当前家庭财务一览')
        st.info('内容可以先为空，我后面会继续加入。')
        st.subheader('📈 投资一览表')
        st.info('内容可以先为空，我后面会继续加入。')



    st.markdown("---")
    st.markdown(f"<p style='text-align: center; color: #888;'>最后更新: {get_now_sgt().strftime('%Y-%m-%d %H:%M')}</p>", unsafe_allow_html=True)

except Exception as e:
    st.error(f"❌ 系统发生错误: {e}")
    st.exception(e)
