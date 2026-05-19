import json, os, re, sys, traceback
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import feedparser, requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# Load from .env (with defaults)
VAULT = Path(os.getenv("VAULT_PATH", "/Users/I543625/Documents/Obsidian Vault"))
MODEL = os.getenv("MODEL", "claude-haiku-4-5")
ANTHROPIC_NEWS     = os.getenv("ANTHROPIC_NEWS_URL",     "https://www.anthropic.com/news")
ANTHROPIC_RESEARCH = os.getenv("ANTHROPIC_RESEARCH_URL", "https://www.anthropic.com/research")
CLAUDE_BLOG        = os.getenv("CLAUDE_BLOG_URL",        "https://claude.com/blog")
OPENAI_RSS         = os.getenv("OPENAI_RSS_URL",         "https://openai.com/news/rss.xml")
OPENAI_RESEARCH    = os.getenv("OPENAI_RESEARCH_URL",    "https://openai.com/research/index")
OPENAI_DEV_BLOG    = os.getenv("OPENAI_DEV_BLOG_URL",    "https://developers.openai.com/blog")
STATE_FILE  = Path(os.getenv("STATE_FILE", "state.json"))
MAX_ARTICLES = int(os.getenv("MAX_ARTICLES_PER_SOURCE", "20"))
TIMEOUT     = int(os.getenv("REQUEST_TIMEOUT", "15"))
USER_AGENT  = os.getenv("USER_AGENT", "Magpie/0.1")
MONTHS = {m: i for i, m in enumerate(["January","February","March","April","May","June","July","August","September","October","November","December"], 1)}
DATE_RE = (r"(January|February|March|April|May|June|July|August|"
           r"September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})")
# source key → (vault subdir, display name for digest)
SOURCES = {
    "anthropic":          ("Anthropic",         "Anthropic"),
    "anthropic-research": ("Anthropic Research", "Anthropic Research"),
    "claude":             ("Claude",             "Claude Blog"),
    "openai":             ("OpenAI",             "OpenAI"),
    "openai-research":    ("OpenAI Research",    "OpenAI Research"),
    "openai-dev":         ("OpenAI Dev",         "OpenAI Dev"),
}

log = lambda lvl, msg: print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] [{lvl}] {msg}", flush=True)
def log_exc(msg):
    """ERROR 级别日志 + 完整堆栈，用于 except 块中。"""
    log("ERROR", msg)
    print(traceback.format_exc(), end="", flush=True)
sanitize = lambda s: re.sub(r'[/\\:*?"<>|]', "", s).strip()
load_state = lambda: json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"seen": {}}

def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.rename(STATE_FILE)

def fetch_page(url):
    """Returns (title, body_text) or ('', '') on failure."""
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        h1 = soup.find("h1")
        el = soup.find("article") or soup.find("main")
        return (h1.get_text(strip=True) if h1 else ""), (el.get_text("\n", strip=True) if el else "")
    except Exception as e:
        log("WARN", f"fetch failed {url}: {type(e).__name__}: {e}")
        return "", ""

