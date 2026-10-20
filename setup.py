"""安装向导：问几个问题，把 config.yaml 写好。

    python setup.py                      # 交互式，一路回车也能过
    python setup.py --yes                # 全用默认值，一个都不问
    python setup.py --api-key sk-xxx --name 遥遥 --budget 2 --force

它只做三件事：
    1. 检查依赖（缺了帮你装）
    2. 问你的 DeepSeek Key、人设名、日预算、端口 → 写 config.yaml（注释全部保留）
    3. 告诉你 NapCat 那一步怎么点，最后跑一遍体检

它不会：启动程序、连 QQ、改你的 QQ 设置。config.yaml 已存在时会先备份成 .bak。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import app  # noqa: F401,E402  —— 触发 app/__init__.py 里的 UTF-8 输出修复
# 中文版 Windows 的控制台默认 GBK，脚本里打一个 ✓ 就会 UnicodeEncodeError 崩掉。
# app 包在 import 时已经把 stdout/stderr 切成 utf-8，所以哪怕只用 setup.py 也得先 import 它。

TEMPLATE = ROOT / "config.example.yaml"
TARGET = ROOT / "config.yaml"
REQUIREMENTS = ROOT / "requirements.txt"
PERSONA_DIR = ROOT / "persona"
DEFAULT_PERSONA_FILE = PERSONA_DIR / "default.md"

DEFAULTS = {"name": "遥遥", "budget": "2.0", "port": "3001", "mode": "reverse"}
SK_PREFIX = "sk-"


# ────────────────────────── 小工具 ──────────────────────────

def head(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 44 - len(t)))


def ask(question: str, default: str = "", *, secret: bool = False) -> str:
    """问一句。读不到输入（管道 / Ctrl+Z）就退回默认值。

    关于 secret：**故意不用 getpass**。Windows 上 getpass 走 msvcrt.getwch()，
    只要标准输入不是一块真正的控制台（管道、ConPTY、Git Bash/mintty、
    被重定向、某些终端模拟器），getwch() 不会返回，整个向导就**卡死在那儿**
    —— 现象就是「输不进去」。实测会直接挂住。
    而这个 key 反正会以明文写进 config.yaml，遮罩只是心理安慰，
    不值当拿「向导能不能用」去换。想不把 key 敲在命令行里就用：
        $env:DEEPSEEK_API_KEY = "sk-xxx"  →  python setup.py --yes --force
    """
    tip = f"（回车 = {default}）" if default else ""
    if secret and not sys.stdin.isatty():
        print("  ! 标准输入不是终端（管道/重定向），下面直接读一行。")
    try:
        return input(f"  {question}{tip}：").strip() or default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def yes(question: str, default: bool = True) -> bool:
    tip = "Y/n" if default else "y/N"
    try:
        raw = input(f"  {question} [{tip}] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not raw:
        return default
    return raw in ("y", "yes", "是", "好", "1")


def run(cmd: list[str]) -> int:
    print(f"  $ {' '.join(cmd)}")
    try:
        return subprocess.run(cmd, cwd=str(ROOT)).returncode
    except FileNotFoundError:
        return 127


# ────────────────────────── 各步骤 ──────────────────────────

def step_deps(args) -> bool:
    head("依赖")
    missing = []
    for mod in ("httpx", "websockets", "yaml"):
        try:
            __import__(mod)
            print(f"  ✓ {mod}")
        except ImportError:
            missing.append(mod)
            print(f"  ✗ {mod} 没装")
    if not missing:
        return True
    if args.yes or yes("要现在帮你装吗？", True):
        if run([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)]) != 0:
            print("  装失败了。手动来一次：python -m pip install -r requirements.txt")
            return False
        print("  装好了。")
        return True
    print("  那先装依赖再回来：python -m pip install -r requirements.txt")
    return False


def step_key(args, current: str) -> str:
    head("DeepSeek API Key")
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if args.api_key:
        key = args.api_key.strip()
    elif args.yes:
        key = current or env
        print(f"  （--yes，用{'环境变量' if env and not current else '现有配置'}里的："
              f"{key[:6]}…）" if key else "  （--yes 且没有 key，先留空，之后手填）")
        return key
    else:
        if env:
            print(f"  环境变量 DEEPSEEK_API_KEY 已经有了（{env[:6]}…），直接回车用它就行。")
        print("  去 https://platform.deepseek.com/api_keys 建一个，长这样 sk-xxxxxxxx")
        print("  （粘的时候屏幕上看得见 —— 它反正会明文写进 config.yaml）")
        key = ask("粘进来", current or env, secret=True)
        if key and not key.startswith(SK_PREFIX):
            print(f"  ! 看着不像 DeepSeek 的 key（一般以 {SK_PREFIX} 开头）。先按你给的存。")
    return key


def verify_key(key: str, base_url: str = "https://api.deepseek.com",
               assume_yes: bool = False) -> None:
    """拿 key 拉一下模型列表。不产生 token，纯验证 key 有没有效。"""
    if not key:
        return
    if not assume_yes and not yes("  现在测一下这个 key 能不能用？（不花钱）", True):
        return
    try:
        import httpx
        r = httpx.get(f"{base_url.rstrip('/')}/models", timeout=15.0,
                      headers={"Authorization": f"Bearer {key}"})
    except Exception as e:
        print(f"  ! 测不了（网络问题？）：{e}")
        return
    if r.status_code == 200:
        try:
            names = [m.get("id") for m in (r.json().get("data") or [])]
        except Exception:
            names = []
        print(f"  ✓ key 有效（可用模型：{', '.join(names) if names else '（未列出）'}）")
    elif r.status_code in (401, 403):
        print(f"  ✗ key 被拒了（HTTP {r.status_code}）。检查一下是不是复制少了字符。")
    else:
        print(f"  ! 返回 HTTP {r.status_code}：{r.text[:160]}")


def step_misc(args) -> dict:
    head("最后几个选择（全都直接回车也行）")
    if args.yes:
        out = dict(DEFAULTS)
        out["name"] = args.name or DEFAULTS["name"]
        out["budget"] = str(args.budget or DEFAULTS["budget"])
        out["port"] = str(args.port or DEFAULTS["port"])
        out["mode"] = args.mode or DEFAULTS["mode"]
        print(f"  （--yes：人设名={out['name']}，日预算={out['budget']} 元，"
              f"{out['mode']} 模式，端口 {out['port']}）")
        return out

    name = ask("它叫什么名字（群里被叫到这个名会回）", args.name or DEFAULTS["name"])
    budget = ask("每天最多花多少钱（超过自动转省电模式，单位：元）",
                 str(args.budget or DEFAULTS["budget"]))
    print("  接入方式：reverse = 程序监听端口等 NapCat 连（推荐）")
    print("            forward = 程序去连 NapCat")
    mode = ask("选哪个", args.mode or DEFAULTS["mode"])
    port = ask("程序监听/连接的端口", str(args.port or DEFAULTS["port"]))
    return {"name": name, "budget": budget, "port": port, "mode": mode}


def step_write(args, key: str, opts: dict) -> bool:
    head("写 config.yaml")
    if not TEMPLATE.exists():
        print(f"  ✗ 找不到模板 {TEMPLATE.name}，项目文件不完整。")
        return False

    if TARGET.exists() and not args.force:
        if args.yes or not yes(f"  {TARGET.name} 已存在，要覆盖吗？（会先备份成 .bak）", False):
            print("  保留原配置，什么都不改。")
            print(f"  要改的话直接编辑 {TARGET.name}，或者重新跑：python setup.py --force")
            return True
    if TARGET.exists():
        shutil.copy2(TARGET, TARGET.with_suffix(".yaml.bak"))
        print(f"  原配置备份到 {TARGET.name}.bak")

    text = TEMPLATE.read_text(encoding="utf-8")
    subs = [
        # llm.api_key
        (r'(?m)^(\s*api_key:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{key}"'),
        # llm.mock 在向导阶段一律先关掉
        (r'(?m)^(\s*mock:\s*)(true|false)', lambda m: f'{m.group(1)}false'),
        # persona.name
        (r'(?m)^(\s*name:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{opts["name"]}"'),
        # budget.daily_yuan
        (r'(?m)^(\s*daily_yuan:\s*)[\d.]+',
         lambda m: f'{m.group(1)}{opts["budget"]}'),
        # onebot.mode / port
        (r'(?m)^(\s*mode:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{opts["mode"]}"'),
        (r'(?m)^(\s*port:\s*)\d+', lambda m: f'{m.group(1)}{opts["port"]}'),
    ]
    for pat, rep in subs:
        text, n = re.subn(pat, rep, text, count=1)
        if n == 0:
            print(f"  ! 模板里没找到 {pat!r}，跳过")

    # 语音运行时：发布包里它和项目并排摆着（../GPT-SoVITS-deploy）。
    # 找到就写绝对路径 —— 比相对路径更直观，面板里看得到实际位置；
    # 没找到就保留模板里的相对路径，用户之后自己放好也能用。
    deploy = (ROOT.parent / "GPT-SoVITS-deploy").resolve()
    if (deploy / "app").is_dir():
        text, n = re.subn(r'(?m)^(\s*install_dir:\s*)"[^"]*"',
                          lambda m: f'{m.group(1)}"{deploy.as_posix()}"',
                          text, count=1)
        if n:
            print(f"  ✓ 语音运行时：{deploy}")

    # 顺带把当年的法定节假日填上（省一半话费，纯本地正则替换）
    TARGET.write_text(text, encoding="utf-8")
    print(f"  ✓ 写好 {TARGET.name}")

    try:
        sys.argv = [sys.argv[0], "--write", "--config", str(TARGET)]
        import runpy
        runpy.run_path(str(ROOT / "tools" / "holidays.py"), run_name="__main__")
    except SystemExit:
        pass
    except Exception as e:
        print(f"  ! 节假日没填上（不影响使用）：{e}")
    return True


def step_persona(opts: dict) -> None:
    head("人设")
    PERSONA_DIR.mkdir(exist_ok=True)
    if DEFAULT_PERSONA_FILE.exists():
        n = len(DEFAULT_PERSONA_FILE.read_text(encoding="utf-8"))
        print(f"  ✓ 已有 {DEFAULT_PERSONA_FILE.relative_to(ROOT)}（{n} 字）")
        print("    想换性格就直接编辑这个文件 —— 它是每轮对话的开头，写得越具体越像人。")
    else:
        print(f"  ! 没有 {DEFAULT_PERSONA_FILE.relative_to(ROOT)}，程序会用内置的极简人设。")
        print("    建议自己写一段：名字、年龄、说话习惯、不想聊什么。")


def step_next(cfg_hint: dict) -> None:
    head("接下来只剩 NapCat 这一步")
    port = cfg_hint.get("port", "3001")
    mode = cfg_hint.get("mode", "reverse")
    bundled = (ROOT.parent / "NapCatQQ" / "NapCat.Shell").is_dir()
    if bundled:
        print(f"""
  1) 本包已自带 NapCat，不用另外下载。
     双击 {ROOT.parent.name}\\NapCatQQ\\NapCat.Shell\\start-napcat.bat，扫码登录你的 QQ 小号。
     （注入 QQ 需要管理员权限，会弹一次 UAC，点「是」。）

  2) 网络配置已经预置好了（指向 ws://127.0.0.1:{port}/onebot/v11/ws），不用手动加。
     想核对：NapCat 的 config\\onebot11.json。

  3) 回来启动：双击 {ROOT.parent.name}\\启动全部.bat
     （它会先把语音服务拉起来，再起机器人和管理面板）

  4) 拿你的主号给那个小号发一条「在吗」。
     第一个私聊它的人会被自动认成主人，之后只有你能聊。
