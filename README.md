# B2B Lead Finder

通过 Google 搜索关键词，自动发现目标公司的**决策者真实邮箱**（过滤掉 info@ / support@ 等通用邮箱），支持邮箱有效性验证，最终导出 CSV 用于邮件营销。

---

## 功能特点

- **Google 搜索**：通过 SerpAPI 稳定获取搜索结果（不会被反爬封 IP）
- **域名提取**：自动去重，获取前 N 页的所有独立域名
- **邮箱发现**：通过 Hunter.io 查找每个域名下的公开邮箱
- **智能过滤**：自动过滤 50+ 种通用邮箱前缀（info, support, sales, contact, hello, admin...）
- **个人邮箱识别**：通过 Hunter.io 的 `personal` 标签 + 正则模式识别真实人名邮箱
- **邮箱验证**（可选）：通过 ZeroBounce 验证邮箱是否真实可达
- **CSV 导出**：包含邮箱、姓名、职位、部门、公司、置信度等字段

---

## 环境准备

### 1. 安装 Python 依赖

```bash
cd lead-finder
pip install -r requirements.txt
```

### 2. 注册 API 账号

| 服务 | 注册地址 | 免费额度 | 付费起步 |
|------|----------|----------|----------|
| **SerpAPI** | https://serpapi.com | 100 次/月 | $50/月 |
| **Hunter.io** | https://hunter.io/api | 25 次/月 | $49/月 (500 次) |
| **ZeroBounce** | https://www.zerobounce.net | 100 次/月 | $16/月 |

> **建议**：先用免费额度测试，确认数据质量后再付费。

### 3. 配置 API Key

复制模板文件并填入你的 API Key：

```bash
cp config.yaml config.yaml.bak  # 备份模板
# 编辑 config.yaml，替换 YOUR_xxx_KEY_HERE 为真实 key
```

`config.yaml` 格式：

```yaml
serpapi_key: "your_serpapi_key"
hunter_key: "your_hunter_key"
zerobounce_key: "your_zerobounce_key"  # 可选
```

---

## 使用方法

### 基础搜索

```bash
python lead_finder.py "football gloves manufacturer" --pages 5 --output leads.csv
```

参数说明：
- `keyword`：搜索关键词（必填）
- `--pages`：搜索 Google 前几页结果（默认 5 页，约 50 个结果）
- `--output`：输出 CSV 文件路径（默认 `leads.csv`）
- `--validate`：启用 ZeroBounce 邮箱验证
- `--max-domains`：限制处理的域名数量（测试时用）

### 启用邮箱验证

```bash
python lead_finder.py "football equipment distributor" --pages 10 --validate --output verified_leads.csv
```

### 快速测试（只处理前 5 个域名）

```bash
python lead_finder.py "sports gear wholesale" --pages 2 --max-domains 5
```

---

## 输出 CSV 字段说明

| 字段 | 说明 |
|------|------|
| `email` | 邮箱地址 |
| `first_name` | 名字（Hunter 提供） |
| `last_name` | 姓氏（Hunter 提供） |
| `position` | 职位（如 CEO, Sales Manager） |
| `department` | 部门 |
| `company` | 公司名 |
| `domain` | 网站域名 |
| `confidence_score` | Hunter 置信度（0-100，越高越可靠） |
| `email_type` | `personal`（个人）或 `generic`（通用） |
| `validation_status` | ZeroBounce 验证结果：`valid` / `invalid` / `catch-all` / `unknown` |
| `sources` | 数据来源 |
| `search_keyword` | 搜索关键词 |
| `found_at` | 发现时间 |

---

## 工作原理

```
关键词搜索 (SerpAPI)
    ↓
提取前 N 页的唯一域名
    ↓
对每个域名调用 Hunter.io Domain Search
    ↓
过滤通用邮箱 (info@, support@, sales@...)
    ↓
保留个人邮箱（first.last@, first_last@, Hunter personal 标签）
    ↓
可选：ZeroBounce 验证邮箱是否真实可达
    ↓
去重 → 导出 CSV
```

### 过滤逻辑

工具会**自动排除**以下前缀的邮箱：

```
info, support, sales, contact, hello, admin, noreply,
marketing, help, webmaster, office, service, team, general,
hr, press, media, careers, jobs, abuse, legal, privacy,
security, billing, finance, accounting, orders, feedback,
newsletter, subscribe, unsubscribe, postmaster, hostmaster,
root, www, ftp, mail, email, customerservice, enquiries,
inquiry, request, quote, estimates, reservations, booking,
complaints, returns, shipping, logistics, procurement,
purchasing, buyer, vendors, partners, affiliates, advertising,
events, sponsorship, donations, pr, communications,
community, social, content, editorial, web, it, tech,
systems, network, operations, facilities, maintenance,
reception, frontdesk, concierge, info2, sales1, sales2,
contactus, reachus, getintouch, talktous, askus, questions,
fag, helpdesk, customersuccess, client, business, corporate,
enterprise, wholesale, distributor, retail, store, shop
```

---

## API 费用估算

| 场景 | SerpAPI | Hunter.io | ZeroBounce | 总费用/月 |
|------|---------|-----------|------------|-----------|
| 小规模测试（5 页 × 10 关键词） | 50 次 | 50 次 | 0 | ~$0（免费额度内） |
| 中等规模（10 页 × 50 关键词） | 500 次 | 200 次 | 200 次 | ~$99 |
| 大规模（20 页 × 200 关键词） | 4000 次 | 1000 次 | 1000 次 | ~$200-300 |

