# 🌐 Google Drive 备份设置指南

为了让您的家庭管理系统能够自动备份到您的 Google Drive，请按照以下步骤获取必要的凭证：

### 第 1 步：创建 Google 服务账号 (Service Account)
1. 前往 [Google Cloud Console](https://console.cloud.google.com/)。
2. 创建一个新项目（例如叫 `Family-Manager-Backup`）。
3. 在左侧菜单中选择 **API 和服务 > 库**。
4. 搜索 `Google Drive API` 并点击 **启用**。
5. 前往 **API 和服务 > 凭据**。
6. 点击屏幕顶部的 **+ 创建凭据**，选择 **服务账号**。
7. 填写账号名称，点击 **完成**。
8. 在服务账号列表中，点击刚才创建的账号，进入 **管理密钥 (Manage Keys)** 选项卡。
9. 点击 **添加密钥 > 创建新密钥**，选择 **JSON** 格式。密钥文件会自动下载到您的电脑。

### 第 2 步：配置文件夹
1. 在您的 Google Drive 中创建一个文件夹，用于存放备份文件。
2. 打开该文件夹，从浏览器地址栏获取 **文件夹 ID**（例如：`https://drive.google.com/drive/folders/1abc123...`，其中 `1abc123...` 就是 ID）。
3. **关键操作**：点击文件夹的“共享”按钮，输入您刚才在 JSON 文件中看到的 `client_email` 地址，并赋予其 **编辑者 (Editor)** 权限。

### 第 3 步：更新本地配置
1. 打开您的 `.env` 文件。
2. 将 JSON 文件的全部内容（包括大括号）复制到 `GOOGLE_SERVICE_ACCOUNT_JSON` 变量中。
3. 将文件夹 ID 填入 `GOOGLE_DRIVE_FOLDER_ID`。

---
完成以上步骤后，点击网页上的“☁️ 云端同步备份”按钮即可开始！