def _scrape(index_url, prefix, base, source):
    """Generic scraper: fetches an HTML index page and yields article dicts."""
    arts, slugs = [], set()
    try:
        r = requests.get(index_url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        tip = prefix.rstrip("/").split("/")[-1]
        for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
            href = a["href"]
            slug = href.rstrip("/").split("/")[-1]
            if not href.startswith(prefix) or href.count("/") != prefix.count("/") or not slug or slug == tip or slug in slugs:
                continue
            slugs.add(slug)
            url = base + href
            title, content = fetch_page(url)
            title = title or slug.replace("-", " ").title()
            m = re.search(DATE_RE, content)
            today = datetime.now().strftime("%Y-%m-%d")
            published = f"{m.group(3)}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}" if m else today
            arts.append({"url": url, "title": title, "published": published,
                         "source": source, "content": content})
            if len(arts) >= MAX_ARTICLES:
                break
    except Exception as e:
        log_exc(f"fetch {source} failed: {e}")
    return arts

fetch_anthropic          = lambda: _scrape(ANTHROPIC_NEWS,     "/news/",     "https://www.anthropic.com", "anthropic")
fetch_anthropic_research = lambda: _scrape(ANTHROPIC_RESEARCH, "/research/", "https://www.anthropic.com", "anthropic-research")
fetch_claude_blog        = lambda: _scrape(CLAUDE_BLOG,        "/blog/",     "https://claude.com",        "claude")
# TODO: openai.com/research/index returns 403 — blocked, will log error and skip each run
fetch_openai_research    = lambda: _scrape(OPENAI_RESEARCH,    "/research/", "https://openai.com",        "openai-research")
fetch_openai_dev_blog    = lambda: _scrape(OPENAI_DEV_BLOG,    "/blog/",     "https://developers.openai.com", "openai-dev")

def fetch_openai():
    articles = []
    try:
        feed = feedparser.parse(OPENAI_RSS)
        if feed.bozo and not feed.entries:
            raise ValueError(feed.bozo_exception)
        for entry in feed.entries[:MAX_ARTICLES]:
            url = entry.get("link", "")
            if not url:
                continue
            pp = entry.get("published_parsed")
            published = datetime(*pp[:3]).strftime("%Y-%m-%d") if pp else datetime.now().strftime("%Y-%m-%d")
            _, content = fetch_page(url)
            articles.append({"url": url, "title": entry.get("title", "Untitled"),
                             "published": published, "source": "openai",
                             "content": content or entry.get("summary", "")})
    except Exception as e:
        log_exc(f"fetch_openai failed: {e}")
    return articles

class _DailyLimitReached(Exception):
    pass

def summarize(article, client):
    try:
        prompt = (Path("prompts/summarize.txt").read_text()
                  .replace("{source}", SOURCES[article["source"]][1])
                  .replace("{title}", article["title"])
                  .replace("{content}", article["content"][:12000]))
        text = client.messages.create(model=MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]).content[0].text
        parts = {}
        for chunk in re.split(r"---SECTION---", text):
            chunk = chunk.strip()
            if chunk.startswith("SUMMARY"):
                parts["summary"] = chunk[7:].strip()
            elif chunk.startswith("BULLETS"):
                parts["bullets"] = [l.lstrip("- ").strip()
                                     for l in chunk[7:].strip().splitlines()
                                     if l.strip().startswith("-")]
            elif chunk.startswith("TAGS"):
                parts["tags"] = re.findall(r"#\w[\w-]*", chunk)
        return parts if parts.get("summary") else None
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        log_exc(f"API auth/permission failure: {e}")
        sys.exit(1)
    except anthropic.RateLimitError as e:
        if "86400" in str(e) or "ByDay" in str(e):
            log("WARN", "Daily rate limit reached — stopping for today")
            raise _DailyLimitReached()
        log("WARN", f"Rate limited (per-minute), skipping '{article['title'][:50]}'")
        return None
    except Exception as e:
        # 单篇文章失败，跳过继续处理其他文章
        log_exc(f"summarize failed '{article['title'][:50]}': {e}")
        return None

def write_article(article, parsed, stem):
    src_dir, _ = SOURCES[article["source"]]
    out = VAULT / "Sources" / src_dir / f"{stem}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    tags = parsed.get("tags", []) + [f"source/{article['source']}"]
    bullets = "\n".join(f"- {b}" for b in parsed.get("bullets", []))
    out.write_text(
        f"---\nsource: {article['source']}\nurl: {article['url']}\n"
        f"title: {article['title']}\npublished: {article['published']}\n"
        f"fetched: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"tags: [{', '.join(tags)}]\nsummary_version: v1\n---\n\n"
        f"# {article['title']}\n\n## 摘要\n{parsed.get('summary','')}\n\n"
        f"## 要点\n{bullets}\n\n## 原文链接\n{article['url']}\n\n"
        f"## 原文正文\n{article['content'][:8000]}\n", encoding="utf-8")
    log("INFO", f"wrote Sources/{src_dir}/{stem}.md")

DIGEST_MAX_ARTICLES = 20   # digest 最多收录的文章数
DIGEST_SUMMARY_LEN  = 250  # 每篇摘要在 digest block 中的最大字符数
DIGEST_MAX_BULLETS  = 3    # 每篇最多取几条要点

