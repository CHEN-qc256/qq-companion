"""联网搜索（P5）。

DeepSeek 的 API **本身没有联网搜索** —— 官网聊天里那个开关是产品功能，API 不开放。
官方给的正路是 Tool Calls（函数调用）：把「搜索」作为一个工具交给模型，
模型自己判断这一句要不要用。所以这里只负责「真正去搜」，要不要搜是模型决定的。

搜索源用免 key 的 DuckDuckGo HTML 端点，失败自动退回 Bing。
不引入任何新依赖 —— 只用已经装了的 httpx，解析用标准库的 re + html。

成本：工具定义挂在**静态前缀**上走缓存，平时一次都不调，等于 0。
真搜一次大约多一轮请求（输入走缓存 + 几百 token 输出），折合 0.001~0.003 元。

★ 这个模块**不 import app 里的任何东西**，便于单独测。
"""

from __future__ import annotations

import html as _html
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

log = logging.getLogger("companion.websearch")

# 装成浏览器，否则 DuckDuckGo / Bing 会返回反爬页面。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

_TIMEOUT = 12.0
_MAX_RESULTS = 8
_PAGE_CHARS = 2500


# ──────────────────────────────────────────────────────────────────────
#  给模型看的工具定义（OpenAI / DeepSeek 通用格式）
# ──────────────────────────────────────────────────────────────────────
TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索。只在需要最新消息或者你不知道的事实时才用："
                "新闻、天气、比赛比分、股价、某个东西现在的价格、你不确定的事实。"
                "日常闲聊、回忆你们聊过的事、说话本身，都不要调用这个工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，像你在搜索引擎里输入的那样",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "要几条结果，默认 5，最多 8",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "打开一个网页读正文。只有在搜索结果的摘要不够、"
                "确实需要原文细节时才调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "完整网址，必须以 http:// 或 https:// 开头",
                    },
                },
                "required": ["url"],
            },
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


# ──────────────────────────────────────────────────────────────────────
#  HTML 处理
# ──────────────────────────────────────────────────────────────────────
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_WS = re.compile(r"[ \t\u00a0]+")
_MANY_NL = re.compile(r"\n{3,}")


def strip_tags(s: str) -> str:
    """把一小段 HTML 变成纯文本。

    ★ 顺序要紧：**先剥标签，再还原实体**。反过来的话 `&lt;一&gt;` 会先变成
    `<一>`，再被当成标签删掉 —— 标题里就凭空少一段。
    """
    s = _TAG.sub("", s or "")
    s = _html.unescape(s)
    s = _WS.sub(" ", s)
    return s.strip()


def html_to_text(s: str) -> str:
    """把整页 HTML 变成可读正文。够用就行，不求还原排版。"""
    s = _SCRIPT.sub(" ", s or "")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", s, flags=re.I)
    s = _html.unescape(_TAG.sub(" ", s))
    s = _WS.sub(" ", s)
    s = re.sub(r" *\n *", "\n", s)
    return _MANY_NL.sub("\n\n", s).strip()


def unwrap_ddg(href: str) -> str:
    """DuckDuckGo 的结果链接是 //duckduckgo.com/l/?uddg=<编码后的真网址>，剥出来。"""
    if not href:
        return ""
    href = _html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href or "/l/?" in href:
        q = parse_qs(urlparse(href).query)
        if q.get("uddg"):
            return unquote(q["uddg"][0])
    return href


# ──────────────────────────────────────────────────────────────────────
#  两个搜索源
# ──────────────────────────────────────────────────────────────────────
_DDG_A = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.I | re.S)
_DDG_SNIP = re.compile(
    r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.I | re.S)

# DuckDuckGo 的 HTML 端点会把广告混在结果里，长这样：
#   //duckduckgo.com/y.js?ad_domain=foo.com&ad_provider=...
# 它的标题往往是「Chat With XXX Now」这种卖课的。留着它，模型会一本正经地
# 把广告当资料念给用户听 —— 所以在这里就丢掉。
_DDG_AD = re.compile(r"duckduckgo\.com/(y\.js|aclick)|[?&]ad_domain=", re.I)


def _is_ad(url: str) -> bool:
    return bool(_DDG_AD.search(url or ""))


def parse_ddg(page: str, limit: int) -> List[Dict[str, str]]:
    hits = _DDG_A.findall(page or "")
    snips = [strip_tags(x) for x in _DDG_SNIP.findall(page or "")]
    out: List[Dict[str, str]] = []
    for i, (href, title) in enumerate(hits):
        if len(out) >= limit:
            break
        url = unwrap_ddg(href)
        t = strip_tags(title)
        if not url or not t:
            continue
        # 只留能直接打开的绝对网址。相对链接（/foo）拿在手里没用，
        # 模型照着念出来用户也点不开。
        if not url.startswith(("http://", "https://")):
            continue
        if _is_ad(url):
            continue
        out.append({
            "title": t,
            "url": url,
            "snippet": snips[i] if i < len(snips) else "",
        })
    return out


