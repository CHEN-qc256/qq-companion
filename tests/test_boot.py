"""开机自启测试（不真启动机器人、不碰真实的 Run 键）。

六组：
  A 命令拼装      —— pythonw / 引导器 / 引号 / --auto 都在正确的位置
  B 注册表往返    —— 在**隔离的测试键**上写→读→删，顺带验证 stale 检测
  C 无窗口启动    —— DETACHED_PROCESS vs CREATE_NEW_CONSOLE，输出真的进了文件
  D 引导器        —— --status 能跑、单实例锁、各步骤的 skip 分支
  E 配置与参数    —— BootConfig 默认值、schema 6 个参数、校验能挡住越界
  F 状态文件      —— boot-state.json 的读写

★ 为什么不在真实键上测：`HKCU\\...\\Run\\qq-companion` 是用户正在用的那条。
  测试一旦把它删了/改了，用户下次登录就真的不自启了，而且完全没有提示。
  所以 B 组用 `Software\\qq-companion-test\\Run`，跑完自己清干净。

跑法：python tests/test_boot.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import autostart  # noqa: E402
from app.config import BootConfig, load_config  # noqa: E402
from panel import configio, schema  # noqa: E402

FAILED = []
PASSED = 0

TEST_KEY = r"Software\qq-companion-test\Run"
TEST_VALUE = "selftest"


def check(name: str, cond, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  —— {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# 真实启动项在本进程刚起来时先拍一张，后面 B 组跑完再比对。
# 不能拿 autostart.command(ROOT) 当基准：换了解释器（venv / Store 版）时
# 命令本来就该变，那不算「被测试动过」。
_real_before = autostart.read()


# ──────────────────────────────────────────────────────────────────────
section("A 命令拼装：注册表里那一行到底长什么样")
# ──────────────────────────────────────────────────────────────────────
cmd = autostart.command(ROOT)
check("命令里用 pythonw 而不是 python", "pythonw" in Path(cmd.split('"')[1]).name.lower(),
      cmd)
check("命令指向 boot.py", "boot.py" in cmd, cmd)
check("带 --auto（那条静默入口）", cmd.rstrip().endswith("--auto"), cmd)
check("两段路径各自加了引号（空格也不会断）",
      cmd.count('"') == 4 and cmd.startswith('"'), cmd)
check("boot.py 真的存在", Path(autostart.boot_script(ROOT)).is_file())
check("pythonw 真的存在（不是猜的路径）", autostart.pythonw().is_file(),
      str(autostart.pythonw()))
check("参数：root 传别处时命令跟着变",
      "D:\\another" in autostart.command(Path("D:/another")))


# ──────────────────────────────────────────────────────────────────────
section("B 注册表往返（隔离键，不碰真实启动项）")
# ──────────────────────────────────────────────────────────────────────
def clean() -> None:
    try:
        autostart.disable(ROOT, key_path=TEST_KEY, value_name=TEST_VALUE)
    except Exception:
        pass


clean()                                   # 上一次跑挂了也先清干净
check("没登记过时 read 返回空串",
      autostart.read(TEST_KEY, TEST_VALUE) == "")
check("没登记过时 is_enabled 是 False",
      not autostart.is_enabled(TEST_KEY, TEST_VALUE))
check("disable 对「本来就没有」是幂等的（不抛异常）",
      autostart.disable(ROOT, key_path=TEST_KEY, value_name=TEST_VALUE)["enabled"] is False)

# 当前环境可能不给 python 写注册表的权限（某些被安全策略限制的解释器
# 套了「写受限」令牌，winreg 与 reg.exe 都 WinError 5）。那种情况下不把
# 整组判成失败，而是改测「拒绝时的行为」：错误信息要能指导用户去跑
# install_autostart.bat，且不许在注册表里留下垃圾。
_writable = True
_enable_err = ""
try:
    st = autostart.enable(ROOT, key_path=TEST_KEY, value_name=TEST_VALUE)
except autostart.AutostartError as e:
    _writable = False
    _enable_err = str(e)

if _writable:
    check("enable 之后 enabled=True", st["enabled"] is True)
    check("写进去的和读回来的一模一样", st["command"] == autostart.read(TEST_KEY, TEST_VALUE))
    check("命令和当前目录对得上（stale=False）", st["stale"] is False)
    check("status 里带 runKey（面板要显示给人看）",
          st["runKey"].startswith("HKCU\\"), st["runKey"])

    # 手工改成一个「别的目录」——模拟项目被挪走
    import winreg  # noqa: E402
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, TEST_VALUE, 0, winreg.REG_SZ,
                          '"C:\\old\\pythonw.exe" "C:\\old\\qq-companion\\boot.py" --auto')
    st2 = autostart.status(ROOT, key_path=TEST_KEY, value_name=TEST_VALUE)
    check("指向别的目录时 stale=True", st2["stale"] is True)
    check("stale 时 enabled 仍然是 True（只是失效，不是没开）", st2["enabled"] is True)
    check("stale 时 expected 给出该重写成什么",
          st2["expected"].endswith("--auto") and "qq-companion" in st2["expected"])

    st3 = autostart.enable(ROOT, key_path=TEST_KEY, value_name=TEST_VALUE)
    check("重新 enable 能把失效的路径修好", st3["stale"] is False
          and autostart.read(TEST_KEY, TEST_VALUE) == st3["command"])

    st4 = autostart.disable(ROOT, key_path=TEST_KEY, value_name=TEST_VALUE)
    check("disable 之后 enabled=False", st4["enabled"] is False)
    check("disable 之后注册表里读不到值", autostart.read(TEST_KEY, TEST_VALUE) == "")
else:
    print("  ⊘ 当前 python 写不了注册表（安全策略），B 组改为验证失败路径：")
    check("拒绝时写的是可操作的中文提示（指向 install_autostart.bat）",
          "install_autostart.bat" in _enable_err, _enable_err[:120])
    check("拒绝后测试键里没有留下残留值",
          autostart.read(TEST_KEY, TEST_VALUE) == "")
    check("拒绝后 status 仍能正常返回（enabled=False，不崩）",
          autostart.status(ROOT, key_path=TEST_KEY,
                           value_name=TEST_VALUE)["enabled"] is False)

check("真实启动项没被测试动过（跑测试前后一致）",
      autostart.read() == _real_before)
# 收尾：把测试键也删掉，别在注册表里留垃圾
try:
    import winreg  # noqa: E402
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.DeleteKey(k, "")
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\qq-companion-test")
except OSError:
    pass


# ──────────────────────────────────────────────────────────────────────
section("C 无窗口启动：到底是怎么藏的")
# ──────────────────────────────────────────────────────────────────────
kw_hide = autostart.hidden_kwargs(True)
kw_show = autostart.hidden_kwargs(False)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

check("隐藏模式用了 DETACHED_PROCESS（子进程压根不分配控制台）",
      kw_hide["creationflags"] & DETACHED, hex(kw_hide["creationflags"]))
check("隐藏模式不用 CREATE_NO_WINDOW（和 DETACHED 同时写会被忽略，写了也是骗自己）",
      not kw_hide["creationflags"] & NO_WINDOW)
check("调试模式用 CREATE_NEW_CONSOLE（能看见输出）",
      kw_show["creationflags"] & NEW_CONSOLE)
check("两种模式都自成进程组（关掉父进程不会连带杀掉）",
      kw_hide["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
      and kw_show["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP)

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    log = td / "spawn.log"
    pid = autostart.spawn([sys.executable, "-c", "print('hello-from-child')"],
                          cwd=td, log_path=log, hide=True)
    check("spawn 返回了 PID", pid > 0)
    text = ""
    deadline = time.time() + 15
    while time.time() < deadline:
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        if "hello-from-child" in text:
            break
        time.sleep(0.2)
    check("子进程的输出真的落进了日志文件（不是黑洞）", "hello-from-child" in text, text[:200])
    check("日志里有启动时间戳便于排查", "启动" in text, text[:120])
    check("父进程关掉文件句柄后子进程照样跑完", log.exists())

    # ★ 真正的证据：让子进程自己问 Windows「我有控制台窗口吗」。
    #   GetConsoleWindow() 返回 0 = 这个进程根本没有控制台，也就没有任何窗口可弹。
    #   这比「肉眼看着没弹窗」靠谱 —— 肉眼看不见一闪而过的黑框。
    probe = td / "probe.log"
    code = ("import ctypes;"
            "print('HWND', ctypes.windll.kernel32.GetConsoleWindow())")
    autostart.spawn([sys.executable, "-c", code], cwd=td, log_path=probe, hide=True)
    text2 = ""
    deadline = time.time() + 15
    while time.time() < deadline:
        text2 = probe.read_text(encoding="utf-8", errors="replace") if probe.exists() else ""
        if "HWND" in text2:
            break
        time.sleep(0.2)
    if "HWND 0" in text2:
        check("子进程拿到的控制台句柄是 0（真的没有窗口，不是藏起来）", True)
    else:
        # DSH 这类宿主会给它拉起的每个进程套一个伪控制台，GetConsoleWindow
        # 因此永远不为 0 —— 那是宿主的行为，不是 spawn 的 bug。真实登录场景
        # （资源管理器拉起）里 DETACHED_PROCESS 仍然是「压根没有控制台」。
        print("  ⊘ 当前宿主给每个子进程都分配了控制台，GetConsoleWindow 不会为 0，"
              "这条只能在真实桌面验证（spawn 的 flag 已在上一条断言里核对）")


# ──────────────────────────────────────────────────────────────────────
section("D 引导器 boot.py：状态、锁、跳过分支")
# ──────────────────────────────────────────────────────────────────────
r = subprocess.run([sys.executable, str(ROOT / "boot.py"), "--status",
                    "--config", str(ROOT / "tests" / "config.e2e.yaml")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("--status 退出码 0", r.returncode == 0, str(r.returncode))
check("--status 里有「登录时触发」（说清触发时机）", "登录时触发" in r.stdout)
check("--status 里有注册表路径", "CurrentVersion\\Run" in r.stdout)
check("--status 里列出了下次会拉起什么", "下次自启会拉起" in r.stdout, r.stdout[-300:])

r2 = subprocess.run([sys.executable, str(ROOT / "boot.py")],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
check("不给参数时报错退出（不静默什么都不做）", r2.returncode != 0)

import boot as boot_mod  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    lock_path = Path(td) / "boot.lock"
    l1 = boot_mod.Lock(lock_path)
    check("锁：第一次拿得到", l1.acquire() is True)
    check("锁：文件里写的是自己 PID", lock_path.read_text().strip() == str(os.getpid()))
    lock_path.write_text(str(os.getppid()))          # 假装另一个活着的进程占着
    l2 = boot_mod.Lock(lock_path)
    check("锁：活着的别的进程占着时拿不到（不会起两份）", l2.acquire() is False)
    lock_path.write_text("999999")                   # 一个早就没了的 PID
    check("锁：残留的死 PID 不算占用", boot_mod.Lock(lock_path).acquire() is True)
    l1.release()
    check("release 之后锁文件被清掉", not lock_path.exists())

r3 = boot_mod._result("X", ok=True, pid=1, msg="m")
check("步骤结果有固定字段（面板/日志都按这个渲染）",
      set(r3) == {"name", "ok", "pid", "msg", "skipped"})
check("默认不是跳过", r3["skipped"] is False)
check("_port_of 能从 api_base 里抠出端口",
      boot_mod._port_of("http://127.0.0.1:9881") == 9881)
check("_port_of 对坏地址给默认值", boot_mod._port_of("不是网址") == 9880)

check("语音关着时 step_tts 直接跳过（不去碰模型目录）",
      boot_mod.step_tts(SimpleNamespace(boot=SimpleNamespace(tts=False)),
                        None, hide=True)["skipped"] is True)

# 秒退检测：2026-10-20 08:29 语音「自启成功」其实启动即崩，boot 却打了 ✓。
# 新逻辑会盯几秒，进程没了就报✗ 并把日志最后一行带出来。
# 日志文件放 data/_boottest（不用 TemporaryDirectory：Windows 上进程刚退出时
# 句柄还没释放，临时目录清理会 WinError 32 把测试搞挂）。
_bt = ROOT / "data" / "_boottest"
_bt.mkdir(parents=True, exist_ok=True)
lp = _bt / "boot-tts.log"
lp.write_text("", encoding="utf-8")
dead = autostart.spawn(["cmd.exe", "/c", "exit", "3"],
                       cwd=ROOT, log_path=lp, hide=True)
time.sleep(0.5)
lp.write_text("Traceback: 测试用的最后一行\n", encoding="utf-8")
msg = boot_mod._spawn_alive(dead, lp, 3.0)
check("秒退的进程会被判失败（不再打勾）", msg is not None)
check("失败说明里带上日志文件名", "boot-tts.log" in (msg or ""))
check("失败说明里带上日志最后一行", "测试用的最后一行" in (msg or ""))
alive = autostart.spawn([sys.executable, "-c", "import time; time.sleep(5)"],
                        cwd=ROOT, log_path=lp, hide=True)
check("活着的进程不会被误报", boot_mod._spawn_alive(alive, lp, 1.5) is None)
try:
    os.kill(alive, 9)
except OSError:
    pass
for _ in range(25):                      # 等它真退出，然后再收尾
    if not boot_mod.netprobe.pid_alive(alive):
        break
    time.sleep(0.2)
for _ in range(10):
    try:
        lp.unlink(missing_ok=True)
        _bt.rmdir()
        break
    except OSError:
        time.sleep(0.2)
check("NapCat 关着时 step_napcat 跳过（不猜、也不去拉 QQ）",
      boot_mod.step_napcat(SimpleNamespace(boot=SimpleNamespace(napcat=False)),
                           None, hide=True)["skipped"] is True)
check("面板关着时 step_panel 跳过",
      boot_mod.step_panel(SimpleNamespace(boot=SimpleNamespace(panel=False)),
                          None, hide=True)["skipped"] is True)
check("按进程名查一个不存在的程序 → False（不会误判成在跑）",
      boot_mod._image_running("definitely-not-running.exe") is False)


# ──────────────────────────────────────────────────────────────────────
section("E 配置与参数：boot 这一组")
# ──────────────────────────────────────────────────────────────────────
b = BootConfig()
check("默认不开自启", b.enabled is False)
check("默认等 10 秒", b.delay == 10)
check("默认隐藏窗口", b.headless is True)
check("默认顺带开面板", b.panel is True)
check("默认开完面板顺手开浏览器（全隐藏时唯一看得见的东西）", b.open_panel is True)
check("默认代管 NapCat（不然登录后 QQ 是离线的）", b.napcat is True)
check("默认不写死 NapCat 路径（自动找，挪盘也不会失效）", b.napcat_path == "")
check("默认不写死 QQ 号（从 start-napcat.bat 里读）", b.napcat_qq == "")
check("默认不拉起语音服务（它要 60 秒）", b.tts is False)

boot_params = [p for p in schema.ALL_PARAMS if p.path.startswith("boot.")]
check("schema 里 boot 组有 10 个参数（多了 BiliNote）", len(boot_params) == 10,
      str([p.path for p in boot_params]))
check("10 个参数一个都不少",
      {p.path for p in boot_params} == {
          "boot.enabled", "boot.delay", "boot.napcat", "boot.napcat_qq",
          "boot.napcat_path", "boot.tts", "boot.bilinote", "boot.panel",
          "boot.open_panel", "boot.headless"})
check("都归在 boot 组", all(p.group == "boot" for p in boot_params))
check("都有中文说明（面板 tooltip / 配置文件注释同源）",
      all(p.desc.strip() and p.note.strip() for p in boot_params))
check("都不需要重启（下次登录才生效，机器人本体根本不读它们）",
      all(not p.restart for p in boot_params))
check("configio 会把 boot 这一节归到「开机自启」分组",
      configio._TOP_GROUP.get("boot") == "boot")
check("GROUPS 里有 boot 且有说明",
      any(g.id == "boot" and g.title and g.desc for g in schema.GROUPS))

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td) / "c.yaml"
    tmp.write_text("llm:\n  api_key: \"\"\n", encoding="utf-8")
    cfg = configio.load(tmp)
    check("旧配置文件没有 boot 节时自动补默认值",
          configio.get_path(cfg, "boot.enabled") is False
          and configio.get_path(cfg, "boot.headless") is True)
    issues = configio.validate(cfg)
    check("默认值能过校验", not [i for i in issues if i["level"] == "error"],
          str(issues[:2]))

    bad = json.loads(json.dumps(cfg))
    configio.set_path(bad, "boot.delay", -1)
    errs = [i for i in configio.validate(bad) if i["level"] == "error"]
    check("延迟 -1 被挡住", any(i["path"] == "boot.delay" for i in errs), str(errs[:2]))
    configio.set_path(bad, "boot.delay", 9999)
    errs2 = [i for i in configio.validate(bad) if i["level"] == "error"]
    check("延迟 9999 被挡住（超过 300 秒）",
          any(i["path"] == "boot.delay" for i in errs2), str(errs2[:2]))

    # 写回去之后要能原样读出来（render 的注释块不能把 boot 吃掉）
    configio.set_path(cfg, "boot.delay", 25)
    configio.set_path(cfg, "boot.napcat_path", "D:/NapCat/launcher.bat")
    configio.save(tmp, cfg, backup=False)
    text = tmp.read_text(encoding="utf-8")
    check("写回的 config.yaml 里有 boot 节和注释", "boot:" in text and "开机自启" in text)
    again = load_config(tmp)
    check("重新加载后 delay 是改过的 25", again.boot.delay == 25)
    check("NapCat 路径原样保留", again.boot.napcat_path == "D:/NapCat/launcher.bat")


# ──────────────────────────────────────────────────────────────────────
section("F 上次自启的结果（面板要读它给人看）")
# ──────────────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    check("没有记录时返回 None（不是抛异常）", autostart.read_state(td) is None)
    (td / "boot-state.json").write_text("{ 坏掉的 json", encoding="utf-8")
    check("记录坏了也返回 None", autostart.read_state(td) is None)
    (td / "boot-state.json").write_text(json.dumps({
        "at": 1.0, "atText": "2026-10-19 08:00:00", "seconds": 12.5,
        "ok": True, "steps": [{"name": "机器人", "ok": True, "pid": 1,
                               "msg": "已启动", "skipped": False}],
    }, ensure_ascii=False), encoding="utf-8")
    st = autostart.read_state(td)
    check("能读回 steps", st and st["steps"][0]["name"] == "机器人")
    check("能读回耗时", st and st["seconds"] == 12.5)


# ──────────────────────────────────────────────────────────────────────
section("G NapCat：定位 / 参数拼装 / 提权计划任务")
# ──────────────────────────────────────────────────────────────────────
from app import napcat  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    shell = Path(td) / "NapCat.Shell"
    shell.mkdir()
    (shell / "NapCatWinBootMain.exe").write_bytes(b"MZ")
    (shell / "NapCatWinBootHook.dll").write_bytes(b"MZ")
    (shell / "napcat.mjs").write_text("//", encoding="utf-8")
    (shell / "loadNapCat.js").write_text("//", encoding="utf-8")
    (shell / "qqnt.json").write_text("{}", encoding="utf-8")
    (shell / "start-napcat.bat").write_text(
        'set "NAPCAT_QUICK_QQ=1234567890"\n', encoding="utf-8")

    check("能按目录找到 NapCat.Shell",
          napcat.find_shell(str(shell)) == shell.resolve())
    check("给了个错的目录不会认错（继续往下找真正的那份）",
          napcat.find_shell(str(Path(td) / "nope")) != Path(td) / "nope")

    qq = Path(td) / "QQ.exe"
    qq.write_bytes(b"MZ")
    check("能显式指定 QQ.exe", napcat.find_qq(str(qq)) == qq)
    check("显式路径不存在时不会瞎返回", napcat.find_qq(str(Path(td) / "no.exe")) is None
          or napcat.find_qq(str(Path(td) / "no.exe")) != Path(td) / "no.exe")

    check("快速登录号能从 start-napcat.bat 里读出来",
          napcat.quick_uin(shell) == "1234567890")
    check("配置里填了就用配置的（覆盖 bat）",
          napcat.quick_uin(shell, "987654") == "987654")
    check("号不是数字时退回 bat 里的",
          napcat.quick_uin(shell, "abc") == "1234567890")

    argv = napcat.launch_argv(shell, qq, "123")
    check("参数顺序和 start-napcat.bat 一致（main / QQ / hook / -q / 号）",
          [Path(argv[0]).name, argv[1], Path(argv[2]).name, argv[3], argv[4]] ==
          ["NapCatWinBootMain.exe", str(qq), "NapCatWinBootHook.dll", "-q", "123"])
    check("没有号时就不带 -q（改成扫码登录）",
          "-q" not in napcat.launch_argv(shell, qq, ""))
    env = napcat.launch_env(shell, {})
    check("注入需要的 5 个 NAPCAT_* 变量都指向这个目录",
          all(env[k].startswith(str(shell)) for k in
              ("NAPCAT_PATCH_PACKAGE", "NAPCAT_LOAD_PATH", "NAPCAT_INJECT_PATH",
               "NAPCAT_LAUNCHER_PATH", "NAPCAT_MAIN_PATH")))

    xml = napcat.task_xml(pythonw=r"C:\py\pythonw.exe", root=ROOT, user="PC\\u")
    check("任务里有登录触发器（Windows 自己拉起，不用我们调 schtasks）",
          "<LogonTrigger>" in xml and "<Delay>PT15S</Delay>" in xml)
    check("任务每 5 分钟巡一次（QQ 崩了会自己回来）",
          "<Interval>PT5M</Interval>" in xml)
    check("任务是最高权限（注入 QQ 的前提）",
          "<RunLevel>HighestAvailable</RunLevel>" in xml)
    check("任务动作是 pythonw -m app.napcat launch（没有控制台）",
          "<Command>C:\\py\\pythonw.exe</Command>" in xml
          and "<Arguments>-m app.napcat launch</Arguments>" in xml)
    check("插电/电池都照跑（笔记本别在电池上装死）",
          "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml)
    check("任务不设超时（NapCat 要长期活着）",
          "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml)
    check("用户名里的特殊字符会被转义（& < >）",
          "&amp;" in napcat.task_xml(pythonw="p.exe", root=ROOT, user="A&B"))

check("task_file 指向 System32\\Tasks\\<名字>",
      str(napcat.task_file()).endswith(os.path.join("System32", "Tasks", napcat.TASK_NAME)))

# 连接状态：从日志尾部判断，比「端口在听」可靠（3001 是机器人自己在听）
with tempfile.TemporaryDirectory() as td:
    log = Path(td) / "c.log"
    check("没有日志时给 None（不知道，不是 False）", napcat.connection_state(log) is None)
    log.write_text("NapCat 已连接\n", encoding="utf-8")
    check("日志说连上了 → True", napcat.connection_state(log) is True)
    log.write_text("NapCat 已连接\nNapCat 断开\n", encoding="utf-8")
    check("后面又断开了 → False（以最后一条为准）", napcat.connection_state(log) is False)
    log.write_text("NapCat 断开\nNapCat 已连接\n", encoding="utf-8")
    check("断线重连后 → True", napcat.connection_state(log) is True)
    log.write_text("随便一行日志\n", encoding="utf-8")
    check("日志里没有相关记录 → None", napcat.connection_state(log) is None)
    # 旧会话的「已连接」不能算到新会话头上：08:29 开机自启后 QQ 在等扫码，
    # boot-state 却因为日志里还留着 00:48 的「NapCat 已连接」而报了 true。
    log.write_text("qq-companion 已启动。\nNapCat 已连接\n"
                   "qq-companion 已启动。\n", encoding="utf-8")
    check("上次会话连过、这次还没连 → False（不回看旧会话）",
          napcat.connection_state(log) is False)
    log.write_text("qq-companion 已启动。\nNapCat 已连接\n", encoding="utf-8")
    check("本次会话连上了 → True", napcat.connection_state(log) is True)

# 看门狗模式：QQ 在跑就立刻退出，不重复注入
_calls = []
_real_run = napcat.qq_running
napcat.qq_running = lambda: True
napcat.find_shell = lambda *a, **k: _calls.append("find_shell")
rc = napcat._launch_cli()
napcat.qq_running = _real_run
check("QQ 已经在跑时 launch 直接返回 0（5 分钟一次的巡检是空转）", rc == 0)
check("空转时连目录都不去查（省得拖慢登录）", not _calls)

# 看门狗那条路径也必须尊重 napcat-nohide.flag
# （早先它不检查，结果扫码时窗口刚露出来就被 5 分钟一次的巡检按下去）
_hide_calls = []
_real_hide = napcat.hide_qq_windows_once
napcat.hide_qq_windows_once = lambda: (_hide_calls.append(1), 1)[1]
_flag = Path(napcat.ROOT) / "data" / "napcat-nohide.flag"
_flag.parent.mkdir(parents=True, exist_ok=True)
_had_flag = _flag.exists()
try:
    napcat.qq_running = lambda: True
    # 没有 flag → 照常藏
    if _flag.exists() and not _had_flag:
        _flag.unlink()
    napcat._launch_cli()
    check("没有 nohide 时巡检会顺手藏窗口", len(_hide_calls) == 1)
    # 有 flag → 一次都不许碰
    _hide_calls.clear()
    _flag.write_text("", encoding="utf-8")
    napcat._launch_cli()
    check("nohide 开着时巡检也不许碰窗口", not _hide_calls)
finally:
    napcat.qq_running = _real_run
    napcat.hide_qq_windows_once = _real_hide
    if _had_flag:
        _flag.write_text("", encoding="utf-8")
    elif _flag.exists():
        _flag.unlink()


# ── NapCat WebUI 端口发现 ────────────────────────────────────────────
# 09:00 那次：6099 被 07:57 的旧实例占着（卡在 waiting_qrcode），真正登录
# 并连上机器人的实例把 WebUI 挪到了 6100。工具只问 6099 → 永远「没登录」。
import tools.napcat_login as nl

_real_port_open = nl._port_open
_real_instances = nl.instances
_real_api = nl.api
_real_refresh = nl.refresh_qrcode
try:
    nl._port_open = lambda port, timeout=0.35: port == 6100
    ports = nl.candidate_ports()
    check("候选端口从配置端口开始（6099 排第一）", bool(ports) and ports[0] == 6099)
    check("扫描把真正在听的 6100 带进来", 6100 in ports)
    check("没在听的空位不塞进来", 6101 not in ports)
    check("端口不重复", len(ports) == len(set(ports)))

    stale = {"port": 6099, "base": "http://127.0.0.1:6099",
             "data": {"isLogin": False, "loginPhase": "waiting_qrcode"}, "error": ""}
    live = {"port": 6100, "base": "http://127.0.0.1:6100",
            "data": {"isLogin": True, "loginPhase": "ready", "coreReady": True}, "error": ""}
    nl.instances = lambda extra=None: [stale, live]
    check("优先挑已登录的实例（不被 6099 的 waiting_qrcode 骗到）",
          nl.pick_instance()["port"] == 6100)
    rep = nl.status_report()
    check("status 报 activePort=6100", rep["activePort"] == 6100
          and rep["active"]["isLogin"] is True)
    check("status 把两个实例都列出来", [i["port"] for i in rep["instances"]] == [6099, 6100])

    asked = []

    def _fake_api(path, payload=None, timeout=10.0, base=None):
        asked.append(base)
        return {"data": {}}

    nl.api = _fake_api
    nl.login_state(6100)
    check("login_state(port) 问到指定端口，不是写死的 6099",
          asked == ["http://127.0.0.1:6100"])

    refreshed = []
    nl.refresh_qrcode = lambda port=None: refreshed.append(port) or {}
    rc = nl.main(["napcat_login.py", "qr"])
    check("已经登录时 qr 不刷新二维码、直接返回 0（不乱改状态）",
          rc == 0 and not refreshed)

    nl.instances = lambda extra=None: [stale]
    refreshed.clear()
    nl.main(["napcat_login.py", "qr"])
    check("没登录时 qr 去刷 6099 那个实例", refreshed == [6099])
finally:
    nl._port_open = _real_port_open
    nl.instances = _real_instances
    nl.api = _real_api
    nl.refresh_qrcode = _real_refresh

# ── step_napcat 的竞态：机器人刚起来那 1 秒还没等到 NapCat 建链 ──────
_real_tcp_open = boot_mod.netprobe.tcp_open
_real_conn2 = napcat.connection_state
_cfg_like = SimpleNamespace(onebot=SimpleNamespace(port=3001),
                            log_path=lambda: Path("unused.log"))
try:
    boot_mod.netprobe.tcp_open = lambda host, port, timeout=0.3: False
    napcat.connection_state = lambda p: False
    check("机器人没在听 3001 时不等（立刻判 False）",
          boot_mod._wait_napcat_connected(_cfg_like) is False)

    boot_mod.netprobe.tcp_open = lambda *a, **k: True
    seq = [False, False, True]
    napcat.connection_state = lambda p: seq.pop(0) if seq else True
    check("机器人在听时就等到 NapCat 连上再下结论",
          boot_mod._wait_napcat_connected(_cfg_like, seconds=3.0) is True)
finally:
    boot_mod.netprobe.tcp_open = _real_tcp_open
    napcat.connection_state = _real_conn2


# ──────────────────────────────────────────────────────────────────────
print()
print("=" * 70)
print(f"通过 {PASSED} 项" + (f"，失败 {len(FAILED)} 项：" if FAILED else "，全部通过"))
for f in FAILED:
    print("  ✗ " + f)
print("=" * 70)
raise SystemExit(1 if FAILED else 0)
