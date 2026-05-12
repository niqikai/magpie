# Magpie

每天早上从 Anthropic 和 OpenAI 抓取新文章,用 Claude 生成中文摘要,写入 Obsidian vault。

## 安装

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env,填入你的 ANTHROPIC_API_KEY
```

## 配置

编辑 `config.json` 自定义：
- `vault_path` — Obsidian vault 路径
- `model` — 使用的 Claude 模型
- `anthropic_news_url` — Anthropic 新闻页面 URL
- `openai_rss_url` — OpenAI RSS 源 URL
- `max_articles_per_source` — 每个源最多处理的文章数
- 等等

## 手动运行

```bash
python main.py
```

## 挂 macOS cron(每天 7:30 运行)

```bash
crontab -e
```

添加(替换路径为你的实际路径):

```
30 7 * * * cd /Users/I543625/Documents/Claude/Projects/Magpie && /usr/bin/python3 main.py >> run.log 2>&1
```

建议用绝对路径指向你的 Python 虚拟环境:

```
30 7 * * * cd /Users/I543625/Documents/Claude/Projects/Magpie && /path/to/venv/bin/python main.py >> run.log 2>&1
```

## 查看日志

```bash
tail -f run.log
```

## 输出结构

```
Obsidian Vault/
├── Sources/
│   ├── Claude/
│   │   └── 2026-05-12 文章标题.md
│   └── OpenAI/
│       └── 2026-05-12 文章标题.md
└── Daily/
    └── 2026-05-12.md      ← 早间简报
```

## 已知限制(Phase 0)

- Anthropic 无 RSS,通过抓取 `/news` 页面获取文章链接
- 不处理付费墙内容
- 不处理 PDF / 视频 / 播客
- 不支持多 vault 或自定义输出路径
- 无 retry,API 失败则跳过该篇(下次重试)
- 无 web UI,纯命令行
