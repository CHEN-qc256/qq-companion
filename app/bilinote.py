"""BiliNote 笔记生成客户端。

BiliNote 是独立的后端（FastAPI，默认 8483），我们只走它公开的两个接口：
  POST /api/generate_note      {video_url, platform, quality, provider_id, model_name, style, ...}
  GET  /api/task_status/{tid}  轮询到 SUCCESS / FAILED

笔记生成这条链路很慢（下载 → 抽音轨 → whisper → LLM 改写，几分钟到十几
分钟），所以调用方要有耐心；VIDEOPUSH 里是串行一条条做的。

后端没起的时候可以替它拉起来（autostart.spawn，无窗口），复用 BiliNote
自己的 start-backend.ps1 里的环境变量。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

from .config import Config

log = logging.getLogger("companion.bilinote")

_POLL_SECONDS = 4.0


class BiliNoteError(RuntimeError):
    """BiliNote 不可用、任务失败或超时。"""


def python_path(vp) -> Optional[Path]:
    """BiliNote 用哪个解释器：配置优先，否则 <backend>/.venv/Scripts/python.exe。"""
    if vp.bilinote_python:
        p = Path(vp.bilinote_python)
        return p if p.is_file() else None
    root = Path(vp.bilinote_dir or "")
    if not str(root):
        return None
    if os.name == "nt":
        p = root / ".venv" / "Scripts" / "python.exe"
    else:
        p = root / ".venv" / "bin" / "python"
    return p if p.is_file() else None


def spawn_env() -> Dict[str, str]:
    """BiliNote 需要的环境变量（和它自己的 start-backend.ps1 一致）。"""
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "HF_ENDPOINT": "https://hf-mirror.com",
        "HF_HUB_OFFLINE": "1",
    })
    return env


class BiliNote:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = httpx.AsyncClient(timeout=30.0)
        self._spawned = False
        self._provider: Tuple[str, str] = ("", "")

    @property
    def base(self) -> str:
        # 每次现读，面板热更新 bilinote_url 后立刻生效
        return (self.cfg.videopush.bilinote_url or "").rstrip("/")

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    # ---------------- 可用性 ----------------

    async def health(self) -> Optional[Dict[str, Any]]:
        if not self.base:
            return None
        try:
            r = await self._client.get(f"{self.base}/api/sys_health", timeout=5.0)
            data = r.json()
        except Exception:
            return None
        if isinstance(data, dict) and int(data.get("code", -1)) == 0:
            return data.get("data") or {}
        return None

    def _python(self) -> Optional[Path]:
        return python_path(self.cfg.videopush)

    def _spawn(self) -> int:
        """把 BiliNote 后端拉起来（无窗口），返回 PID。只在没起时调用。"""
        from . import autostart

        vp = self.cfg.videopush
        root = Path(vp.bilinote_dir or "")
        py = self._python()
        if py is None:
            raise BiliNoteError(
                f"找不到 BiliNote 的 Python：{root / '.venv' / 'Scripts' / 'python.exe'}。"
                "请在面板里把 videopush.bilinote_dir 指到 BiliNote 的 backend 目录")
        if not (root / "main.py").is_file():
            raise BiliNoteError(f"{root} 里没有 main.py，videopush.bilinote_dir 指错了")

        log_path = self.cfg.db_path().parent / "bilinote.log"
        pid = autostart.spawn([str(py), "main.py"], cwd=root, log_path=log_path,
                              env=spawn_env(), hide=True)
        self._spawned = True
        log.info("已拉起 BiliNote 后端（PID %s，cwd=%s，日志 %s）", pid, root, log_path)
        return pid

    async def ensure(self, *, spawn: bool = True, timeout: float = 120.0) -> bool:
        if await self.health() is not None:
            return True
        if spawn:
            try:
                self._spawn()
            except BiliNoteError:
                raise
            except Exception as e:
                raise BiliNoteError(f"拉起 BiliNote 失败：{e}") from e
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            if await self.health() is not None:
                log.info("BiliNote 后端已就绪：%s", self.base)
                return True
        raise BiliNoteError(
            f"BiliNote 在 {int(timeout)} 秒内没起来（{self.base}/api/sys_health）。"
            "日志见 data/bilinote.log")

    # ---------------- 模型 ----------------

    async def resolve_model(self) -> Tuple[str, str]:
        """优先用配置里写死的；没写就问 BiliNote 当前启用的供应商和模型。"""
        vp = self.cfg.videopush
        if vp.provider_id and vp.model_name:
            return vp.provider_id, vp.model_name
        if self._provider[0] and self._provider[1]:
            return self._provider

        provider_id = vp.provider_id
        try:
            r = await self._client.get(f"{self.base}/api/get_all_providers")
            data = r.json()
            rows = (data.get("data") or []) if int(data.get("code", -1)) == 0 else []
            if isinstance(rows, dict):
                rows = rows.get("providers") or []
            if not provider_id:
                # BiliNote 把内置供应商全部标成 enabled，但只有真正填过 key 的
                # 能用；先挑有 key 的，都没有再退回第一个 enabled。
                usable = [r for r in rows if isinstance(r, dict) and r.get("enabled")
                          and str(r.get("api_key") or "").strip()]
                pool = usable or [r for r in rows
                                  if isinstance(r, dict) and r.get("enabled")]
                for row in pool:
                    provider_id = str(row.get("id") or row.get("name") or "")
                    break
        except Exception as e:
            log.debug("问 BiliNote 供应商失败：%s", e)

        model_name = vp.model_name
        if not model_name and provider_id:
            try:
                r = await self._client.get(f"{self.base}/api/model_list/{provider_id}")
                data = r.json()
                models = ((data.get("data") or {}).get("models") or []) \
                    if int(data.get("code", -1)) == 0 else []
                if models:
                    model_name = str(models[0].get("id") or "")
            except Exception as e:
                log.debug("问 BiliNote 模型失败：%s", e)

        if not provider_id:
            # BiliNote 的示例库默认就是 deepseek + deepseek-flash，兜底用这个
            provider_id = "deepseek"
        if not model_name:
            model_name = "deepseek-flash"
        self._provider = (provider_id, model_name)
        log.info("BiliNote 使用供应商=%s 模型=%s", provider_id, model_name)
        return self._provider

    # ---------------- 生成 ----------------

    async def generate(self, video_url: str, *, style: Optional[str] = None,
                       timeout: Optional[float] = None) -> Dict[str, Any]:
        """跑完一条笔记，返回 {task_id, markdown, meta}。失败抛 BiliNoteError。"""
        vp = self.cfg.videopush
        await self.ensure(spawn=bool(vp.bilinote_autostart))
        provider_id, model_name = await self.resolve_model()

        body: Dict[str, Any] = {
            "video_url": video_url,
            "platform": "bilibili",
            "quality": (vp.note_quality or "fast"),
            "screenshot": bool(vp.note_screenshot),
            "link": False,          # 链接我们自己拼在推送里，不用它再插一遍
            "model_name": model_name,
            "provider_id": provider_id,
            "format": [],
        }
        use_style = (style if style is not None else vp.note_style) or ""
        if use_style:
            body["style"] = use_style

        try:
            r = await self._client.post(f"{self.base}/api/generate_note", json=body)
            data = r.json()
        except Exception as e:
            raise BiliNoteError(f"提交笔记任务失败：{e}") from e
        if int(data.get("code", -1)) != 0:
            msg = data.get("msg") or data.get("message") or data
            hint = ""
            if int(data.get("code", 0)) == 300102:
                hint = "（BiliNote 的转写模型还没就绪，先打开它的设置页下载 whisper 模型）"
            raise BiliNoteError(f"BiliNote 拒绝生成：{msg}{hint}")
        task_id = str((data.get("data") or {}).get("task_id") or "")
        if not task_id:
            raise BiliNoteError(f"BiliNote 没返回 task_id：{data}")
        log.info("BiliNote 任务已提交：%s（%s）", task_id, video_url)

        wait = float(timeout if timeout is not None else (vp.note_timeout or 900))
        deadline = time.monotonic() + wait
        last = "PENDING"
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_SECONDS)
            try:
                r = await self._client.get(f"{self.base}/api/task_status/{task_id}")
                st = r.json()
            except Exception as e:
                log.debug("轮询 %s 失败（继续）：%s", task_id, e)
                continue
            if int(st.get("code", -1)) != 0:
                raise BiliNoteError(f"笔记任务失败：{st.get('msg') or st}")
            payload = st.get("data") or {}
            last = str(payload.get("status") or "")
            if last == "SUCCESS":
                result = payload.get("result") or {}
                markdown = str(result.get("markdown") or "")
                if not markdown:
                    raise BiliNoteError(f"任务 {task_id} 成功了但没有 markdown")
                meta = result.get("audio_meta") or {}
                log.info("BiliNote 任务完成：%s（%d 字）", task_id, len(markdown))
                return {"task_id": task_id, "markdown": markdown, "meta": meta}
            if last == "FAILED":
                raise BiliNoteError(f"笔记任务失败：{payload.get('message') or '未知原因'}")
        raise BiliNoteError(f"笔记任务超时（{int(wait)} 秒，最后状态 {last}）")
