# qq-companion

把 DeepSeek 接进 QQ 的陪聊机器人 —— 有人设、有作息、有长期记忆，还会自己找你说话。

![License](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![Deps](https://img.shields.io/badge/dependencies-3-brightgreen.svg)
![Tests](https://img.shields.io/badge/tests-850%20passed-brightgreen.svg)

```
QQ 客户端 ──► NapCat（协议端，一个真实 QQ 号）──► OneBot 11 ──► 本项目 ──► DeepSeek API
                                                                 ├──► GPT-SoVITS（本地语音）
                                                                 └──► B 站 + BiliNote（视频笔记）
```

---

## 目录

- [它是什么](#它是什么)
- [功能](#功能)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [配置](#配置)
- [语音（TTS）](#语音tts)
- [B 站视频推送](#b-站视频推送)
- [命令行参考](#命令行参考)
- [目录结构](#目录结构)
- [测试](#测试)
- [花了多少钱](#花了多少钱)
- [常见问题](#常见问题)
- [风险与免责](#风险与免责)
- [参与贡献](#参与贡献)
- [许可证](#许可证)
- [English](#english)

---

## 它是什么

一个**跑在你自己电脑上**的 QQ 陪聊机器人。它用 [NapCat](https://github.com/NapNeko/NapCatQQ) 作为协议端
登录一个真实的 QQ 号，通过 OneBot 11 和本项目对话，回复由 DeepSeek 生成。

和"接个大模型到群里"的玩具不同，这个项目主要解决四件麻烦事：

| 麻烦 | 这里的做法 |
|---|---|
| **不像人**：秒回、长段落、客服腔 | 回之前先停顿、按心情决定回多长、长回复拆成几条、洗掉书面语 |
| **记不住**：聊两小时就忘 | SQLite 存短期上下文，定期压成摘要 + 用户画像，再抽成可检索的长期事实 |
| **太主动 / 太沉默** | 作息表决定"现在该不该说话"；静默太久且有由头才主动开口，每天有次数上限 |
| **烧钱** | 上下文按"越靠前越不变"排布吃满前缀缓存 + 关思考模式 + 本地规则接掉琐碎消息 + 预算守卫 |

技术上它是**单进程 asyncio** 应用，只依赖 `httpx` / `websockets` / `PyYAML` 三个包，
数据全部落在本地 SQLite，不经过任何第三方服务器（除了 DeepSeek API 本身）。

---

## 功能

### 说话像个人

- **延迟**：按消息长度随机停顿，不是固定的 `sleep(1)`
- **拆条**：长回复拆成 2~4 条依次发出，带"正在输入"的节奏感
- **洗稿**：本地正则清掉"作为一个 AI""希望能帮到你"这类句式，不改原意、不额外花钱
- **琐碎消息本地接**：`在吗` / `在不在` / `？` 这类由规则表直接回，**一次 API 都不调**

### 作息与情绪

`free` / `working` / `winding_down` / `sleeping` 四种状态，按配置的作息表切换。
凌晨三点它不会精神抖擞地长聊 —— 该睡就睡，被吵醒会敷衍你。

### 长期记忆

- 短期：最近若干轮对话，超预算时折叠成摘要（顺便刷新用户画像，一次请求两用）
- 长期：从对话里抽出事实（工作、喜好、关系、情绪模式），存进 SQLite
- 检索：**纯本地**打分（关键词 + 时间衰减），不调 embedding 接口，一分钱不花
- 记忆按会话隔离：私聊和群聊、不同账号各记各的，不会串台

### 主动搭话

静默超过 `min_idle_hours`（默认 6 小时）、当天没超过 `max_per_day`（默认 2 次）、
并且有"由头"（记着的旧事、时间点、日程）时才会开口。四条护栏都可配，
默认值刻意保守 —— 陪聊机器人最容易被讨厌的地方就是话太多。

### 群聊

- 区分说话人，回复里带 `@昵称` 前缀，不会把张三的话回给李四
- 群里更收敛：默认只在被点名或命中关键词时说话
- 独立群聊人设（`persona/group.md`），和私聊互不影响
- 三层限流：每人群冷却、全局频率、每日总量

### 主人和客人

`whitelist.masters` 里的人会被当作主人（会被叫"老大"、能听到私事）；
`whitelist.private` 里的其他人是客人（能聊，但听不到主人的事，也不会被当成主人）。
群里同理，只有 master 才是主人。

### 联网搜索

DeepSeek 的 **API 没有联网开关**（那是官网聊天页的功能）。这里走 Tool Calls：
把"搜索"作为工具交给模型，由模型自己判断这一句要不要搜。搜索源是免 key 的
DuckDuckGo HTML 端点，失败自动退回 Bing，不引入新依赖。
平时闲聊一次都不调，等于 0 成本；真搜一次约 0.001~0.003 元。

### 语音

接本地 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 做 TTS，
支持多音色（`refs/` 下的 wav + 情绪绑定）、按概率自动转语音、全语音模式。
详见 [语音（TTS）](#语音tts)。

### B 站视频推送

按关键词搜 B 站视频 → 用思考模式逐条筛掉标题党 → BiliNote 生成内容笔记 → 私聊发给你。
全局去重（推过的 `bvid` 不再推，被毙掉的 45 天后重新考虑）。详见 [B 站视频推送](#b-站视频推送)。

### 本地管理面板

`panel.bat` 起一个只监听 `127.0.0.1` 的网页面板（纯标准库 `http.server` + 原生 HTML/JS，零构建、零新增依赖）：

- **配置**：139 个参数分 15 组，按语义自动匹配控件（开关 / 下拉 / 步进 / 标签列表 / 文件选择 / 密文），保存前校验，写回时保住你的手写注释
- **日志**：跟随 `data/companion.log`，关键词过滤、自动滚动
- **花费与记忆**：今日花费、缓存命中率、近 14 天柱状图、按用途分账、长期记忆列表
- **启停**：热更新参数点保存即生效；启动期参数会提示需要重启并给按钮

### 省钱与预算守卫

三档预算：`normal` / `saver`（自动砍历史、压回复长度）/ `stop`（直接罢工等人改配置）。
它是最后一道保险 —— 前面全写错了也不会一夜烧掉几百块。

---

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | **Windows 10/11**（`.bat` 脚本是按 Windows 写的；核心 Python 代码本身跨平台，但要自己起进程） |
| Python | **3.10 或更新**（在 3.12 / 3.13 上测过） |
| 依赖 | `httpx` `websockets` `PyYAML`（见 `requirements.txt`） |
| DeepSeek | 一个 API Key，[platform.deepseek.com](https://platform.deepseek.com/) 申请 |
| QQ 接入 | [NapCat](https://github.com/NapNeko/NapCatQQ)（另装，本项目不包含）+ 一个**小号** |
| 语音（可选） | GPT-SoVITS + `api_v2.py`（另装） |
| 视频推送（可选） | BiliNote 后端（另装） |

> ⚠️ 用 Python 时请确认它能正常写 `%TEMP%` 和注册表：某些被安全策略限制的便携版 / uv 环境
> 会导致"语音起不来""开机自启装不上"。

---

## 快速开始

### 0. 拿到代码

```powershell
git clone https://github.com/CHEN-qc256/qq-companion.git
cd qq-companion
```

### 1. 装依赖

```powershell
python -m pip install -r requirements.txt
```

只有三个包，没有前端构建、没有 Node。

### 2. 写配置

三种方式，任选其一。

**A. 配置向导（推荐）**

```powershell
python setup.py
```

它会依次问：API Key、机器人名字、每天预算、端口、连接模式。每个问题都有默认值，
**一路回车也能过**。写完后顺手做两件事：用 `GET /models` 验证一次 key（不产生 token 费用）、
拉取当年的法定节假日填进计价表（法定节假日算半价时段）。

**B. 直接改模板**

```powershell
copy config.example.yaml config.yaml
```

`config.example.yaml` 里每一项都有中文注释，改 `llm.api_key` 就能跑。

**C. 用面板**

```powershell
panel.bat
```

浏览器里改，见下面 [配置](#配置)。

### 3. 体检

```powershell
python doctor.py          # 环境 / 依赖 / 配置 / 端口 / NapCat / 数据
python main.py --check    # 只查配置里的"烧钱项"，不启动
```

全绿再往下走。

### 4. 离线试跑（还没接 QQ 也能测）

一个终端起机器人，另一个终端起假 NapCat：

```powershell
# 终端 1
python main.py

# 终端 2
python tools\mock_napcat.py
```

`mock_napcat.py` 冒充 NapCat 连上来，你在它里面打字就等于对方在说话，每条回复会标出
**到达时刻**，方便直接看"像不像人"，末尾还有一段节奏复盘。

想跑可复现的回归（关掉随机延迟和作息）：

```powershell
python main.py --config tests\config.e2e.yaml
python tools\mock_napcat.py --script tests\chat_script.txt --gap 3 --wait 12
```

### 5. 接真 QQ

**a. 装并用小号登录 NapCat**

去 [NapCat 仓库](https://github.com/NapNeko/NapCatQQ) 下载，解压后用 `launcher.bat` 启动，
扫码登录**小号**。请务必用小号 —— 协议端有封号风险，见 [风险与免责](#风险与免责)。

**b. 让 NapCat 连过来**

本项目默认 `onebot.mode: reverse`：**机器人监听 3001，NapCat 主动连上来**。
在 NapCat 的 WebUI 里加一个「WebSocket 客户端」，地址填：

```
ws://127.0.0.1:3001/onebot/v11/ws
```

消息格式选 `array`，重连间隔 5000ms。

> NapCat v4.5.3+ 支持读 `NapCat.Shell/config/onebot11.json` 作为所有账号的默认网络配置，
> 把上面这段写进 `network.websocketClients`，登录后就不用每次进 WebUI 点。

也可以反过来：把 `onebot.mode` 改成 `forward`，让机器人主动去连 NapCat 的 WebSocket 服务器。

**c. 启动**

```powershell
start.bat
```

`start.bat` 会依次：没有 `config.yaml` 就先跑向导 → 跑 `doctor.py` → 跑 `main.py --check`
→ 都过了才真正启动。

**d. 认主人**

拿你的主号给小号发第一条私聊 —— 第一个私聊它的人会被自动写进 `whitelist.masters`。
想手动指定就改配置里的 `whitelist.masters`。

### 6. 停止

```powershell
stop.bat
# 或
python main.py --stop
```

它读 pidfile 精确停掉自己，不会按进程名乱杀。

### 7. 开机自启（可选）

```powershell
python boot.py --install     # 写入 HKCU\...\Run，登录 Windows 后静默拉起
python boot.py --status      # 看登记的命令、上次自启结果
python boot.py --uninstall   # 取消
```

用 `pythonw.exe` + `DETACHED_PROCESS` 启动，**不弹黑框**。

---

## 配置

配置的唯一入口是 `config.yaml`（由 `config.example.yaml` 复制而来，**不要提交它**，里面有 API Key）。

三种改法：

1. **面板**（推荐）：`panel.bat` → 配置参数页。改完点保存，热更新参数 1~2 秒内自动生效
2. **手改**：文件里每一项都有注释
3. **向导**：`python setup.py --force`

### 参数分区

| 分区 | 管什么 |
|---|---|
| `llm` | API Key、模型、思考模式、温度、超时 |
| `onebot` | 连接模式（reverse / forward）、端口、路径、鉴权 |
| `budget` | 每日预算、省电阈值、硬停倍数 |
| `context` | 上下文预算、历史条数、压缩阈值 |
| `humanize` | 延迟、拆条、打字节奏、琐碎消息规则表 |
| `life` | 作息表与四档状态 |
| `memory` | 抽取频率、检索条数、衰减 |
| `persona` | 人设文件路径、名字 |
| `whitelist` | 主号、允许私聊的人、允许的群 |
| `group` | 群聊限流、触发关键词 |
| `database` | SQLite 路径 |
| `tts` | 语音开关、音色、概率、语速 |
| `boot` | 开机自启要拉起哪些进程 |
| `logging` | 日志级别、文件、轮转 |
| `pricing` | 计价表（含高峰时段与法定节假日） |
| `videopush` | B 站推送关键词、时间、条数、BiliNote 地址 |

**139 个参数逐条说明**见 [`参数对照表.md`](参数对照表.md)（由 `panel/schema.py` 生成，
改了参数就跑 `python tools\param_table.py --write` 重新生成，别手改）。

### 人设

- 私聊：`persona/default.md`
- 群聊：`persona/group.md`

两个文件都是纯 Markdown，直接写"你叫谁、什么性格、怎么说话、有哪些雷区"。
仓库里自带的人设（"遥遥"，一个元气型助理）可以直接改，也可以整个替换。

写人设的建议：**多写具体行为，少写形容词**。写"句子短，经常就三五个字"比写"活泼开朗"有用得多。

---

## 语音（TTS）

**可选功能。** 不开也能正常聊天，只是不发语音条。

### 原理

```
app/tts.py ──HTTP──► GPT-SoVITS api_v2.py（127.0.0.1:9880）──► wav ──► 上传 ──► QQ 发语音
```

`app/tts.py` 负责三件事：决定这条要不要转语音、把文字洗成"能念出来"的话（去表情、
去 markdown、控制字数）、合成并发出去。

### 装 GPT-SoVITS

1. 按 [GPT-SoVITS 官方说明](https://github.com/RVC-Boss/GPT-SoVITS) 装好，准备好模型权重
2. 启动 API：
   ```powershell
   runtime\python.exe api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\configs\tts_infer.yaml
   ```
3. 在 `tts_infer.yaml` 里把 `t2s_weights_path` / `vits_weights_path` 指向你的 GPT / SoVITS 权重

### 配 refs

参考音频放 `refs/`，一个音色一个 wav + 同名 txt（内容是那段音频的文字）：

```
refs/
├── 胡桃.wav
├── 胡桃.txt
├── default.wav        ← 找不到指定音色时的兜底
├── default.txt
└── emotions.json      ← 情绪 → 音色 的映射（可选）
```

### 配置项

```yaml
tts:
  enabled: true                 # 总开关
  always_voice: false           # 全语音模式：每条都发
  api_base: "http://127.0.0.1:9880"
  install_dir: "D:/GPT-SoVITS"  # 也支持相对路径（按项目根目录解析）
  default_emotion: "胡桃"        # 必须和 refs 里的 wav 同名
  trigger: "auto"               # request=只有被要求时才发 / auto=按概率
  chance: 0.25                  # auto 模式下每条转语音的概率
  max_chars: 120                # 一条语音最多念多少字
  speed: 1.0
  group: false                  # 群里是否也主动发语音
```

面板的"语音"页还能做音色试听、上传参考音频、绑定情绪 —— 不需要手改文件。

---

## B 站视频推送

**也是可选功能**，需要有 BiliNote 后端。

### 分工

```
app/bilisearch.py  搜得到：B 站 web 接口 + WBI 签名（不需要登录）
      ↓ 候选
   AI（思考模式）    筛得准：逐条判断值不值得看，宁可少推不凑数
      ↓ 选中
app/bilinote.py    看得懂：调 BiliNote 把视频转成 Markdown 笔记
      ↓
app/videopush.py   送得到：私聊发标题 + UP 主 + 链接 + 摘要
```

### 装 BiliNote

[BiliNote](https://github.com/JefferyHcool/BiliNote) 是独立后端（FastAPI，默认 8483 端口）。
装好后把地址填进 `videopush.bilinote_dir`，并在面板里配好它的 Python 解释器路径。

### 配置

```yaml
videopush:
  enabled: true
  keywords: ["AI", "编程", "硬件"]
  times: ["12:30", "21:30"]   # 每天推两次
  max_items: 3                # 宁缺毋滥
  bilinote_dir: "D:/BiliNote/backend"
```

去重是全局的：推过的 `bvid` 永久记住，被 AI 毙掉的记住 45 天。

---

## 命令行参考

| 命令 | 作用 |
|---|---|
| `python main.py` | 启动机器人 |
| `python main.py --check` | 只体检配置，不连 NapCat |
| `python main.py --config x.yaml` | 用指定配置（测试常用） |
| `python main.py --stop` | 停掉正在跑的实例（读 pidfile） |
| `python setup.py` | 配置向导；`--yes` 全默认，`--force` 覆盖，`--api-key/--name/--budget/--port/--mode` |
| `python doctor.py` | 环境体检：Python / 依赖 / 配置 / 端口 / NapCat / 数据 |
| `python boot.py --install/--status/--uninstall` | 开机自启的装 / 查 / 卸 |
| `python -m panel [端口]` | 起管理面板（默认 8899） |
| `python tools\mock_napcat.py` | 假 NapCat，离线测试用（`--group 群号` 测群聊，`--script` 跑脚本） |
| `python tools\estimate.py --compare` | 按 token 模型推演成本，不上线就能看账单 |
| `python tools\report.py --days 7` | 上线后查账（按天 / 按用途 / 按会话） |
| `python tools\holidays.py --write` | 拉当年法定节假日填进计价表 |
| `python tools\param_table.py --write` | 由 schema 重新生成 `参数对照表.md` |
| `python tools\napcat_login.py status\|qr\|wait\|quick` | NapCat WebUI API 客户端（查状态 / 出二维码 / 等登录 / 快速登录） |

---

## 目录结构

```
qq-companion/
├── main.py                  启动入口（--check / --stop / --config）
├── boot.py                  引导器：开机自启、按序拉起进程、静默无窗口
├── setup.py                 配置向导
├── doctor.py                环境体检
├── config.example.yaml      配置模板（每项都有注释）
├── requirements.txt         httpx / websockets / PyYAML
├── start.bat                一键启动（向导 → 体检 → 启动）
├── stop.bat                 一键停止（读 pidfile）
├── panel.bat                一键启动管理面板
├── install_napcat_task.bat  把 NapCat 注册成计划任务（可选）
├── install_autostart.bat    开机自启的批处理入口（可选）
│
├── app/                     核心逻辑
│   ├── config.py            配置加载 + 计价表 + 高峰时段判定
│   ├── store.py             SQLite（消息 / 摘要 / 画像 / 事实 / 白名单 / 用量 / 推送去重）
│   ├── llm.py               DeepSeek 客户端（关思考模式、读缓存用量、记账）
│   ├── context.py           ★ 上下文组装：越靠前越不变，吃满前缀缓存
│   ├── memory.py            长期记忆：抽取、本地检索、去重合并
│   ├── orchestrator.py      闸门编排：一条消息要过哪些关
│   ├── onebot.py            OneBot 11 适配器（反向 / 正向 WebSocket）
│   ├── humanize.py          拟人化：延迟、拆条、洗掉 AI 腔
│   ├── life.py              作息与情绪状态机
│   ├── proactive.py         主动搭话（四条护栏）
│   ├── budget.py            预算守卫（normal / saver / stop）
│   ├── websearch.py         联网搜索（DuckDuckGo → Bing 兜底）
│   ├── tts.py               GPT-SoVITS 客户端
│   ├── bilisearch.py        B 站搜索（WBI 签名）
│   ├── bilinote.py          BiliNote 后端客户端
│   ├── videopush.py         视频推送编排（搜索 → 筛选 → 笔记 → 发送）
│   ├── napcat.py            NapCat / QQ 进程探测
│   ├── netprobe.py          端口与进程只读探针
│   └── autostart.py         注册表自启读写
│
├── panel/                   本地管理面板（零依赖）
│   ├── schema.py            ★ 139 个参数的唯一真源（表单 / 对照表 / 注释都从它生成）
│   ├── configio.py          读写 + 校验 + 手写 YAML（保住注释）
│   ├── server.py            本地 HTTP 服务（只监听 127.0.0.1）
│   ├── runner.py            启停机器人进程（pidfile）
│   ├── voice.py             音色管理（试听 / 上传 / 绑定情绪 / 删除到回收站）
│   ├── __main__.py          python -m panel
│   └── web/                 原生 HTML / CSS / JS
│
├── tools/                   手工工具
│   ├── mock_napcat.py       假 NapCat（离线测试）
│   ├── estimate.py          成本推演
│   ├── report.py            成本报表
│   ├── holidays.py          法定节假日
│   ├── param_table.py       生成 参数对照表.md
│   ├── napcat_login.py      NapCat WebUI API 客户端
│   └── napcat_login_page.py 自刷新扫码页
│
├── persona/                 人设
│   ├── default.md           私聊
│   └── group.md             群聊
│
├── tests/                   850 项离线测试
└── 使用指南.md              面向使用者的完整手册（面板 / 启动 / 排错 / 改人设）
```

运行时生成的东西（**都不进版本库**）：`config.yaml`、`data/`（SQLite、日志、语音缓存）。

---

## 测试

全部**离线、不联网、不花钱**，不需要 NapCat：

```powershell
python tests\smoke.py
python tests\test_p2p3.py
python tests\test_group.py
python tests\test_panel.py
python tests\test_websearch.py
python tests\test_tts.py
python tests\test_voice.py
python tests\test_videopush.py
python tests\test_boot.py
```

| 脚本 | 项数 | 覆盖 |
|---|---|---|
| `smoke.py` | 34 | 导入、配置、数据库、基本流转 |
| `test_p2p3.py` | 87 | 记忆 / 作息 / 主动搭话 / 延迟不占锁 / 记忆隔离 |
| `test_group.py` | 63 | 说话人区分、前缀、限流、按人归属 |
| `test_panel.py` | 97 | schema、读写、校验、进程、HTTP |
| `test_websearch.py` | 75 | 搜索解析、工具循环、主人/客人（含 1 项真联网） |
| `test_tts.py` | 150 | 该不该发、洗稿、合成、拆条、失败降级 |
| `test_voice.py` | 97 | 音色管理、路径安全、情绪绑定 |
| `test_videopush.py` | 127 | 搜索、筛选、去重、发送 |
| `test_boot.py` | 120 | 自启命令、注册表往返、无窗口启动、状态文件 |
| **合计** | **850** | |

> `test_boot.py` 只在**隔离的测试注册表键**上写读删（`Software\qq-companion-test\Run`），
> 不会碰你正在用的那条自启项。

---

## 花了多少钱

同样每天聊 200 轮：**配置对了 0.06 元，配置错了 5.6 元 —— 差 94 倍。**

DeepSeek 的输入分"缓存命中"和"未命中"两档价，**未命中比命中贵 50 倍**（deepseek-flash）。
所以省钱的本质不是少说话，而是**让请求前缀别变**。

本项目做的四件事：

1. **把 messages 排成"越靠前越不变"**：人设 → 说话规矩 → 记忆档案 → 历史对话 → 本轮动态。
   时间戳、心情、想起来的旧事全部塞在最后一条，绝不进 system 前缀。
2. **不用滑动窗口，用块式重建**：超预算时把最老的一大段压成摘要（顺便刷新画像），
   而不是每轮丢掉最老一轮 —— 后者会让缓存命中率当场归零。
3. **能本地打发的就不调模型**：琐碎消息规则表、记忆检索都在本地完成，0 token。
4. **预算守卫兜底**：超了自动降级，再超直接停。

自己算：

```powershell
python tools\estimate.py --compare
```

```
【本项目的默认配置】
thinking 关 + 缓存命中        0.06 元   1.79 元/月   命中 95.1%
【各种做法对比，同样每天 200 轮】
把 thinking 打开              0.53 元  15.95 元/月   命中 76.3%
没有缓存优化                  0.46 元  13.82 元/月   命中  0.0%
10 元/天 的典型配方           5.57 元 167.03 元/月   命中  0.0%
```

上线后查账：`python tools\report.py --days 7`，或看面板的"花费与记忆"页。

---

## 常见问题

**扫码后一直停在"等待确认"，手机上没有"电脑版已登录"**
机器上同时跑了两套 QQ 进程树，它们在抢同一个账号库（`Documents\Tencent Files\nt_qq`）。
把 `QQ.exe` 和 `NapCatWinBootMain.exe` 全部结束，再只拉起一套。

**NapCat 的 `launcher.bat` 报 `provided QQ path is invalid`**
它只从注册表读 QQ 安装路径。QQ 换过盘 / 重装过就会失效。手动指定路径，
或用环境变量 `NAPCAT_QQ_PATH` 指向真实的 `QQ.exe`。

**NapCat 连不上机器人**
1. 确认 `onebot.mode` 和你在 NapCat 里配的方向一致（默认 reverse：机器人监听 3001）
2. `python doctor.py` 会逐条打出来：端口通不通、QQ 在不在跑、反向 WS 配好没
3. 先开哪个都行 —— reverse 模式下 NapCat 每 5 秒重连一次

**改了 `.bat` 之后双击闪退**
Windows `cmd` 解析 **LF-only** 的批处理会出错。仓库里带了 `.gitattributes` 把 `*.bat` 固定成 CRLF，
自己新建脚本时注意别存成 LF。

**语音起不来**
- 先确认 `api_v2.py` 真的在 9880 上听着（`curl http://127.0.0.1:9880/docs`）
- 冷启动第一句要 7~8 秒，`tts.timeout` 默认 60 秒是够的
- 某些被安全策略限制的 Python 写不了 `%TEMP%`，子进程会直接失败 —— 换一个正常的 Python

**面板打不开**
默认 8899，被占用就 `python -m panel 9000`。它只监听 `127.0.0.1`，不要对外暴露。

**记忆串台 / 群里把别人的话当成主人的**
检查 `whitelist.masters` 和 `whitelist.groups`。记忆按会话 ID 隔离，配置错了才会串。

更多排错见 [`使用指南.md`](使用指南.md)。

---

## 风险与免责

**这条路走的是协议端（NapCat），不是官方 QQ 机器人。**

| | 官方 QQ Bot | 本项目（协议端） |
|---|---|---|
| 私聊 | 只能聊管理员手动加的白名单 | 任意人 |
| 群聊 | 个人主体只能进自己是群主的群 | 任意群 |
| 形象 | 有机器人标识 | 就是一个正常 QQ 号 |
| 门槛 | 需要实名主体 | 无 |
| 风险 | 违规封 Bot（不可解封） | **腾讯风控可能封号** |

因此：

- **必须用小号。** 别拿主号试。
- **控制频率。** 默认 `min_interval_seconds` 已经在做这件事（它是"等"不是"丢"，不会掉消息）。
- **主动搭话别开太猛。** 默认每天 2 次、静默 6 小时起，这是安全频率。
- **别加陌生好友、别发广告。** 封号通常不是因为用了协议端，而是因为行为像营销号。
- **登录后手机端会掉线**，这是同一账号被两个端挤的正常现象。小号的手机端留着别登。
- **聊天记录只在本机**（`data/companion.db`），不会上传。但也别把这个库发给别人 ——
  长期记忆里存的是你的工作、喜好、情绪，比原文更浓缩。

**语音克隆**：`refs/` 里的参考音频请用你有权使用的声音素材。用真人（尤其是他人）声音做克隆
可能涉及肖像权、声音权与平台规则，自行判断。

**用途边界**：做陪聊工具没问题，但**别用它冒充某个具体真人去骗人**。

本项目按 MIT 协议"按原样"提供，作者不对使用后果（包括但不限于账号被封、API 费用、
生成内容引发的纠纷）承担责任。

---

## 参与贡献

欢迎 Issue / PR。几条约定：

1. **参数只有一个真源**：`panel/schema.py`。新增参数就在那里加，面板表单和
   `参数对照表.md` 会跟着变（`python tools\param_table.py --write`）。
2. **别让前缀变**：任何进 system 前缀的内容都必须稳定。时间戳、随机数、本轮上下文
   一律放最后一条，否则缓存命中率会归零 —— 这是本项目最贵的错误。
3. **改完跑测试**：9 个脚本、850 项，全部离线。新增行为请顺手加断言。
4. **`config.yaml` 和 `data/` 永远不进库**（已 `.gitignore`）。提 PR 前确认没带上密钥。

---

## 许可证

[MIT](LICENSE) © 2026 CHEN-qc256

---

## English

**qq-companion** is a self-hosted QQ chatbot that puts DeepSeek behind a real QQ account
(via [NapCat](https://github.com/NapNeko/NapCatQQ) + OneBot 11).

It focuses on the four things that usually break believability and budgets:

- **Feels human** — typing delays, message splitting, AI-tone scrubbing, cheap local canned replies
- **Remembers** — SQLite short-term context, periodic compression into summary + user profile,
  locally retrieved long-term facts (no embedding API needed)
- **Knows when to stay quiet** — schedule state machine, proactive messages with four guardrails
- **Stays cheap** — cache-friendly prompt layout, thinking mode disabled, budget guard
  (0.06 CNY per 200 turns on the default config)

Also included: group chat with speaker attribution, master/guest roles, web search via tool calls,
local GPT-SoVITS TTS, Bilibili video digest via BiliNote, and a zero-dependency local web panel
for configuration, logs and spending.

**Requirements**: Windows 10/11, Python 3.10+, 3 pip packages, a DeepSeek API key,
NapCat plus a throwaway QQ account.

**Quick start**:

```powershell
git clone https://github.com/CHEN-qc256/qq-companion.git
cd qq-companion
python -m pip install -r requirements.txt
python setup.py            # interactive wizard, Enter-through-able
python doctor.py           # health check
start.bat                  # launch
```

850 offline tests: `python tests\smoke.py`, `python tests\test_p2p3.py`, …

**Warning**: this uses a third-party protocol implementation. Use a secondary QQ account —
Tencent's risk control may ban accounts that behave like bots. MIT licensed, provided as-is.