_BING_BLOCK = re.compile(r'<li class="b_algo".*?</li>', re.I | re.S)
_BING_A = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_BING_P = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)


def parse_bing(page: str, limit: int) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for block in _BING_BLOCK.findall(page or "")[: limit * 2]:
        a = _BING_A.search(block)
        if not a:
            continue
        url = _html.unescape(a.group(1))
        title = strip_tags(a.group(2))
        if not url.startswith("http") or not title:
            continue
        if _is_ad(url):
            continue
        p = _BING_P.search(block)
        out.append({
            "title": title,
            "url": url,
            "snippet": strip_tags(p.group(1)) if p else "",
        })
        if len(out) >= limit:
            break
    return out


# ──────────────────────────────────────────────────────────────────────
#  对外的两个动作
# ──────────────────────────────────────────────────────────────────────
async def search(query: str, limit: int = 5) -> List[Dict[str, str]]:
    """搜一次。两个源都失败就返回空列表（不抛异常，让上层决定怎么跟模型说）。"""
    query = (query or "").strip()
    if not query:
        return []
    try:
        limit = max(1, min(int(limit or 5), _MAX_RESULTS))
    except (TypeError, ValueError):
        limit = 5

    headers = {
        "User-Agent": _UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml",
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cli:
        # 源一：DuckDuckGo 的纯 HTML 端点
        try:
            r = await cli.post("https://html.duckduckgo.com/html/",
                               data={"q": query}, headers=headers)
            r.raise_for_status()
            hits = parse_ddg(r.text, limit)
            if hits:
                log.info("搜索命中 %d 条（duckduckgo）：%s", len(hits), query[:40])
                return hits
            log.warning("duckduckgo 返回 0 条，退回 bing：%s", query[:40])
        except Exception as e:                      # noqa: BLE001
            log.warning("duckduckgo 搜索失败（%s），退回 bing：%s",
                        type(e).__name__, query[:40])

        # 源二：Bing
        try:
            r = await cli.get("https://www.bing.com/search",
                              params={"q": query, "setlang": "zh-CN"},
                              headers=headers)
            r.raise_for_status()
            hits = parse_bing(r.text, limit)
            if hits:
                log.info("搜索命中 %d 条（bing）：%s", len(hits), query[:40])
            return hits
        except Exception as e:                      # noqa: BLE001
            log.warning("bing 搜索也失败了（%s）：%s", type(e).__name__, query[:40])
            return []


async def fetch_page(url: str, max_chars: int = _PAGE_CHARS) -> str:
    """抓一个网页的正文。失败返回一句人话，不抛异常。"""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "网址不合法，必须以 http:// 或 https:// 开头。"
    headers = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cli:
            r = await cli.get(url, headers=headers)
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").lower()
            if "html" not in ctype and "text" not in ctype:
                return f"这个链接不是网页（{ctype or '未知类型'}），读不了。"
            text = html_to_text(r.text)
    except Exception as e:                          # noqa: BLE001
        return f"打不开这个网页（{type(e).__name__}）。"
    if not text:
        return "这个网页打开是空的，可能内容要靠脚本加载，读不到。"
    if len(text) > max_chars:
        text = text[:max_chars] + "……（后面还很长，先看到这）"
    return text


def format_hits(query: str, hits: List[Dict[str, str]]) -> str:
    """把搜索结果整理成给模型看的一小段文本。尽量短，省 token。"""
    if not hits:
        return f"搜索「{query}」没有结果（可能没网，或者搜索源改版了）。"
    lines = [f"搜索「{query}」的结果："]
    for i, h in enumerate(hits, 1):
        snip = h.get("snippet") or ""
        if len(snip) > 160:
            snip = snip[:160] + "…"
        lines.append(f"{i}. {h['title']}\n   {h['url']}\n   {snip}")
    lines.append("（这是搜索结果，不是你的记忆。挑跟问题相关的说，别整段念出来。）")
    return "\n".join(lines)


async def run_tool(name: str, args: Dict[str, Any]) -> str:
    """执行一次工具调用，返回要喂回模型的结果文本。"""
    args = args or {}
    if name == "web_search":
        q = str(args.get("query") or "").strip()
        if not q:
            return "没给搜索关键词。"
        hits = await search(q, args.get("limit") or 5)
        return format_hits(q, hits)
    if name == "fetch_page":
        return await fetch_page(str(args.get("url") or ""))
    return f"没有叫 {name} 的工具。"
