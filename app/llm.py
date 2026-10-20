"""DeepSeek 客户端。

三个"省钱"要点都焊死在这里，调用方绕不过去：
  1. 默认显式关闭思考模式（DeepSeek 默认是开的，且 effort=high，思维链按输出价计费）
  2. 强制 max_tokens 上限（输出是最贵的 token）
  3. 每次请求都把 prompt_cache_hit_tokens / prompt_cache_miss_tokens 记账，
     没有这个数字你永远不知道钱花在哪
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from .config import Config
from .store import Store

log = logging.getLogger("companion.llm")


@dataclass
class ChatResult:
    text: str
    cache_hit: int = 0
    cache_miss: int = 0
    output_tokens: int = 0
    cost_yuan: float = 0.0
    latency_ms: int = 0
    model: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    # 模型要求调用的工具（P5 联网搜索）。为空表示这一轮就是最终回复。
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    # 带工具调用那轮的原样 assistant 消息，下一轮要原封不动放回 messages 里，
    # 否则 OpenAI 兼容接口会因为 tool 消息找不到对应的 tool_calls 而报错。
    assistant_message: Dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_tokens(self) -> int:
        return self.cache_hit + self.cache_miss

    @property
    def hit_rate(self) -> float:
        total = self.prompt_tokens
        return (self.cache_hit / total) if total else 0.0


class DeepSeek:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self._client = httpx.AsyncClient(
            base_url=cfg.llm.base_url.rstrip("/"),
            timeout=httpx.Timeout(cfg.llm.timeout, connect=15.0),
            headers={
                "Authorization": f"Bearer {cfg.llm.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        purpose: str = "chat",
        session_id: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        thinking: Optional[bool] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> ChatResult:
        max_tokens = int(max_tokens if max_tokens is not None else self.cfg.llm.max_tokens)
        temperature = self.cfg.llm.temperature if temperature is None else temperature
        thinking = self.cfg.llm.thinking if thinking is None else thinking

        if self.cfg.llm.mock:
            return await self._mock(messages, purpose, session_id, max_tokens)

        body: Dict[str, Any] = {
            "model": self.cfg.llm.model,
            "messages": messages,
            "max_tokens": max_tokens,
            # ★ 显式关闭思考模式。OpenAI 兼容接口需要用 extra_body 传 thinking，
            #   这里直接写在 body 顶层（DeepSeek 的 HTTP 接口就是这个位置）。
            "thinking": {"type": "enabled" if thinking else "disabled"},
            "stream": False,
        }
        if not thinking:
            # 思考模式下这两个参数会被忽略，非思考模式才有意义
            body["temperature"] = temperature
        if thinking and reasoning_effort:
            # 思考档位只对思考模式有效：low / high / max。
            # 聊天那条路不开思考，所以永远走不到这里。
            body["reasoning_effort"] = reasoning_effort
        if tools:
            # ★ 工具定义是**常量**，跟着 messages 一起进缓存前缀，平时几乎不花钱。
            body["tools"] = tools
            body["tool_choice"] = "auto"

        t0 = time.perf_counter()
        try:
            resp = await self._client.post("/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:500] if e.response is not None else ""
            log.error("LLM HTTP %s: %s", e.response.status_code if e.response else "?", detail)
            raise
        latency_ms = int((time.perf_counter() - t0) * 1000)

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = (msg.get("content") or "").strip()
        tool_calls = msg.get("tool_calls") or []
        usage = data.get("usage") or {}

        cache_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        cache_miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        if not cache_hit and not cache_miss:
            # 老响应格式兜底
            cache_miss = int(usage.get("prompt_tokens") or 0)
        out_tokens = int(usage.get("completion_tokens") or 0)

        cost = self.cfg.pricing.cost(self.cfg.llm.model, cache_hit, cache_miss, out_tokens)

        await self.store.add_usage(
            session_id=session_id, model=self.cfg.llm.model, purpose=purpose,
            cache_hit=cache_hit, cache_miss=cache_miss, output_tokens=out_tokens,
            cost=cost, latency_ms=latency_ms,
        )

        log.info(
            "llm[%s] hit=%d miss=%d out=%d 命中率=%.0f%% %.4f元 %dms%s",
            purpose, cache_hit, cache_miss, out_tokens,
            (cache_hit / (cache_hit + cache_miss) * 100) if (cache_hit + cache_miss) else 0.0,
            cost, latency_ms,
            f" 工具={','.join(t.get('function', {}).get('name', '?') for t in tool_calls)}"
            if tool_calls else "",
        )

        return ChatResult(
            text=text, cache_hit=cache_hit, cache_miss=cache_miss,
            output_tokens=out_tokens, cost_yuan=cost, latency_ms=latency_ms,
            model=self.cfg.llm.model, raw=data,
            tool_calls=tool_calls,
            assistant_message=msg if tool_calls else {},
        )

    # ------------------------------------------------------------------
    # mock 模式：不联网也能把整条链路跑通，并且模拟"缓存基本命中"的用量，
    # 用来验证记账和报表是否正确。
    # ------------------------------------------------------------------
    async def _mock(self, messages, purpose, session_id, max_tokens) -> ChatResult:
        await asyncio.sleep(0.05)
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = str(m.get("content") or "")
                break
        prefix_chars = sum(len(str(m.get("content") or "")) for m in messages[:-1])
        cache_hit = int(prefix_chars * 0.6)
        cache_miss = int(len(last_user) * 0.6)
        if purpose == "chat":
            # 把他说的话回显出来，这样 mock 模式下肉眼就能确认
            # "哪条消息配了哪条回复"，以及拆条、延迟有没有生效。
            # 注意要切掉后面的动态块（【刚好想起来】/主动搭话那些），
            # 否则回显里会混进记忆内容，看着像回复说胡话了。
            said = last_user.split("【他刚说】")[-1].split("【群里】")[-1]
            for mark in ("【刚好想起来】", "【当前】", "【他没说话】", "【可以提的旧事】"):
                said = said.split(mark)[0]
            said = said.strip().replace("\n", " ")[:14]
            text = f"嗯，{said}啊，我也这么觉得" if said else "在的，咋啦"
        elif purpose == "memory":
            # 假的抽取结果，让离线链路能真的走完"写进长期记忆"这一段
            text = ("事实=他在折腾一个QQ陪聊机器人|关键词=机器人,QQ,项目,陪聊|重要度=4\n"
                    "事实=他最近工作很忙总在加班|关键词=工作,忙,加班,累|重要度=3")
        elif purpose == "proactive":
            text = "你那个机器人搞到哪一步了"
        elif purpose == "summarize":
            text = "他最近在折腾 QQ 机器人，之前提过工作有点忙。"
        else:
            text = "嗯嗯"
        out_tokens = max(1, int(len(text) * 0.6))
        cost = self.cfg.pricing.cost(self.cfg.llm.model, cache_hit, cache_miss, out_tokens)
        await self.store.add_usage(
            session_id=session_id, model=f"{self.cfg.llm.model}(mock)", purpose=purpose,
            cache_hit=cache_hit, cache_miss=cache_miss, output_tokens=out_tokens,
            cost=cost, latency_ms=50,
        )
        log.info("[MOCK] llm[%s] hit=%d miss=%d out=%d cost=%.5f",
                 purpose, cache_hit, cache_miss, out_tokens, cost)
        return ChatResult(text=text, cache_hit=cache_hit, cache_miss=cache_miss,
                          output_tokens=out_tokens, cost_yuan=cost, latency_ms=50,
                          model=f"{self.cfg.llm.model}(mock)")
