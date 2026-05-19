# Render 免费计划 + Supabase 免费 PostgreSQL 部署指南

## 一、方案优势

- **Render**：免费 Web Service，自动从 GitHub 部署，休眠后自动唤醒。
- **Supabase**：免费 PostgreSQL 数据库（500MB），数据完全独立持久，Render 重新部署或休眠都不会丢失。
- **成本**：两边都是免费层，**0 美金/月**。

---

## 二、Supabase 配置（数据库）

### 1. 注册/登录 Supabase
访问 [supabase.com](https://supabase.com)，用 GitHub 账号登录。

### 2. 创建新项目
- 点击 **New Project**
- 选择组织，输入项目名称（如 `lead-finder-db`）
- 设置数据库密码（**务必保存好**，后面要用）
- 选择区域：建议 **Singapore ( Southeast Asia )**，国内访问较快
- 等待 1~2 分钟项目创建完成

### 3. 获取数据库连接字符串
- 进入项目 → 左侧 **Project Settings** → **Database**
- 找到 **Connection string** 区域
- 选择 **URI** 标签
- 复制类似下面的连接字符串：
  ```
  postgresql://postgres:[YOUR-PASSWORD]@db.xxxxxxxxxx.supabase.co:5432/postgres
  ```
- **把 `[YOUR-PASSWORD]` 替换为你刚才设置的密码**

> 这就是 `DATABASE_URL`，后面要填到 Render 的环境变量里。

---

## 三、Render 配置（Web 服务）

### 1. 登录 Render
访问 [render.com](https://render.com)，用 GitHub 账号登录。

### 2. 新建 Web Service
- Dashboard 点击 **New +** → **Web Service**
- 连接你的 GitHub 仓库 `adongtt/lead-finder`
- 填写基本信息：
  - **Name**：`lead-finder`
  - **Region**：`Singapore`（和 Supabase 同区域，延迟最低）
  - **Runtime**：`Python 3`
  - **Build Command**：`pip install -r requirements.txt`
  - **Start Command**：`uvicorn api:app --host 0.0.0.0 --port $PORT`

### 3. 设置环境变量（最关键！）
在 Render 的 **Environment** 标签页添加：

| Key | Value | 说明 |
|-----|-------|------|
| `DATABASE_URL` | `postgresql://postgres:你的密码@db.xxx.supabase.co:5432/postgres` | Supabase 的连接字符串 |
| `SESSION_SECRET` | 随便输一串 32 位随机字符 | 用于加密登录 Session |
| `HUNTER_KEY` | （可选）你的 Hunter.io API Key | 邮件发现服务 |
| `SNOV_KEY` | （可选）你的 Snov.io API Key | 邮件发现备用 |

> 如果 Supabase 连接报错 SSL 相关，把 `DATABASE_URL` 末尾加上 `?sslmode=require`，变成：
> `postgresql://postgres:你的密码@db.xxx.supabase.co:5432/postgres?sslmode=require`

### 4. 部署
点击 **Create Web Service**，Render 会自动拉代码、安装依赖、启动服务。

第一次部署大概 2~3 分钟。看到 **"Your service is live"** 就成功了。

---

## 四、验证部署

1. **打开 Render 分配的域名**（如 `https://lead-finder.onrender.com`）
2. 用 `zhangsan / zs123456` 登录
3. 执行一次搜索
4. 进入 Supabase 后台 → **Table Editor**，应该能看到自动创建的 4 张表：
   - `searches`
   - `contacts`
   - `followups`
   - `keywords`

---

## 五、后续更新代码

以后只要往 GitHub `main` 分支 push 代码，Render 会自动重新部署。
**数据库数据不会丢失**，因为数据存在 Supabase，不在 Render 本地。

---

## 六、常见问题

**Q：Supabase 免费数据库会暂停吗？**  
A：会。如果连续 7 天没有任何访问，Supabase 会暂停项目。下次访问时自动恢复，冷启动约 1~3 秒。建议偶尔登录一下后台，或者设置一个定时 ping（如 UptimeRobot 每 5 分钟访问一次你的网站）。

**Q：Render 免费实例休眠后第一次访问慢？**  
A：正常。免费实例 15 分钟无访问会休眠，下次请求需要 10~30 秒唤醒。这是 Render 免费层的限制，不影响数据。

**Q：如何把之前的 SQLite 数据迁移到 Supabase？**  
A：如果本地 `web_results/leadfinder.db` 有数据，最简单的方式是：
1. 先把本地的 `.json.bak` 文件恢复为 `.json`
2. 设置环境变量 `DATABASE_URL` 指向 Supabase
3. 在本地运行一次 `python api.py`（或直接启动 uvicorn）
4. 代码会自动执行 `_migrate_json_to_postgres()`，把 JSON 数据导入 Supabase

**Q：我想同时保留 Fly.io 配置可以吗？**  
A：可以。项目里的 `Dockerfile`、`fly.toml`、`FLYIO_DEPLOY.md` 都还在，不影响 Render 部署。Render 会忽略这些文件，直接用 Python 环境运行。
