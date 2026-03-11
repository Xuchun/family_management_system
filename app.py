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
    c.execute("UPDATE tasks SET task = ? WHERE id = ?", (new_text, task_id))
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

    def format_tasks_to_txt(df):
        if df.empty:
            return "没有任务数据。"
        # 表格表头
        header = f"{'截止时间':<20} | {'任务内容':<50} | {'循环':<15} | {'状态':<10}\n"
        sep = "-" * 100 + "\n"
        lines = [header, sep]
        
        # 按照完成状态和日期排序
        sorted_df = df.sort_values(by=['completed', 'due_date'], ascending=[True, True])
        
        for _, row in sorted_df.iterrows():
            due = row['due_date'] if row['due_date'] else "未设置"
            task = row['task'].replace('\n', ' ')
            recur = row['recurring_pattern'] if row['recurring_pattern'] else "无"
            status = "✅ 已完成" if row['completed'] else "⏳ 待办"
            lines.append(f"{due:<20} | {task:<50} | {recur:<15} | {status:<10}\n")
        
        return "".join(lines)

    @st.dialog("📋 事项添加结果")
    def show_add_dialog(result):
        if result["success"]:
            st.success("✅ 该事项已成功入库！")
            st.markdown(f"**内容：** {result['task']}")
            if result['due']:
                st.markdown(f"**⏰ 预计执行时间：** {result['due']}")
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
                c1.markdown("🔄")
                
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
                due_val = f"📅 预计: {row['due_date']}" if row['due_date'] else ""
                
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

    # Sidebar
    with st.sidebar:
        st.header("🏠 系统控制")
        if st.button("🔴 退出登录", use_container_width=True):
            del st.session_state["password_correct"]
            cookie_manager.delete("family_system_auth")
            st.rerun()
        st.info(f"📍 新加坡时间\n{get_now_sgt().strftime('%Y-%m-%d %H:%M')}")
        st.divider()
        def handle_add_cb():
            st.session_state["temp_task_text"] = st.session_state.get("input_new_task", "")
            st.session_state["input_new_task"] = ""

        st.text_input("➕ 新增事项:", placeholder="例如：每周二拿快递...", key="input_new_task")
        if st.button("立即添加", use_container_width=True, on_click=handle_add_cb):
            task_to_add = st.session_state.get("temp_task_text")
            if task_to_add:
                with st.spinner("AI 解析并提交中..."):
                    res = add_task(task_to_add)
                    st.session_state["last_add_result"] = res
                    # Clear temp and rerun to refresh
                    st.session_state["temp_task_text"] = None
                    st.rerun()

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

    # --- 8. Combine and Sort ---
    def prepare_sorted_list(normal_items, shadow_items_with_dates=None, shadow_items_plain=None, default_date=None):
        combined = []
        for r in normal_items:
            temp = r.copy()
            temp['_is_shadow'] = False
            combined.append(temp)
        
        if shadow_items_plain and default_date:
            for r in shadow_items_plain:
                temp = r.copy()
                temp['_is_shadow'] = True
                temp['due_date'] = default_date.strftime("%Y-%m-%d 12:00")
                combined.append(temp)
        
        if shadow_items_with_dates:
            for r, d in shadow_items_with_dates:
                temp = r.copy()
                temp['_is_shadow'] = True
                temp['due_date'] = d.strftime("%Y-%m-%d 12:00")
                combined.append(temp)
        
        # Sort by due_date
        combined.sort(key=lambda x: x['due_date'] if x['due_date'] else "9999-12-31")
        return combined

    final_today = prepare_sorted_list(today_list, shadow_items_plain=shadow_today, default_date=today_date)
    final_tomorrow = prepare_sorted_list(tomorrow_list, shadow_items_plain=shadow_tomorrow, default_date=tomorrow_date)
    final_week = prepare_sorted_list(week_list, shadow_items_with_dates=shadow_week)
    final_later = prepare_sorted_list(later_list)

    # Main Interface
    st.markdown("<h1 class='main-header'>🏠 家庭事项管理中心</h1>", unsafe_allow_html=True)

    # CSS to style the download button to match tab labels (approx 14px/16px)
    st.markdown("""
        <style>
        div.stDownloadButton > button {
            font-size: 14px !important;
            font-weight: 500 !important;
            color: #555 !important;
            background-color: #f8f9fa !important;
            border: 1px solid #e9ecef !important;
            padding: 5px 15px !important;
            height: 38px !important;
            border-radius: 6px !important;
            margin-top: 48px !important; /* Offset to align with tab labels */
            transition: all 0.2s ease !important;
        }
        div.stDownloadButton > button:hover {
            color: #ef4444 !important;
            border-color: #ef4444 !important;
            background-color: #fff !important;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
        }
        </style>
    """, unsafe_allow_html=True)

    c_tabs, c_dl = st.columns([0.76, 0.24])
    with c_tabs:
        t1, t2, t3 = st.tabs(["📝 待办事宜", "🔄 循环事项", "✅ 已完成事项"])
    with c_dl:
        if not tasks_df.empty:
            txt_content = format_tasks_to_txt(tasks_df)
            st.download_button(
                label="📥 下载任务报表 (TXT)",
                data=txt_content,
                file_name=f"家庭事项清单_{get_now_sgt().strftime('%m%d_%H%M')}.txt",
                mime="text/plain",
                key="dl_btn_v3"
            )

    with t1:
        if tasks_df.empty:
            st.info("目前没有任务。在侧边栏添加一个吧！")
        else:

            # --- Displays Tab 1 ---
            if final_today:
                st.markdown('<div class="section-header" style="color: #ef4444; border-bottom-color: #fecaca;">⚡ 今日急需处理</div>', unsafe_allow_html=True)
                for row in final_today: render_task(row, is_shadow=row['_is_shadow'], location="final_today")

            if final_tomorrow:
                st.markdown('<div class="section-header">🌙 明日处理事项</div>', unsafe_allow_html=True)
                for row in final_tomorrow: render_task(row, is_shadow=row['_is_shadow'], location="final_tomorrow")
            
            if final_week:
                st.markdown('<div class="section-header">🗓️ 本周剩余任务</div>', unsafe_allow_html=True)
                for row in final_week: render_task(row, is_shadow=row['_is_shadow'], location="final_week")
                
            if final_later:
                st.markdown('<div class="section-header">⏳ 以后待办</div>', unsafe_allow_html=True)
                for row in final_later: render_task(row, location="final_later")

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


    st.markdown("---")
    st.markdown(f"<p style='text-align: center; color: #888;'>最后更新: {get_now_sgt().strftime('%Y-%m-%d %H:%M')}</p>", unsafe_allow_html=True)

except Exception as e:
    st.error(f"❌ 系统发生错误: {e}")
    st.exception(e)
