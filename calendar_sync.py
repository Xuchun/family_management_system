import os
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import pytz

SGT = pytz.timezone('Asia/Singapore')

# 优先从 st.secrets 中加载 Google 凭据
# 用户应在 Streamlit Cloud 的 Secrets 中添加 google_service_account 字段
def get_calendar_service():
    creds = None
    if "google_service_account" in st.secrets:
        creds_info = st.secrets["google_service_account"]
        creds = service_account.Credentials.from_service_account_info(creds_info)
    elif os.path.exists("service_account.json"):
        creds = service_account.Credentials.from_service_account_file("service_account.json")
    
    if creds:
        scoped_creds = creds.with_scopes(['https://www.googleapis.com/auth/calendar'])
        return build('calendar', 'v3', credentials=scoped_creds)
    return None

def upsert_calendar_event(task_id, task_text, due_date_str, google_event_id, cal_email):
    service = get_calendar_service()
    if not service or not cal_email:
        return google_event_id

    try:
        # 准备任务时间
        if not due_date_str:
            # 如果没有截止日期，默认设为今天全天
            start_date = datetime.now(SGT).strftime('%Y-%m-%d')
            end_date = (datetime.now(SGT) + timedelta(days=1)).strftime('%Y-%m-%d')
            event_body = {
                'summary': f'🏠 {task_text}',
                'start': {'date': start_date},
                'end': {'date': end_date},
                'description': '由家庭事项管理系统同步',
            }
        else:
            # 这里的 due_date_str 格式通常是 YYYY-MM-DD HH:MM
            dt = datetime.strptime(due_date_str, '%Y-%m-%d %H:%M')
            dt_with_tz = SGT.localize(dt)
            start_time = dt_with_tz.isoformat()
            end_time = (dt_with_tz + timedelta(hours=1)).isoformat()
            event_body = {
                'summary': f'🏠 {task_text}',
                'start': {'dateTime': start_time, 'timeZone': 'Asia/Singapore'},
                'end': {'dateTime': end_time, 'timeZone': 'Asia/Singapore'},
                'description': '由家庭事项管理系统同步',
            }

        if google_event_id:
            # 更新现有事件
            event = service.events().update(calendarId=cal_email, eventId=google_event_id, body=event_body).execute()
        else:
            # 创建新事件
            event = service.events().insert(calendarId=cal_email, body=event_body).execute()
        
        return event.get('id')
    except Exception as e:
        print(f"日历同步失败: {e}")
        return google_event_id

def delete_calendar_event(google_event_id, cal_email):
    if not google_event_id or not cal_email:
        return
    service = get_now_sgt_service() if 'get_now_sgt_service' in globals() else get_calendar_service()
    if not service:
        return

    try:
        service.events().delete(calendarId=cal_email, eventId=google_event_id).execute()
    except Exception as e:
        print(f"日历删除失败: {e}")

def get_now_sgt_service():
    return get_calendar_service()
