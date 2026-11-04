<div align="center">

# 🎤 rvc-cover-station · RVC 翻唱点歌台

**开箱即用的 AI 翻唱站：访客点歌 → 站长审批排队 → GPU 制作 → 在线下载**

[![Release](https://img.shields.io/badge/Release-v2.0.0-blue)](https://github.com/panxian-AI/rvc-cover-station/releases)
[![License](https://img.shields.io/badge/License-AGPL--3.0-green)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2B-0078D6)](#一键安装给完全不懂代码的人)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB)](#快速开始)

一键安装 · 网页审批队列 · 成品复用 · 公网分享 · 管理仅限本机

</div>

---

自托管的 RVC AI 翻唱站，同时是 [astrbot_plugin_matsuko_cover](https://github.com/sdfsfsk/matsuko_cover)
AI 点歌插件的**自托管后端**（兼容其 RVC API 调用方式）。

> 本项目是给 QQ 机器人插件「AI 点歌」当后端用的产物演化而来，也可独立当一个小型翻唱站运营。
> **模型与 RVC-WebUI 需自行准备**，本仓库只含调度与业务层代码。

## ✨ 功能亮点

**访客侧（公网可访问）**
- 🎵 搜歌（走本地网易云代理，图像验证码防刷）、选声线、点播翻唱 / 文字转语音 / 清唱
- 📥 提交申请后排队等待，重开页面仍能看到自己的申请与下载链接
- ⚡ 成品复用：同参数做过的歌秒回，不重复烧显卡

**管理侧（仅限本机 127.0.0.1，无密码、无远程管理入口）**
- 🎛 控制台：服务**三路并行**一键开关（v1.2.1 起 WebUI/后端/网易云同时拉起，不再串行等待）、
  **公网隧道一键开关**（v1.2.2 起页面里直接开/关，不用再挂命令行窗口，地址自动刷新）、
  制作队列（置顶/上移/下移/移出）
- ⚙️ 高级设置：**同时制作数量**（1~6 单并行，随时改随时生效）+ 成品默认翻唱参数
  （音高算法 / 声线检索强度 / 滤波半径 / 混响 / 回声 / 分离聚合度 / 人声与伴奏音量）
- 🧩 **模型市场**（`/market`）：在公开免费模型站（HuggingFace 模型库 / 数据集合集）里搜声线，
  点开看文件清单选 `.pth` + 配套 `.index`，一键下载并安装；下载前给各线路实测测速挑最快的那条，
  下完依次核验**大小 → SHA-256 → torch 权重结构**，全过才落进 WebUI；
  线路也能自己加：页面上填自己的镜像 / 反代地址，测速通过就参与选线，慢的可以停用；
  装好后页面右上角有 **🔄 更新声线** 按钮，一键重启 3333 后端让新声线立刻可用（不用再手动关窗口重开）
- 📥 模型导入：控制台点按钮弹 Windows 文件选择框，自动识别 `.pth` 是否为 RVC 声线、
  自动配对 `.index` 索引，确认后直接装进 WebUI；导完就地 **🔄 更新声线** 一键重启后端
- 🗂 **模型管理页**（`/models`）：全部声线列表 + 索引配对情况 + 孤儿索引单列，
  两段式删除（先预览后确认），防路径穿越
- 🩺 **启动前体检**（v1.3.1）：安装器装完建 WebUI 日志目录 + 检测网易云
  Cookie 配置并明确提示；面板开服务前自动补目录、缺文件给人话报错
  （修掉全新安装「一键开启」报 Errno 2 的问题）；控制台显示网易云配置状态
- 🔓 **验证码解锁请求**（v1.3.0）：访客连错被锁时页面给出明确提示，可向站长
  发解锁请求（限频），控制台一键同意/拒绝——同意只解当次，错误计数保留，
  再错照常锁，绝不从此豁免
- 🪶 **模型按需加载**：人声分离器不再开机常驻内存，首个任务现场加载、空闲 15 分钟
  自动释放（`RVC_UVR_IDLE_UNLOAD` 可调）——装再多声线也不白占内存
- ⚡ **启动提速**（v1.2.1）：后端 3333 从约 20 秒降到约 7 秒（torch / UVR 重模块
  推迟到真正用到时才导入），「一键开启」三路并行再省 15~30 秒
- 🚀 **首曲不再等**（v1.3.2）：服务就绪后在后台把 torch、分离模型、rmvpe 预加载好，
  第一首歌省掉约 70 秒的冷启动（分离模型 43 秒 + F0 首次 30~40 秒）
- 🎛 **设备自适应**（v1.3.2）：按显卡显存/物理核/内存自动选档位（极速/均衡/省内存），
  线程数按物理核给、内存不够自动不预热；控制台「服务状态」里直接显示当前档位
- 🧱 **拆包重构**（v2.0.0）：`panel.py` 从 2318 行瘦到 200 行，只剩「怎么起服务」；
  实现按依赖分层搬进 `dashboard/panelkit/`——`core`（常量/工具）→ `security` / `sysinfo`
  / `services` / `pages` / `trayapp` → `routes_get` / `routes_post` → `server`。
  以后改一个功能不用在两千行里翻，也不会因为手滑动到别处
- 🗂 **控制台分多页**（v2.0.0）：原来一页塞满 13 张卡片，现在拆成 **概览 / 任务 / 访客 /
  设置** 四页，外加原有的模型市场、模型管理、监控、自助翻唱、访客页。样式与脚本收到
  `dashboard/assets/` 共用（改完刷新就生效），页面本身只留结构，一屏就能看完一类事
- 🔒 **依赖锁版本**（v2.0.0）：顶层 `requirements.txt` 全部写成 `==`（含传递依赖），
  `dashboard/deps.py` 启动时核对机器上实际装的版本，被别的程序顺手升过会在控制台
  「概览」页直接提示，并给出可直接粘贴的修复命令——不再出现「上游一升级，功能突然不行」
- 🧵 **翻唱提速**（v1.3.2）：伴奏变调与推理并行、同一模型不再重复加载，
  每首歌再省 5~10 秒；后端日志会打「本首耗时」分段账
- 🪟 **静默启动**（v1.4.0）：双击 `启动RVC.bat` 不再挂着黑窗口——由 `pythonw`
  在后台拉起面板、自动开浏览器，窗口一闪即走；出错不闷掉，弹原生消息框说清
  原因和下一步。排障想看窗口就 `启动RVC.bat --console`（或 `set RVC_CONSOLE=1`）
- 🖱 **任务栏图标**（v1.4.0）：通知区常驻一个图标，**左键单击呼出菜单**——打开
  控制台 / 打开安装目录 / 一键开启服务 / 停止全部服务 / 停止服务并退出面板；
  左键双击直接开控制台。黑窗口没了也能一键收摊
- 📜 **启动日志进控制台**（v1.4.0）：静默之后面板自己的输出落在
  `dashboard/panel_console.log`，控制台「服务状态」里点「📜 启动日志」就能看，
  超 2MB 自动轮转；再也没有「窗口一闪，什么都没看见」
- 🔇 **开启服务不再弹终端**（v1.4.2）：点「一键开启」时三个服务改用 `pythonw`
  拉起，桌面不会再冒出三个终端窗口（Win11 把 Windows Terminal 设成默认终端时，
  光靠 `DETACHED_PROCESS` 是拦不住的）；网易云服务的 ANSI 开关也不再借 `os.system`
- 📋 **自助翻唱任务进总面板**（v1.4.2）：在「自助翻唱」页下的单，控制台里新增
  「🎤 自助翻唱任务」卡片——正在做的（阶段 + 已用时）、排队的、最近完成的
  （直接试听）都在一页里，不用再切回翻唱页看进度
- 🎯 **排队位次与制作进度**（v1.4.2）：访客页每单多了四步进度条（提交 → 排队 →
  制作 → 完成）和一句人话「前面还有 2 单 · 你在第 3 位 · 预计约 2 分 15 秒开始」；
  制作中会写清当前阶段（下载 / 分离 / 演唱、收尾导出）、已用多久、还剩多久。
  控制台同样：正在做的每单独占一行带进度条，「申请记录」的状态列直接写位次和阶段。
  位次是队列里的真实下标，预计时间按最近成品的实际耗时中位数算，不写死常数
- 💾 **数据可备份、可迁移**（v1.5.0）：控制台新增「数据备份与迁移」卡片。一键把
  访客记录、申请与队列、白名单、高级设置、成品库索引（可勾选连 mp3 一起）和一份
  已装声线清单打包成带 sha256 的 `.zip`；换硬盘、重装系统、手改 JSON 改坏了都能
  导回来。导入分**覆盖**（整份替换）和**合并**（按业务主键去重，两台机器各攒的
  记录合成一份，同一个包导两次也不会翻倍）两种；导入前先展示「本机现在 → 导入后」
  的对照表，动手前自动把本机数据再存一份，写失败自动整体回滚。备份落在
  `RVCSVC/_backup`，自动存档按份数裁剪，不会把盘撑爆
- 🔄 版本更新：打开控制台自动查最新版，有新版本弹窗显示更新内容，一键更新代码
  （**8 条线路先并行测速再挑最快的下**，失败自动换线；旧文件自动备份；
  模型、成品、访客记录、白名单、设置都不动）
- ✅ 审批：批准入队（先到先来，可手动调整顺序）、拒绝、失败自动重试与冷却
- ⭐ 白名单：访客申请（附理由）→ 站长批准 → 该设备+该网络后续点歌**免审批直通**；
  四要素（访客 id + 设备指纹 + IP + 设备密钥）全匹配才生效；每个身份每天 12 单、
  每小时 6 单、20 秒一单；出现多设备/多网络共用同一身份自动挂起并告警
- 👥 申请记录 + 访客记录（IP / 地区 / 设备 / 访问搜索申请次数）

**安全**
- 🛡 公网暴露面**白名单制（fail-closed）**：隧道里只能打开访客页和 4 个访客接口，
  其它路径（含以后新增的管理接口）默认不可见；管理接口只认本机直连
- 🧱 Host 头校验（防 DNS rebinding：恶意域名解析到 127.0.0.1 也拿不到控制台）
- 🚫 扫描/越权探测计数：同一 IP 10 分钟碰 20 次非公开路径 → 封 30 分钟，控制台可解除
- ⏸ 一键暂停公网访问：被刷时访客只看到维护页，本机控制台照常可用
- 🔒 后端 3333 / WebUI 7865 默认只绑回环（`RVC_BIND=0.0.0.0` 可显式放开），
  不再把无鉴权的模型管理交给整个局域网
- 验证码阶梯锁、取图/搜索/申请多层限流、全局取图闸、请求体上限、安全响应头
- 慢连接耗光防护（并发连接上限）、写盘节流、路径穿越/目录清理防护
- 附带 18 套回归测试共 914 项断言（`dashboard/e2e_*.py`）

## 🚀 三步上手（推荐给完全不懂代码的人）

1. 到 [**Releases**](https://github.com/panxian-AI/rvc-cover-station/releases) 下载 **一键安装包**（zip）并解压
2. 双击 `一键安装.bat`，剩下全自动：
   先对「直连 GitHub + 7 个加速前缀」**测速自动选最快线路**（可手动换线）→
   下载本仓库 → 安装内置 Python 3.12（只装在安装目录里，不动系统）→
   按显卡选装 CUDA/CPU 版依赖 → 拉取基础模型与 ffmpeg → 生成启动入口
3. 双击 `启动RVC.bat` → 控制台点「⚡ 一键开启服务」→ 本机打开 `http://127.0.0.1:8899/`

想**彻底退出**（释放显卡/内存、端口全部空出来）？三种方式任选：

- **面板 → 监控面板 → ⏹ 一键退出服务**：停后端 → 关网易云 → 关 WebUI → 断开公网隧道 → 关面板。
  它会**确认端口真的释放才退出**；万一有进程停不掉，会把占用的 PID 报给你，不会假装退出就完事。
- **双击安装目录里的 `停止RVC.bat`**：不依赖面板，直接按端口收摊。面板已经关掉、
  或者上面那种停不掉的情况，用它兜底。想先看会停谁，可以跑
  `python dashboard\stop_service.py --dry-run`（只列清单，不真停）。
- **只停服务、留着面板看状态**：控制台点「⏹ 全部停止」，面板和公网隧道都留着。

不想用了？双击安装目录里的 `卸载程序.bat`（可选保留翻唱成品、访客记录与 Cookie）。

> 🔄 安装器每次运行都会**自动查询并安装当前最新发布版本**；查不到版本号或该版本包拉不动时，
> 自动回落 `main` 分支最新代码。想固定版本就在 bat 顶部填 `RVC_TAG=v1.0.2`；想固定线路就填 `GH_PREFIX`。

> 🔧 安装器顶部有「下载地址配置区」：GitHub 加速前缀、Python 安装包地址、
> pip 镜像源等都可以自行定义；所有下载按字节数校验。
> 要求：Windows 10 1803+（自带 curl）；重跑安装器安全（断点续装）。

## 🤖 让 AI 帮你装（省 token 的入口）

用 WorkBuddy / Cursor / Claude Code 这类能跑命令的助手时，**把这个仓库地址发给它就行**：

> 帮我装这个：https://github.com/panxian-AI/rvc-cover-station

它会读 [`AI-INSTALL.md`](AI-INSTALL.md)（一页纸：入口命令、参数、成功判定、失败对照表），
跑一条命令装完，**不需要把仓库代码逐个读一遍**——比让它自己摸索安装省下大量 token 和时间。

手工一句话版（自己粘进命令行也行）：

```powershell
$dir = "D:\RVC-Cover-Station"
powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://raw.githubusercontent.com/panxian-AI/rvc-cover-station/main/install/ai_install.ps1' -OutFile $env:TEMP\rvc_ai_install.ps1; & $env:TEMP\rvc_ai_install.ps1 -Dir '$dir' -Gpu auto"
```

- 无人值守：不弹文件夹选择框、不问显卡、装完不自动启动，参数全部走 `-Dir` / `-Gpu` / `-Tag`
- 先空跑：加 `-DryRun`，只验证线路和参数，不装东西（约 20 秒）
- 机器可读清单：[`install.json`](install.json)；结果最后一行是 JSON（`ok` / `version` / `missing` / `error`）
- 升级 = 重跑同一条命令（长效模式拉 main 最新，只覆盖程序，不动模型与成品）

## 架构

```
公网访客 ── Cloudflare Tunnel ──┐
                                ▼
QQ群/AstrBot 插件 ──► 8899 面板(访客页/控制台) ──► 3333 翻唱后端(app_rvc.py) ──► 7865 RVC-WebUI
                                  └────────────► 3002 网易云搜索代理(ncmapi)
```

| 端口 | 服务 | 说明 |
|---|---|---|
| 8899 | dashboard/panel.py | 面板：本机=控制台，公网=访客点歌页 |
| 3333 | RVCSVC-API/app_rvc.py | 翻唱推理后端（人声分离 + RVC 换声），与 matsuko 插件接口兼容 |
| 3002 | ncmapi/ncm_proxy.py | 本地网易云搜索代理（标准库实现，兼容 NeteaseCloudMusicApi 常用端点） |
| 7865 | RVC-WebUI/ | RVC 官方 WebUI（**代码随仓库附带，为上游原版 MIT 代码**） |

### dashboard 的模块分层（v2.0.0 起）

```
dashboard/
  panel.py              入口：起 HTTP 服务、挂托盘、出错怎么办（~200 行）
  panelkit/
    core.py             常量 / 路径 / 可选子模块 / 页面缓存 / HTTP 小工具（最底层）
    security.py         公网路径白名单、探测封禁、安全日志
    sysinfo.py          端口 · 进程 · GPU · 网卡 · 日志 · 依赖核对的数据源
    services.py         服务启停、启动前体检、公网隧道
    pages.py            页面登记表 + assets/ 静态资源（越界一律 404）
    trayapp.py          任务栏图标
    routes_get.py       GET 路由（页面 + 查询接口）
    routes_post.py      POST 路由（动作 + 审批 + 导入导出 + 更新）
    server.py           PanelServer（连接上限）+ Handler 基类 + 路由分发
  assets/               控制台共用的 console.css 与各页脚本
  console_page.html         概览：服务状态 / 版本 / 公网地址 / 日志
  console_tasks_page.html   任务：制作队列 / 自助翻唱 / 待审批 / 申请记录
  console_guest_page.html   访客：白名单 / 解锁请求 / 公网安全
  console_settings_page.html 设置：高级设置 / 模型 / 数据备份
```

分层是单向的：下层不 import 上层（`core` 连 `security` 都不 import），
所以不会有循环导入；老代码 `import panel` 仍然能用——`panel.py` 把名字重新导出，
并且 `panel.SERVICE_PORTS = ...` 这类赋值会转发到真正持有它的子模块。

### 依赖锁

| 文件 | 管什么 |
|---|---|
| `requirements.txt` | 面板层依赖，**全部 `==` 锁死**（含 urllib3/certifi 等传递依赖） |
| `RVCSVC-API/requirements.txt` | 翻唱后端直接依赖（torch 版本按显卡选，见文件顶部注释） |
| `RVCSVC-API/requirements-freeze.txt` | 后端完整环境快照 |
| `dashboard/deps.py` | 启动/打开控制台时核对锁定值与实际安装，漂移会提示 + 给修复命令 |

## 🧰 手动安装（进阶）

1. **准备模型**：运行 `setup/下载基础模型.bat` 拉取基础模型与 ffmpeg；声线模型（.pth/.index）
   自己训练或获取后放入 `RVC-WebUI/assets/weights/` 与 `assets/indices/`。
2. **目录布局**：本仓库放在 `RVCSVC/`，与 `runtime/`（内置 Python 3.12）、`RVC-WebUI/` 平级；
   路径推导见 `dashboard/paths.py`，搬盘不用改代码。
3. **安装依赖**：
   ```bash
   # 面板层（很少几个）
   pip install -r requirements.txt

   # 翻唱后端（3333）建议独立 venv，Python 3.12
   python -m venv RVCSVC-API/venv
   RVCSVC-API/venv/Scripts/pip install -r RVCSVC-API/requirements.txt
   # torch 按显卡选 CUDA 版本，命令见 requirements.txt 顶部注释
   ```
   另需把 `ffmpeg.exe` / `ffprobe.exe` 放进 `RVCSVC-API/`（构建页有下载地址），
   人声分离模型 `5_HP-Karaoke-UVR.pth` 放进 `RVCSVC-API/uvr5/uvr_model/`。
   完整环境锁定（含全部间接依赖）见 `RVCSVC-API/requirements-freeze.txt`。
4. **启动**：双击 `启动RVC.bat` → 控制台点「⚡ 一键开启服务」。
5. **网易云登录态（可选）**：双击 `ncmapi/设置网易云Cookie.bat`，在窗口里粘贴浏览器复制的
   Cookie，自动提取 MUSIC_U 并验证。不配也能用（匿名模式，VIP/独家曲目拿不到）。
6. **公网开放（可选）**：控制台里一键开启 Cloudflare Tunnel（需自备 `cloudflared.exe`），
   把生成的临时地址发给朋友即可。

## RVC-WebUI 与模型说明

- `RVC-WebUI/` 收录的是 [RVC-Project/Retrieval-based-Voice-Conversion-WebUI](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)
  的原版代码（MIT 许可，见其 LICENSE），便于一次克隆全套跑起来；上游更新可自行覆盖同步。
- **大文件不入库**（GitHub 单文件 100MB 上限）：基础模型与 ffmpeg 用一键脚本补齐——
  ```bash
  # 双击 setup/下载基础模型.bat，或：
  python setup/下载基础模型.py
  ```
  脚本自动下载已验证镜像的 **rmvpe.pt、ffmpeg/ffprobe、小肥鱼(FeiyuVoice) 公开声线**，
  并内置 hubert 的 transformers 配置；hubert 本体、roformer 分离模型、UVR5 权重
  会给出精确的手动获取指引（也支持 `--url 键=直链` 覆盖自动下载，脚本按字节数校验）。
- **声线模型不入库**：`assets/weights/*.pth` + `assets/indices/*.index` 由使用者自行训练或获取，
  小肥鱼声线可用脚本一键拉取；也可以在控制台进入 **模型市场** 搜公开模型一键装。

### 模型市场的数据来源

| 来源 | 说明 |
| --- | --- |
| HuggingFace 模型库 | RVC 社区最主要的公开托管地，按下载量 / 喜欢 / 更新时间浏览，支持关键词搜索 |
| HuggingFace 数据集合集 | 社区上传的「模型合集包」，一个包里几十上百个声线（例如 338 个 `.pth` 的合集） |
| 下载线路 | `hf-mirror.com`（国内镜像）+ `huggingface.co`（官方），逐文件实测 256KB/128KB 速度后选最快的 |

只列**真正带 `.pth`** 且判定为语音转换类（有 `.index` / 带 rvc 标签 / 名字含 rvc）的仓库，
`gated`、私有、纯 LLM 权重的仓库会被过滤掉。下载后核验不通过的文件不会进入 `weights`。

线路可以在 `/market` 页面上自己加（填一个 HuggingFace 兼容的镜像 / 反代地址即可），
加进去后会跟内置线路一起参与测速，慢的就停用；地址只存本机 `RVCSVC/_market_lines.json`，
也可以用环境变量一次性指定：`RVC_MARKET_LINES="名字A=https://a.example.com,名字B=https://b.example.com"`。

> ⚠️ 模型版权归原作者所有：下载使用前请看仓库里的 LICENSE / 作者说明，
> **不要**用真人（明星、主播、他人）的声音模型做商业化或公开传播。

## 🔌 对接 AstrBot AI 点歌插件

在 [astrbot_plugin_matsuko_cover](https://github.com/sdfsfsk/matsuko_cover)（或其上游
[CCYellowStar2/astrbot_plugin_rvc_svc](https://github.com/CCYellowStar2/astrbot_plugin_rvc_svc)）
的配置里填：

- 网易云 API 地址 → `http://电脑IP:3002`
- RVC API 地址 → `http://电脑IP:3333`

接口签名（`/convert`、`/wwy_downloader` 及参数名）与插件完全兼容；`RVCSVC-API/自检_模拟插件调用.py`
可在改完后端后模拟插件真实调用做自检。

## ⚠️ 安全须知

- 管理功能**只在运行面板的电脑上可用**，公网打开只会看到访客点歌页；
- `trycloudflare.com` 临时域名会被互联网扫描器发现（证书透明度日志），不用时建议关闭公网窗口；
- 网易云 Cookie 等同登录凭证：已通过 `.gitignore` 排除，请勿外传；
- 翻唱作品仅供个人学习娱乐，请勿用于商业用途；请遵守当地法律法规与平台条款；
- 模型市场只做「按公开接口检索 + 下载 + 校验 + 装机」，不镜像也不分发模型文件；
  模型授权以原仓库为准，用真人声音模型做商业化/公开传播可能侵权。

## 📖 更多文档

完整的运行与维护细节见 [docs/使用说明.md](docs/使用说明.md)。

## 📄 许可证

[AGPL-3.0](LICENSE)。3333 后端衍生自
[astrbot_plugin_matsuko_cover](https://github.com/sdfsfsk/matsuko_cover)（Matsuko），
后者基于 [CCYellowStar2/astrbot_plugin_rvc_svc](https://github.com/CCYellowStar2/astrbot_plugin_rvc_svc) 扩展——感谢原作者。
