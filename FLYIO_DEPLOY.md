# Fly.io 免费部署指南

## 一、确认免费额度

Fly.io 免费层（Free Tier）包含：
- **最多 3 台** `shared-cpu-1x` VM（256MB 内存）
- **最多 3GB** 持久化 Volume（Volume 是 Fly.io 的磁盘挂载，重启不会丢失）
- 绑信用卡仅为防止滥用，**只要不超额就不扣费**

本项目只需要 1 台 VM + 1GB Volume，完全在免费额度内。

---

## 二、本地准备

### 1. 安装 flyctl（Fly.io 官方 CLI）

**macOS / Linux:**
```bash
curl -L https://fly.io/install.sh | sh
```

**Windows (PowerShell):**
```powershell
iwr https://fly.io/install.ps1 -useb | iex
```

装完后记得把 flyctl 加到系统 PATH。

### 2. 登录 Fly.io
```bash
fly auth login
```
这会打开浏览器让你登录/注册。注册时绑信用卡即可。

---

## 三、部署步骤（按顺序执行）

### 第 1 步：进入项目目录
```bash
cd "d:\football gloves\lead-finder"
```

### 第 2 步：创建 Fly.io 应用
```bash
fly apps create lead-finder
```
如果提示名字已被占用，换名字，例如 `fly apps create lead-finder-yourname`。

### 第 3 步：创建持久化 Volume（核心！数据不丢就靠它）
```bash
fly volumes create leadfinder_data --region sin --size 1 --app lead-finder
```
参数说明：
- `leadfinder_data`：卷名，必须和 `fly.toml` 里的 `source` 一致
- `--region sin`：新加坡节点，国内访问较快（可选 `hkg` 香港）
- `--size 1`：1GB 容量，免费额度内

### 第 4 步：设置环境变量（可选但建议）
如果你不想用默认的 session secret，可以设置：
```bash
fly secrets set SESSION_SECRET="your-random-secret-string" --app lead-finder
```

如果用了 Hunter/SerpAPI 等外部服务，也把 key 设置成 secret：
```bash
fly secrets set HUNTER_KEY="your-key" --app lead-finder
```

### 第 5 步：部署！
```bash
fly deploy --app lead-finder
```
第一次部署会构建 Docker 镜像并上传，大概 3~5 分钟。

### 第 6 步：查看运行状态
```bash
fly status --app lead-finder
fly logs --app lead-finder
```

### 第 7 步：打开网站
```bash
fly open --app lead-finder
```
Fly.io 会给你分配一个 `https://lead-finder.fly.dev` 这样的域名。

---

## 四、后续更新代码

以后代码改了，只需重新部署：
```bash
fly deploy --app lead-finder
```
**数据库和 CSV 文件不会丢失**，因为它们存在 Volume 里，不在镜像里。

---

## 五、关键配置说明

### fly.toml
- `primary_region = 'sin'`：新加坡，国内延迟低
- `[[mounts]]`：把名为 `leadfinder_data` 的 Volume 挂载到容器内的 `/data`
- `DATA_DIR = '/data'`：告诉 Python 把 SQLite 和 CSV 都写到这个目录

### Dockerfile
- 基于 `python:3.11-slim`
- 默认暴露 8080 端口（Fly.io 要求）
- 启动命令：`uvicorn api:app --host 0.0.0.0 --port 8080`

---

## 六、常见问题

**Q：以后重新部署，数据还在吗？**  
A：在。SQLite 和 CSV 存在 Volume（`/data`）里，Docker 镜像重建不影响 Volume 内容。

**Q：如何把本地现有数据迁移到 Fly.io？**  
A：使用 `fly sftp` 或 `fly ssh` 把本地 `web_results/leadfinder.db` 上传到 `/data/`。
示例：
```bash
fly ssh sftp shell --app lead-finder
# 然后 put leadfinder.db /data/leadfinder.db
```

**Q：免费实例会休眠吗？**  
A：如果一段时间没有访问，VM 会停止（auto_stop_machines = true）。下次访问时自动唤醒（auto_start_machines = true），冷启动约 2~5 秒。

**Q：我想换区域怎么办？**  
A：先删旧 Volume（会丢数据！），再在新区域创建：
```bash
fly volumes list --app lead-finder
fly volumes destroy <vol-id> --app lead-finder
fly volumes create leadfinder_data --region hkg --size 1 --app lead-finder
```
