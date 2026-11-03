## RVC 翻唱站 v1.1.1 · 模型市场自定义线路 + 让 AI 帮装

两个更新：**市场线路可自己填**，以及一个**专给 AI 用的一页纸安装入口**（省 token、少踩坑）。

---

### 1. 模型市场：自定义市场地址（线路）

`/market` 页面新增「线路 / 自定义市场地址」卡片：

- 填一个 **HuggingFace 兼容**的镜像 / 反代（能访问 `/api/models` 和
  `/<作者>/<仓库>/resolve/main/<文件>` 即可），点「先测一下」当场验证延迟；
- 「＋ 添加」后与内置两条线路一起参与测速，下载时自动挑最快的那条；
- 慢的 / 常超时的点「停用」，自定义的可以删；界面会拦住「停用最后一条」；
- 只存本机 `RVCSVC/_market_lines.json`，不上传任何地方；删掉文件即恢复默认；
- 仍支持环境变量强制指定（此时界面只读，避免两边打架）：
  `RVC_MARKET_LINES="公司反代=https://hf.corp.example.com,官方=https://huggingface.co"`

地址校验：协议必须是 http/https、必须带域名、不许带账号密码、不许路径穿越、最多 12 条、
单条 ≤240 字符；重复地址会被挡。

### 2. 让 AI 帮你装（给 Agent 的省 token 入口）

以前让 AI「读仓库然后自己装」，它得翻几百个文件（还带着整个 RVC-WebUI），又慢又容易读一半就开始猜。
现在仓库根目录多了一页纸：

- `AI-INSTALL.md` —— 专给 AI 看：一条命令、参数表、成功判定、失败对照表，明确写「不需要读其它文件」；
- `install/ai_install.ps1` —— 无人值守安装器：8 条线路挑一条下载最新 `一键安装.bat`、
  用环境变量喂参数、静默跑完、校验产物、最后吐一行 JSON；
- `install.json` —— 机器可读清单（入口、参数、校验点、端口、产物位置）。

用法（把仓库地址发给 AI，或自己粘）：

```powershell
$dir = "D:\RVC-Cover-Station"
powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://raw.githubusercontent.com/panxian-AI/rvc-cover-station/main/install/ai_install.ps1' -OutFile $env:TEMP\rvc_ai_install.ps1; & $env:TEMP\rvc_ai_install.ps1 -Dir '$dir' -Gpu auto"
```

- `-DryRun` 只验证线路和参数（约 20 秒），不装任何东西；
- `-Gpu auto|gpu|cpu`、`-Tag v1.1.0`（锁版本）、`-Start`（装完启动）、`-Installer <路径>`（离线）；
- 结果最后一行是 JSON：`{"ok":true,...,"version":"1.1.1"}`；中文以 `\uXXXX` 转义输出，
  所以 GBK 控制台里也不会被吃成乱码；
- 失败会写 `<目录>\_install_failed.txt`（含 `Stage=`），**重跑同一条命令**即断点续装。

`一键安装.bat` 同步加了无人值守能力（`RVC_UNATTENDED` / `RVC_DEFDIR` / `RVC_GPU` /
`RVC_NO_START` / `RVC_DRYRUN`），双击使用时的交互流程完全不变；
另外测速全挂时会用外部脚本探到的线路兜底，不再直接放弃。

### 3. 其它

- 安装位置校验改用 `%%~fp` 归一化：`E:/x` 这种写法、多余结尾斜杠都能正确处理，盘根（`E:\`）会被拦住；
- 回归测试：`e2e_market.py` 从 59 项加到 **92 项**（线路增删停用、地址校验、配置损坏兜底、
  环境变量优先级、HTTP 接口），新增 `dashboard/e2e_ai_install.py` **59 项**
  （一页纸内容、install.json 一致性、ps1 编码/语法/参数、bat 无人值守钩子、
  用假安装器跑通成功/失败/缺产物/盘根四种结局）。全套 **11 套 526 项全绿**。

### 升级

老版本在控制台点「检测更新 / 立即更新」即可（更新器同样先给 8 条线路测速再挑最快的下）。
模型、成品、访客记录、白名单、高级设置、自定义线路都不会动。

> 模型版权归原作者；模型市场只做检索 + 下载 + 校验 + 装机，不镜像也不分发模型文件。
