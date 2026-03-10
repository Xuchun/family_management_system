import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import pytz
import os
from openai import OpenAI
from dotenv import load_dotenv

# --- Load Environment Variables ---
load_dotenv()
# Prioritize st.secrets for Streamlit Cloud, fallback to .env for local
api_key = st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=api_key) if api_key else None

# --- Timezone Setup ---
SGT = pytz.timezone('Asia/Singapore')

def get_now_sgt():
    return datetime.now(SGT)

# --- Database Setup ---
DB_FILE = "data/tasks.db"

def init_db():
    # Ensure the directory exists (CRITICAL for Streamlit Cloud)
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("PRAGMA table_info(tasks)")
    columns = [col[1] for col in c.fetchall()]
    
    if not columns:
        c.execute('''CREATE TABLE tasks
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      task TEXT NOT NULL,
                      completed BOOLEAN NOT NULL DEFAULT 0,
                      due_date TEXT, -- Storing as YYYY-MM-DD HH:MM
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    elif 'due_date' not in columns:
        c.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT")
        
    conn.commit()
    conn.close()

def extract_date_llm(task_text):
    if not client:
        return None
    
    now = get_now_sgt()
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"""你是一个精确的时间提取助手。今天是 {now.strftime('%Y-%m-%d')} (新加坡时间, {now.strftime('%A')})。
                从用户文本中提取任务的日期和具体时间。
                - 如果有时间（如：下午五点），返回 'YYYY-MM-DD HH:MM' (24小时制)。
                - 如果只有日期，返回 'YYYY-MM-DD 23:59'。
                - 如果没有日期，返回 'None'。
                - 只返回字符串内容，不要包含任何解释。"""},
                {"role": "user", "content": f"提取任务时间: '{task_text}'"}
            ],
            temperature=0
        )
        dt_str = response.choices[0].message.content.strip()
        if dt_str != "None":
            # Simple validation check
            datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            return dt_str
    except Exception as e:
        print(f"LLM Error: {e}")
    return None

def get_tasks():
    conn = sqlite3.connect(DB_FILE)
    # Improved sorting:
    # 1. Uncompleted tasks first
    # 2. Tasks with due dates sorted chronologically
    # 3. Tasks without due dates (NULL) sorted by creation time at the end
    query = """
    SELECT * FROM tasks 
    ORDER BY 
        completed ASC, 
        CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END,
        due_date ASC, 
        created_at ASC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def add_task(task_text):
    due_datetime = extract_date_llm(task_text)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (task, due_date, created_at) VALUES (?, ?, ?)", 
              (task_text, due_datetime, get_now_sgt().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

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

# --- Streamlit UI Config ---
st.set_page_config(
    page_title="家庭事项管理系统",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="auto" # Auto handles mobile/desktop better
)

# --- Initializations ---
init_db()

# Custom CSS for Mobile & Desktop Premium Look
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;700&display=swap');
    
    /* Reset and Base Styles */
    .stApp { 
        background: linear-gradient(135deg, #fdfbfb 0%, #ebedee 100%); 
        font-family: 'Outfit', sans-serif; 
    }
    
    /* Responsiveness for Title */
    .main-header { 
        color: #1e3a8a; 
        text-align: center; 
        padding-bottom: 1rem; 
        font-weight: 700;
        font-size: 2rem !important;
    }
    
    /* Continuous List Style (No Separation) */
    .task-container {
        background: white;
        padding: 0.8rem 1rem;
        margin-bottom: 0px;
        border-bottom: 1px solid #eee;
        transition: background 0.2s;
    }
    .task-container:hover {
        background-color: #f9fafb;
    }
    /* Rounded corners for the list as a whole is handled better by keeping it simple */
    
    /* Bigger touch targets for mobile */
    .stCheckbox > label {
        padding: 10px 0;
    }
    
    .todo-text { 
        font-size: 1.1rem !important; 
        margin: 0; 
        color: #1f2937;
        line-height: 1.4;
    }
    
    .todo-date { 
        font-size: 0.85rem; 
        color: #6366f1; 
        font-weight: 600; 
        margin-top: 4px; 
    }
    
    .todo-completed { 
        text-decoration: line-through; 
        opacity: 0.4; 
    }

    /* Adjust Column Padding for Mobile */
    @media (max-width: 640px) {
        .main-header { font-size: 1.5rem !important; }
        .stMarkdown div p { font-size: 1rem !important; }
    }
</style>
""", unsafe_allow_html=True)

# --- Initializations ---
init_db()

# --- Authentication Logic ---
def check_password():
    """Returns `True` if the user had the correct password."""
    
    def password_entered():
        """Checks whether a password entered by the user is correct."""
        if st.session_state["password"] == "790228":
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # don't store password
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        # Initial login screen
        st.markdown("<h2 style='text-align: center; color: #1e3a8a;'>🏠 家庭系统登录</h2>", unsafe_allow_html=True)
        col_l, col_m, col_r = st.columns([1, 2, 1])
        with col_m:
            st.text_input(
                "请输入访问密码:", 
                type="password", 
                on_change=password_entered, 
                key="password",
                help="提示：密码是6位数字"
            )
            st.info("💡 **提示**: 密码是6位数字")
            st.caption("如有疑问或需重置密码，请发送邮件至: [xuchunli@gmail.com](mailto:xuchunli@gmail.com)")
        return False
    
    elif not st.session_state["password_correct"]:
        # Incorrect password screen
        st.markdown("<h2 style='text-align: center; color: #1e3a8a;'>🏠 家庭系统登录</h2>", unsafe_allow_html=True)
        col_l, col_m, col_r = st.columns([1, 2, 1])
        with col_m:
            st.text_input(
                "请输入访问密码:", 
                type="password", 
                on_change=password_entered, 
                key="password",
                help="提示：密码是6位数字"
            )
            st.error("🚫 密码错误，请重试。")
            st.info("💡 **提示**: 密码是6位数字")
            st.caption("忘记密码？请联系 [xuchunli@gmail.com](mailto:xuchunli@gmail.com) 协助重置。")
        return False
    
    else:
        # Correct password
        return True

# --- Main Application Logic ---
if check_password():
    # Sidebar
    with st.sidebar:
        st.header("🏠 系统控制")
        # Added Logout button
        if st.button("🔴 退出登录"):
            del st.session_state["password_correct"]
            st.rerun()
            
        st.info(f"📍 新加坡时间\n{get_now_sgt().strftime('%Y-%m-%d %H:%M')}")
        
        st.divider()
        new_task = st.text_input("➕ 新增事项:", placeholder="例如：周五下午五点拿快递...")
        if st.button("立即添加", use_container_width=True):
            if new_task:
                with st.spinner("AI 正在解析精确时间..."):
                    add_task(new_task)
                st.rerun()
        
        st.divider()
        st.markdown("### ⚙️ 日历配置")
        cal_email = st.text_input("Google Email:", value="xuchunli@gmail.com")
        
        if not os.getenv("OPENAI_API_KEY"):
            st.error("❗ 请输入 OpenAI Key 以启用 AI 精准排序")

    # Main Content
    st.markdown("<h1 class='main-header'>🏠 家庭事项管理中心</h1>", unsafe_allow_html=True)

    # Tabs for switching views
    tab1, tab2 = st.tabs(["📝 待办事宜", "📅 家庭日历"])

    with tab1:
        h1, h2 = st.columns([0.8, 0.2])
        h1.markdown("### 📝 当前待办清单")
        
        tasks_df = get_tasks()
        if not tasks_df.empty:
            csv = tasks_df.to_csv(index=False).encode('utf-8-sig')
            h2.download_button("📥 下载 CSV", csv, f'tasks_{get_now_sgt().strftime("%m%d")}.csv', 'text/csv', use_container_width=True)

        if tasks_df.empty:
            st.info("目前没有任务。在侧边栏添加一个试试吧！")
        else:
            # Show tasks in a single column layout within the tab
            for _, row in tasks_df.iterrows():
                with st.container():
                    st.markdown('<div class="task-container">', unsafe_allow_html=True)
                    c1, c2, c3 = st.columns([0.05, 0.85, 0.1])
                    
                    is_comp = c1.checkbox("", value=row['completed'], key=f"c_{row['id']}")
                    if is_comp != row['completed']:
                        update_task_status(row['id'], is_comp)
                        st.rerun()
                    
                    style = "todo-completed" if row['completed'] else ""
                    due_label = f"<span class='todo-date'>📅 预计日期: {row['due_date'][:10]}</span>" if row['due_date'] else ""
                    c2.markdown(f"<p class='todo-text {style}'>{row['task']}</p>{due_label}", unsafe_allow_html=True)
                    
                    if c3.button("🗑️", key=f"d_{row['id']}"):
                        delete_task(row['id'])
                        st.rerun()
                    st.markdown('</div>', unsafe_allow_html=True)

    with tab2:
        st.markdown("### 📅 家庭日历日程")
        # Embedding with custom email, SGT, and Agenda mode (mode=AGENDA)
        cal_url = f"https://calendar.google.com/calendar/embed?src={cal_email}&ctz=Asia%2FSingapore&hl=zh_CN&mode=AGENDA"
        st.components.v1.iframe(cal_url, height=700, scrolling=True)
        st.caption(f"当前显示: {cal_email} 的日历")

    # Footer
    st.markdown("---")
    st.markdown(f"<p style='text-align: center; color: #666;'>基于新加坡时间运维 · 最后更新: {get_now_sgt().strftime('%Y-%m-%d %H:%M')}</p>", unsafe_allow_html=True)
