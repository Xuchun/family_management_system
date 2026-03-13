# 🏠 家庭管理系统 (Family Management System)

一个基于 **Streamlit** + **AI** 构建的现代化、极简主义家庭事务管理平台，专为提升家庭协作效率而设计。

---

## 🌟 核心理念
本项目的核心目标是为家庭成员提供一个**清晰、直观、智能**的中心化管理界面，将繁杂的日常琐事与长期的日程规划有机结合。

## 🧩 系统模块架构 (v4.0)
系统已升级为四大核心模块架构，用户可通过顶部主页面板一键切换不同主题功能：

### 📝 模块一：家庭事项 (缺省主页)
承载系统最初始的 AI 驱动任务管理核心，包括智能待办事项、循环事务引擎和分类整理功能。日常琐事与待办日程均在此处总览。

### 💰 模块二：家庭财务
家庭资产的大盘驾驶舱。涵盖“当前家庭财务一览”及“投资一览表”板块，为以后逐步接入家庭账单、理财报表与资产分析等高阶功能打下基础。

### 🏋️‍♂️ 模块三：爸爸的健身
专属于家庭成员的全面健身档案室。集成了健身目标、计划以及每次记录，助力健康管理。

### 🌸 模块四：恩雅的健康
专门为小成员设计的健康成长记录空间。

---

## ✨ 主要功能

### 1. 📝 智能待办事项 (To-Do List)
*   **AI 自然语言解析**: 利用 OpenAI GPT-4o 模型，自动理解用户输入的模糊时间（如“下周五下午三点”）。
*   **多维度自动归类**: 实时将任务切分为“⚡ 今日急需”、“🌙 明日”、“🗓️ 本周”及“⏳ 本月”等板块。
*   **精准排序**: 各区域内任务根据截止时间自动排序。
*   **状态追踪**: 支持行内修改任务内容。

### 2. 🔄 高阶循环引擎 (Recurring Engine)
*   **智能频率提取**: 识别“每月最后一天”、“每周二”等复杂循环指令。
*   **自动投影衍生**: 系统能向未来一个月发起扫描，将长线循环任务精确显影在日历列表中。

### 3. ☁️ 云端同步与备份 (Cloud Backup)
*   **Google Drive 深度集成**: 支持每日定时自动备份及一键“云端同步”，确保数据永不丢失。
*   **多端一致性**: 全系统基于新加坡 (SGT) 时区，杜绝时间偏差。

### 4. 🔐 银行级安全架构 (Security v4.0)
*   **管理员 Gmail 双向信任**: 基于 Google OAuth2 的真实时令牌交换 (Token Exchange)，确保管理员身份不可伪造。
*   **密码加盐哈希 (Salted Hashing)**: 数据库不再明文存储，采用 SHA-256 加盐哈希加密，保障核心凭证安全。
*   **阅后即焚 URL 令牌**: 针对云端 iframe 环境优化，登录钥匙仅在握手瞬间存在，识别后立即从地址栏抹除，极致保护隐私。
*   **持久登录状态 (CHIPS)**: 采用最新的 Partitioned Cookies 协议，支持跨域嵌套环境下的 30 天持久登录，彻底解决刷新掉线问题。
*   **物理级注销**: 注销时自动执行 URL 参数物理清洗与 Cookie 黑名单锁定。

---

## 🔨 技术架构
*   **前端/框架**: Streamlit + Custom CSS + JavaScript Persistence
*   **数据库**: SQLite (System Config & Task Storage)
*   **AI 引擎**: OpenAI API (GPT-4o)
*   **认证/云端**: Google OAuth2 + Google Drive API
*   **依赖管理**: `uv` (推荐), `pandas`, `requests`, `pytz`, `extra-streamlit-components`

---

## 🚀 快速开始

### 1. 环境准备
```bash
uv add streamlit pandas openai python-dotenv pytz requests extra-streamlit-components
```

### 2. 配置环境 (.env)
```text
OPENAI_API_KEY=sk-xxxx
GOOGLE_CLIENT_ID=5555...
GOOGLE_CLIENT_SECRET=GOCSPX...
GOOGLE_BACKUP_URL=https://script.google.com/...
```

### 3. 启动应用
```bash
uv run streamlit run app.py
```

---

## 📧 联系与支持
如有重置密码需求或功能定制建议，请联系：
**管理员**: [xuchunli@gmail.com](mailto:xuchunli@gmail.com)

---
*© 2026 家庭管理系统 v4.0 - 让生活更有序*
