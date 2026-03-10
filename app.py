import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime
import pytz
import os
from openai import OpenAI
from dotenv import load_dotenv

# 1. 必须是第一个命令
st.set_page_config(
    page_title="家庭事项管理系统",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="auto"
)

# 2. 环境初始化
load_dotenv()
try:
    # 优先读取 Streamlit Secrets
    api_key = st.secrets["OPENAI_API_KEY"] if "OPENAI_API_KEY" in st.secrets else os.getenv("OPENAI_API_KEY")
except Exception:
    api_key = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=api_key) if api_key else None
SGT = pytz.timezone('Asia/Singapore')

def get_now_sgt():
    return datetime.now(SGT)

# --- 数据库配置 (直接在根目录) ---
DB_FILE = "tasks.db"

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
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    elif 'due_date' not in columns:
        c.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT")
    conn.commit()
    conn.close()

def extract_date_llm(task_text):
    if not client: return None
    now = get_now_sgt()
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"""你是时间解析专家。今天是 {now.strftime('%Y-%m-%d')} (新加坡时间, {now.strftime('%A')})。
                从用户文本中提取任务日期和时间。
                - 如果提取到具体时间，返回 'YYYY-MM-DD HH:MM' (24小时制)。
                - 如果只有日期，返回 'YYYY-MM-DD 12:00'。
                - 如果用户没有提到任何日期，请默认返回【今天】的日期 'YYYY-MM-DD 23:59'。
                - 只返回 'YYYY-MM-DD HH:MM' 格式的字符串，不要任何多余文字。"""},
                {"role": "user", "content": task_text}
            ],
            temperature=0
        )
        dt_str = response.choices[0].message.content.strip()
        # 验证格式防止AI返回乱码
        datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        return dt_str
    except Exception as e:
        print(f"LLM 解析错误: {e}")
        return now.strftime("%Y-%m-%d 23:59") # 彻底兜底，解析失败也给个今天的日期

def get_tasks():
    conn = sqlite3.connect(DB_FILE)
    query = "SELECT * FROM tasks ORDER BY completed ASC, CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END, due_date ASC, created_at ASC"
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

# --- 界面样式 ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;700&display=swap');
    .stApp { background: linear-gradient(135deg, #fdfbfb 0%, #ebedee 100%); font-family: 'Outfit', sans-serif; }
    .main-header { color: #1e3a8a; text-align: center; font-size: 2rem !important; font-weight: 700; padding: 1rem 0; }
    .task-container { background: white; padding: 0.8rem 1rem; border-bottom: 1px solid #eee; transition: background 0.2s; }
    .task-container:hover { background-color: #f9fafb; }
    .todo-text { font-size: 1.1rem !important; color: #1f2937; margin: 0; }
    .todo-date { font-size: 0.85rem; color: #6366f1; font-weight: 600; margin-top: 4px; }
    .todo-completed { text-decoration: line-through; opacity: 0.4; }
</style>
""", unsafe_allow_html=True)

# --- 主逻辑块 ---
try:
    init_db()

    # 1. 登录验证
    if "password_correct" not in st.session_state:
        st.markdown("<h2 style='text-align: center; color: #1e3a8a; margin-top: 50px;'>🏠 家庭系统登录</h2>", unsafe_allow_html=True)
        col_l, col_m, col_r = st.columns([1, 2, 1])
        with col_m:
            pwd = st.text_input("请输入访问密码 (6位数字):", type="password")
            if pwd == "790228":
                st.session_state["password_correct"] = True
                st.rerun()
            elif pwd:
                st.error("🚫 密码错误，请重试")
            st.info("💡 提示：密码是6位数字")
            st.caption("忘记密码？联系 [xuchunli@gmail.com](mailto:xuchunli@gmail.com)")
        st.stop()

    # 2. 侧边栏
    with st.sidebar:
        st.header("🏠 系统控制")
        if st.button("🔴 退出登录", use_container_width=True):
            del st.session_state["password_correct"]
            st.rerun()
        st.info(f"📍 新加坡时间\n{get_now_sgt().strftime('%Y-%m-%d %H:%M')}")
        st.divider()
        new_task = st.text_input("➕ 新增事项:", placeholder="例如：周五拿快递...")
        if st.button("立即添加", use_container_width=True):
            if new_task:
                with st.spinner("AI 解析中..."):
                    add_task(new_task)
                st.rerun()
        st.divider()
        st.markdown("### ⚙️ 日历配置")
        cal_email = st.text_input("Google Email:", value="xuchunli@gmail.com")

    # 3. 主界面
    st.markdown("<h1 class='main-header'>🏠 家庭事项管理中心</h1>", unsafe_allow_html=True)
    t1, t2 = st.tabs(["📝 待办事宜", "📅 家庭日历"])

    with t1:
        tasks_df = get_tasks()
        if tasks_df.empty:
            st.info("目前没有任务。在侧边栏添加一个吧！")
        else:
            for _, row in tasks_df.iterrows():
                with st.container():
                    st.markdown('<div class="task-container">', unsafe_allow_html=True)
                    c1, c2, c3 = st.columns([0.05, 0.85, 0.1])
                    
                    is_comp = c1.checkbox("", value=row['completed'], key=f"c_{row['id']}")
                    if is_comp != row['completed']:
                        update_task_status(row['id'], is_comp)
                        st.rerun()
                        
                    style = "todo-completed" if row['completed'] else ""
                    due_label = f"<div class='todo-date'>📅 预计: {row['due_date'][:10]}</div>" if row['due_date'] else ""
                    c2.markdown(f"<p class='todo-text {style}'>{row['task']}</p>{due_label}", unsafe_allow_html=True)
                    
                    if c3.button("🗑️", key=f"d_{row['id']}"):
                        delete_task(row['id'])
                        st.rerun()
                    st.markdown('</div>', unsafe_allow_html=True)

    with t2:
        cal_url = f"https://calendar.google.com/calendar/embed?src={cal_email}&ctz=Asia%2FSingapore&hl=zh_CN&mode=AGENDA"
        st.components.v1.iframe(cal_url, height=700, scrolling=True)

    st.markdown("---")
    st.markdown(f"<p style='text-align: center; color: #888;'>最后更新: {get_now_sgt().strftime('%Y-%m-%d %H:%M')}</p>", unsafe_allow_html=True)

except Exception as e:
    st.error(f"❌ 系统发生错误: {e}")
    st.exception(e)
