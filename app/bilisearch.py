"""B 站视频检索 —— 走 web 端的 WBI 签名搜索接口。

不依赖登录：bilibili 的搜索接口要 w_rid 签名（WBI），导航接口会顺手给出
img/sub 两把 key，按固定表混出 32 位 mixin key，再对参数做 md5。表是从
当前页面 bundle 里核对过的（2026-10），nav 接口未登录也能拿到 wbi_img。

需要 buvid3/buvid4 才稳：先问 finger/spi 要一对。不带这两块 cookie 时
偶尔会吃到 -412 风控，所以 cfg.videopush.cookie 可以再补一条登录 cookie
（SESSDATA=...; bili_jct=...）。
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from .config import Config

log = logging.getLogger("companion.bilisearch")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_NAV = "https://api.bilibili.com/x/web-interface/nav"
_FINGER = "https://api.bilibili.com/x/frontend/finger/spi"
_SEARCH = "https://api.bilibili.com/x/web-interface/wbi/search/type"
# 旧版检索接口：不需要 WBI 签名。新版 wbi/search/type 偶尔会对无登录 cookie
# 的请求下发 v_voucher（验证码挑战，返回 code=0 但 result 为空），此时回退到这里。
_SEARCH_LEGACY = "https://api.bilibili.com/x/web-interface/search/type"
_WBI_TTL = 6 * 3600.0

# WBI mixin key 的固定置换表
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


class BiliSearchError(RuntimeError):
    """检索失败（网络、解析、返回结构不对）。"""


class BiliRiskError(BiliSearchError):
    """被 B 站风控拦了 —— 需要人工处理（换 IP / 补 cookie），不是重试能好的。"""


def parse_duration(text: Any) -> int:
    """把 B 站的时长字段换成秒。

    接口给的是「1242:13」（分钟:秒）或「1:02:03」（时:分:秒），
    不是秒数；也有少数情况直接给整数。
    """
    s = str(text or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    total = 0
    for part in s.split(":"):
        part = part.strip()
        if not part.isdigit():
            return total
        total = total * 60 + int(part)
    return total


def human_duration(seconds: int) -> str:
    s = max(0, int(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def human_play(n: int) -> str:
    n = int(n or 0)
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def clean_title(s: Any) -> str:
    """去掉搜索结果的 <em class="keyword"> 高亮标签并反转义。"""
    t = html.unescape(_TAG_RE.sub("", str(s or "")))
    return _SPACE_RE.sub(" ", t).strip()


@dataclass
class VideoHit:
    bvid: str
    title: str
    author: str = ""
    mid: int = 0
    play: int = 0
    duration: int = 0
    pubdate: int = 0
    desc: str = ""
    pic: str = ""
    keyword: str = ""

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"

    @property
    def duration_text(self) -> str:
        return human_duration(self.duration)

    @property
    def play_text(self) -> str:
        return human_play(self.play)

    @property
    def published(self) -> str:
        try:
            return datetime.fromtimestamp(int(self.pubdate)).strftime("%Y-%m-%d")
        except Exception:
            return ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bvid": self.bvid, "title": self.title, "author": self.author,
            "mid": self.mid, "play": self.play, "duration": self.duration,
            "pubdate": self.pubdate, "desc": self.desc, "pic": self.pic,
            "keyword": self.keyword,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VideoHit":
        return cls(
            bvid=str(d.get("bvid") or ""),
            title=str(d.get("title") or ""),
            author=str(d.get("author") or ""),
            mid=int(d.get("mid") or 0),
            play=int(d.get("play") or 0),
            duration=int(d.get("duration") or 0),
            pubdate=int(d.get("pubdate") or 0),
            desc=str(d.get("desc") or ""),
            pic=str(d.get("pic") or ""),
            keyword=str(d.get("keyword") or ""),
        )


class BiliSearch:
    def __init__(self, cfg: Config, *, timeout: float = 15.0):
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": _UA,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        self._mixin: str = ""
        self._mixin_at: float = 0.0
        self._cookie_ready = False

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    # ---------------- 基础设施 ----------------

    async def _bootstrap(self) -> None:
        """拿 buvid（防 -412）并补一条可选的登录 cookie。只做一次。"""
        if self._cookie_ready:
            return
        self._cookie_ready = True
        try:
            r = await self._client.get(_FINGER)
            data = (r.json() or {}).get("data") or {}
            for name, key in (("buvid3", "b_3"), ("buvid4", "b_4")):
                val = str(data.get(key) or "")
                if val:
                    self._client.cookies.set(name, val, domain=".bilibili.com")
        except Exception as e:  # 拿不到也能搜，只是更容易被风控
            log.debug("取 buvid 失败（继续）：%s", e)

        raw = (getattr(self.cfg.videopush, "cookie", "") or "").strip()
        for part in raw.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                self._client.cookies.set(k, v, domain=".bilibili.com")

    async def _ensure_mixin(self, force: bool = False) -> str:
        if not force and self._mixin and (time.time() - self._mixin_at) < _WBI_TTL:
            return self._mixin
        await self._bootstrap()
        r = await self._client.get(_NAV)
        data = (r.json() or {}).get("data") or {}
        img = str((data.get("wbi_img") or {}).get("img_url") or "")
        sub = str((data.get("wbi_img") or {}).get("sub_url") or "")
        img_key = img.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub.rsplit("/", 1)[-1].split(".")[0]
        if not img_key or not sub_key:
            raise BiliSearchError("拿不到 WBI 签名 key（nav 接口没返回 wbi_img）")
        orig = img_key + sub_key
        self._mixin = "".join(orig[i] for i in _MIXIN_TAB)[:32]
        self._mixin_at = time.time()
        return self._mixin

    def _sign(self, params: Dict[str, Any]) -> str:
        params = dict(params)
        params["wts"] = str(int(time.time()))
        items = []
        for k in sorted(params):
            v = "".join(ch for ch in str(params[k]) if ch not in "!'()*")
            items.append((k, v))
        query = urllib.parse.urlencode(items)
        mixin = self._mixin
        w_rid = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
        return f"{query}&w_rid={w_rid}"

    # ---------------- 搜索 ----------------

    async def search(self, keyword: str, *, pages: int = 1,
                     page_size: int = 30, order: str = "totalrank") -> List[VideoHit]:
        """按关键词搜视频。order: totalrank / click / pubdate / dm / stow。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        await self._ensure_mixin()
        out: List[VideoHit] = []
        for page in range(1, max(1, pages) + 1):
            body = await self._search_page(keyword, page, page_size, order)
            out.extend(body)
            if page < pages:
                import asyncio
                await asyncio.sleep(0.8)  # 连续翻页太快容易吃 -412
        return out

    async def _search_page(self, keyword: str, page: int, page_size: int,
                           order: str) -> List[VideoHit]:
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "page_size": min(int(page_size), 50),
            "order": order,
        }
        for attempt in (1, 2):
            qs = self._sign(params)
            try:
                r = await self._client.get(f"{_SEARCH}?{qs}")
            except httpx.HTTPError as e:
                if attempt == 1:
                    await self._sleep(1.5)
                    continue
                raise BiliSearchError(f"搜索「{keyword}」时网络出错：{e}") from e

            if r.status_code in (403, 412):
                if attempt == 1:
                    await self._ensure_mixin(force=True)
                    await self._sleep(2.0)
                    continue
                raise BiliRiskError(
                    f"搜索「{keyword}」被 B 站风控（HTTP {r.status_code}）。"
                    "先在浏览器打开一次 bilibili 看看要不要验证码；"
                    "还不行就在面板里补一条 videopush.cookie（SESSDATA=…）")
            try:
                data = r.json()
            except Exception as e:
                raise BiliSearchError(f"搜索接口返回的不是 JSON：{r.text[:200]}") from e

            code = int(data.get("code") or 0)
            if code == 0:
                body = data.get("data") or {}
                hits = self._parse_items(body.get("result") or [], keyword)
                if hits:
                    return hits
                # 有 result 键但空、且带 v_voucher：风控挑战（不是没结果）
                if body.get("v_voucher") or "result" not in body:
                    log.info("WBI 检索被挑战（v_voucher），回退旧接口：%s", keyword)
                    return await self._search_legacy(keyword, page, page_size, order)
                return []
            if code in (-403, -352):
                if attempt == 1:
                    await self._ensure_mixin(force=True)
                    await self._sleep(2.0)
                    continue
                raise BiliRiskError(f"B 站拒绝了搜索请求（code={code}）：{data.get('message')}")
            raise BiliSearchError(f"搜索「{keyword}」失败（code={code}）：{data.get('message')}")
        return []

    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio
        await asyncio.sleep(seconds)

    async def _search_legacy(self, keyword: str, page: int, page_size: int,
                             order: str) -> List[VideoHit]:
        """旧版 search/type：不需要 wbi 签名，风控挑战时兜底用。"""
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
            "page_size": min(int(page_size), 50),
            "order": order,
        }
        try:
            r = await self._client.get(_SEARCH_LEGACY, params=params)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise BiliSearchError(f"旧检索接口出错：{e}") from e
        body = data.get("data") or {}
        hits = self._parse_items(body.get("result") or [], keyword)
        if hits:
            log.info("旧检索接口可用：%s 返回 %d 条", keyword, len(hits))
            return hits
        raise BiliRiskError(
            "B 站检索被风控（新版接口要求验证码，旧接口也没返回结果）。"
            "请在面板 videopush.cookie 里填一条登录 Cookie（浏览器登录 B 站后"
            "复制 SESSDATA=... 即可），或稍后再试。")

    def _parse_items(self, items: Any, keyword: str) -> List[VideoHit]:
        out: List[VideoHit] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            bvid = str(it.get("bvid") or "").strip()
            if not bvid:
                continue
            try:
                mid = int(it.get("mid") or 0)
            except (TypeError, ValueError):
                mid = 0
            out.append(VideoHit(
                bvid=bvid,
                title=clean_title(it.get("title")),
                author=str(it.get("author") or ""),
                mid=mid,
                play=int(it.get("play") or 0),
                duration=parse_duration(it.get("duration")),
                pubdate=int(it.get("pubdate") or 0),
                desc=str(it.get("description") or ""),
                pic=str(it.get("pic") or ""),
                keyword=keyword,
            ))
        return out