def write_digest(done, today, client):
    # 限制 block 大小，避免超过模型 token 上限
    subset = done[:DIGEST_MAX_ARTICLES]
    block = "\n\n".join(
        f"标题: {d['article']['title']}\n来源: {SOURCES[d['article']['source']][1]}\n"
        f"摘要: {d['parsed'].get('summary','')[:DIGEST_SUMMARY_LEN]}\n要点:\n"
        + "\n".join(f"- {b}" for b in d['parsed'].get("bullets", [])[:DIGEST_MAX_BULLETS])
        for d in subset)
    try:
        prompt = Path("prompts/digest.txt").read_text().format(articles_block=block)
        text = client.messages.create(model=MODEL, max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]).content[0].text
        theme_m = re.search(r"---THEME---\s*(.+?)(?=---ARTICLES---|$)", text, re.DOTALL)
        theme = theme_m.group(1).strip() if theme_m else "今日无明显主线"
        arts_m = re.search(r"---ARTICLES---\s*(.+?)(?=---|$)", text, re.DOTALL)
        arts = arts_m.group(1).strip() if arts_m else ""
        display_pat = "|".join(re.escape(v[1]) for v in SOURCES.values())

        def fix_link(m):
            raw, display = m.group(1), m.group(2).strip()
            src_key = next((k for k, v in SOURCES.items() if v[1] == display), None)
            hit = next((d for d in done if d["article"]["source"] == src_key
                        and sanitize(d["article"]["title"])[:25] in sanitize(raw)[:35]), None)
            return f"### [[{hit['stem'] if hit else sanitize(raw)}]] · {display}"

        arts = re.sub(rf"### (.+?) · ({display_pat})", fix_link, arts)
        srcs = list({SOURCES[d["article"]["source"]][1] for d in subset})
        out = VAULT / "Daily" / f"{today}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            f"---\ndate: {today}\narticles: {len(done)}\nsources: [{', '.join(srcs)}]\n---\n\n"
            f"# {today} 早间简报\n\n## 今日主题\n{theme}\n\n## 文章\n\n{arts}\n",
            encoding="utf-8")
        log("INFO", f"wrote Daily/{today}.md")
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        log("ERROR", f"write_digest API auth failure: {e}")
        sys.exit(1)
    except Exception as e:
        log("ERROR", f"write_digest failed: {e}")
        sys.exit(1)   # digest 失败视为整次 run 失败

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not api_key:
        log("ERROR", "Neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)
    state, today = load_state(), datetime.now().strftime("%Y-%m-%d")
    articles = (fetch_anthropic() + fetch_anthropic_research() +
                fetch_claude_blog() + fetch_openai() + fetch_openai_research() +
                fetch_openai_dev_blog())
    log("INFO", f"fetched {len(articles)} articles total")
    done, attempted = [], 0
    try:
        for article in articles:
            url = article["url"]
            if url in state["seen"]:
                log("INFO", f"skip seen: {article['title'][:60]}")
                continue
            stem = f"{today} {sanitize(article['title'])}"
            src_dir, _ = SOURCES[article["source"]]
            if (VAULT / "Sources" / src_dir / f"{stem}.md").exists():
                log("INFO", f"file exists, skip: {stem}.md")
                state["seen"][url] = datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"
                save_state(state)
                continue
            log("INFO", f"summarizing: {article['title'][:60]}")
            attempted += 1
            parsed = summarize(article, client)
            if parsed is None:
                continue
            write_article(article, parsed, stem)
            state["seen"][url] = datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"
            save_state(state)
            done.append({"article": article, "parsed": parsed, "stem": stem})
    except _DailyLimitReached:
        log("INFO", f"daily limit reached — processed {len(done)} articles, rest will retry tomorrow")
    log("INFO", f"processed {len(done)} new articles")
    if attempted > 0 and len(done) == 0:
        # 有新文章尝试过摘要，但全部失败（非限额原因）
        log("ERROR", f"attempted {attempted} articles but all failed — check API status")
        sys.exit(1)
    if done:
        write_digest(done, today, client)
    else:
        log("INFO", "no new articles, skipping digest")

if __name__ == "__main__":
    main()