""")
    else:
        print(f"""
  1) 下载 NapCat（Windows 一键包 NapCat.Shell.zip）
     https://github.com/NapNeko/NapCatQQ/releases
     解压到任意目录，双击 launcher.bat，扫码登录你的 QQ 小号。

  2) 在 NapCat 里配网络（NapCat → 网络配置 → 新建）
     类型：{"「WebSocket 反向代理」" if mode == "reverse" else "「WebSocket 服务器」"}
     地址/端口：{"ws://127.0.0.1:" + port + "/onebot/v11/ws" if mode == "reverse" else port}
     消息格式：array（不是 string）；勾上「心跳」。

  3) 回来启动：
     python main.py

  4) 拿你的主号给那个小号发一条「在吗」。
     第一个私聊它的人会被自动认成主人，之后只有你能聊。
""")
    print("  卡住了就跑：python doctor.py     （它会把每个环节查一遍并告诉你改哪儿）")


# ────────────────────────── 主流程 ──────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="qq-companion 安装向导")
    ap.add_argument("--yes", "-y", action="store_true", help="全用默认值，不问你")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 config.yaml")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--name", default="")
    ap.add_argument("--budget", default="")
    ap.add_argument("--port", default="")
    ap.add_argument("--mode", default="", choices=["", "reverse", "forward"])
    args = ap.parse_args()

    print("\n" + "=" * 50)
    print("  qq-companion 安装向导")
    print("  把 AI 接进 QQ，像真人一样陪你说话")
    print("=" * 50)

    current_key = ""
    if TARGET.exists():
        m = re.search(r'(?m)^\s*api_key:\s*"([^"]*)"', TARGET.read_text(encoding="utf-8"))
        current_key = (m.group(1) if m else "").strip()

    if not step_deps(args):
        return 1

    key = step_key(args, current_key)
    verify_key(key, assume_yes=args.yes)

    opts = step_misc(args)
    if not step_write(args, key, opts):
        return 1
    step_persona(opts)

    head("自检")
    run([sys.executable, str(ROOT / "doctor.py")])

    step_next(opts)
    print("\n  全部完成。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