> 实际费用取决于你搜索的关键词数量和每页域名密度。

---

## 提高命中率的技巧

1. **关键词要精准**：
   - ❌ `football`（太宽泛）
   - ✅ `football gloves manufacturer USA`（精准）
   - ✅ `American football equipment distributor Europe`（带地域）

2. **多维度搜索**：
   - 按行业：`sports gear wholesale`, `athletic equipment supplier`
   - 按职位：`procurement manager sports`, `buyer football equipment`
   - 按地域：`football gloves manufacturer Germany`, `NFL gear supplier`

3. **结合 Hunter.io Email Finder**：
   如果你知道目标公司的某个人名，可以直接在 Hunter.io 网页版用 Email Finder 功能猜邮箱格式。

4. **LinkedIn 辅助**：
   先用这个工具拿到域名列表，再去 LinkedIn 搜索 `"company name" + "procurement"` 找具体决策人。

---

## GDPR / 合规建议（针对欧美客户）

### 法律风险

在欧盟和英国，**邮箱属于个人数据**，受 GDPR 约束。在美国，受 CAN-SPAM 约束。

### 合规操作建议

1. **只联系企业邮箱**（`name@company.com`）
   - 避免抓取私人邮箱（Gmail, Yahoo, Outlook 等）
   - 本工具默认只处理企业域名邮箱

2. **发送邮件时必须包含**：
   - 清晰的发件人身份和公司信息
   - 真实的物理邮寄地址
   - **一键退订链接**（Unsubscribe）
   - 说明如何删除个人数据

3. **冷邮件（Cold Email）最佳实践**：
   - 每封邮件个性化，不要群发相同模板
   - 主题行诚实，不要误导
   - 提供真实价值，不要纯广告
   - 退订请求必须在 10 个工作日内处理

4. **数据存储**：
   - 不要无限期保留未回复的联系人数据
   - 建议设置数据保留期限（如 12 个月）
   - 收到删除请求后彻底删除

5. **不要使用 purchased lists**：
   - 本工具是发现**公开信息**，不属于购买列表
   - 但仍需确保邮件内容合规

### 建议的邮件开头模板

```
Subject: Quick question about [Company Name]'s football glove lineup

Hi [First Name],

I came across [Company Name] while researching football equipment 
suppliers in [Region]. I'm reaching out because...

[1-2 sentences about what you offer and why it's relevant to them]

If this isn't the right person to talk to, could you point me in 
the right direction?

Best,
[Your Name]
[Your Company]
[Phone]

---
If you'd prefer not to receive emails from me, just reply with 
"unsubscribe" and I'll remove you immediately.
```

---

## 常见问题

**Q: 为什么找不到邮箱？**
A: 不是所有公司都会把邮箱公开在网上。Hunter.io 的数据来自网页抓取和公开来源聚合。命中率通常在 20-40%。

**Q: 置信度低于多少应该丢弃？**
A: 工具默认保留 Hunter 标记为 `personal` 的邮箱，或置信度 >= 50 的邮箱。你可以手动过滤 CSV，只保留 `confidence_score >= 80` 的。

**Q: 可以绕过 API 自己爬吗？**
A: 技术上可以，但 Google 和 Hunter.io 都有反爬机制，而且数据质量远不如 API。付费 API 的时间成本远低于自己维护爬虫。

**Q: 一天能跑多少关键词？**
A: 取决于 API 配额。SerpAPI 付费版每秒可发多次请求。Hunter.io 有速率限制，工具已内置 0.7 秒延迟。

---

## 文件结构

```
lead-finder/
├── lead_finder.py      # 主程序
├── config.yaml         # API 配置（自己填写）
├── requirements.txt    # Python 依赖
├── README.md           # 本文档
└── leads.csv           # 输出文件（运行时生成）
```

---

## 进阶：定时自动跑批

如果你想每周自动跑一批关键词，可以用 `cron`（Linux/Mac）或 Windows 任务计划程序：

```bash
# 每周一早上 9 点跑
0 9 * * 1 cd /path/to/lead-finder && python lead_finder.py "football gloves Europe" --pages 10 --output "leads_$(date +\%Y\%m\%d).csv"
```

---

## Claude Code Skill 与 MCP Server

除了网站和命令行，本项目还提供两种 Skill 形态，方便在 Claude 中直接调用。

### Claude Code Skill

`.claude/skills/lead-finder/` 已包含 Skill 定义。在 Claude Code 中打开本项目后即可使用：

```
/lead-finder "football gloves manufacturer" --pages 5 --output leads.csv
/lead-finder --domains "anthropic.com,openai.com" --output batch.csv
```

更多用法见 `.claude/skills/lead-finder/README.md`。

### MCP Server

也提供了 MCP Server，可在 Claude Desktop、Cursor、VS Code 等支持 MCP 的客户端中使用：

```bash
# 安装
pip install -e .

# 启动（stdio 模式，由客户端调用）
lead-finder-mcp
```

客户端配置示例：

```json
{
  "mcpServers": {
    "lead-finder": {
      "command": "lead-finder-mcp",
      "env": {
        "HUNTER_KEY": "your_hunter_key"
      }
    }
  }
}
```

暴露的工具包括 `search_leads`、`batch_domains`、`apollo_search`、`google_maps_search`、`supplier_portal_scan`、`validate_email`。

---

## 技术支持

- SerpAPI 文档：https://serpapi.com/search-api
- Hunter.io API 文档：https://hunter.io/api/docs
- ZeroBounce API 文档：https://www.zerobounce.net/docs/api/
