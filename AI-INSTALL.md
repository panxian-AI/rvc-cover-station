# 给 AI 的安装说明（RVC 翻唱站）

> 这一页是**写给 AI Agent 看的**（WorkBuddy / Cursor / Claude Code / Codex / 其它能跑命令的助手）。
> **你不需要读仓库里的其它文件**：照下面三步做就能装完。仓库里全是 Windows 端 Python + WebUI 代码，
> 逐个文件读一遍既浪费时间也烧 token。

## 这是什么

Windows 上的 AI 翻唱整套栈：RVC 训练/推理 WebUI（7865）+ 翻唱后端（3333）+ 网易云代理（3002）
+ 监控面板（8899）。安装器会自动完成：多线路测速 → 下载代码 → 装便携 Python 3.12（不写系统 PATH）
→ 建两个 venv → 装依赖（可选 CUDA 版 torch）→ 下载基础模型（rmvpe / ffmpeg / 示例声线）。

**不需要管理员权限**，**不需要预装 Python**，只要求 Windows 10/11 x64 + 联网 + 磁盘 ≥12GB（GPU 版）/ 6GB（CPU 版）。

## 第一步：一条命令装完

```powershell
$dir = "D:\RVC-Cover-Station"   # 装到哪就改这里（不要用盘根 D:\ 或 C:\）
powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://raw.githubusercontent.com/panxian-AI/rvc-cover-station/main/install/ai_install.ps1' -OutFile $env:TEMP\rvc_ai_install.ps1; & $env:TEMP\rvc_ai_install.ps1 -Dir '$dir' -Gpu auto"
```

- `-Gpu auto` 自动判断有没有 NVIDIA 卡；想强制就写 `gpu` 或 `cpu`。
- 装的过程会打印大量 pip 输出，正常；耗时十几分钟到半小时（看网速）。
- 脚本自己会在 8 条 GitHub 线路里挑能用的，并把这条线路传给安装器，不需要你处理网络。

只要判断一句话：**最后一行 JSON 里 `ok` 是不是 `true`**。

```json
{"ok":true,"exit_code":0,"dir":"D:\\RVC-Cover-Station","gpu":"gpu","version":"1.1.2","missing":[],"panel":"http://127.0.0.1:8899","error":""}
```

## 第二步：先空跑核对（可选，约 20 秒）

不确定环境能不能联网时，先加 `-DryRun`：它会下载安装器、校验目录与显卡模式，然后退出，**不装任何东西**。

```powershell
& $env:TEMP\rvc_ai_install.ps1 -Dir $dir -Gpu auto -DryRun
```

`ok:true` 就说明线路通、参数对，去掉 `-DryRun` 正式跑。

## 第三步：告诉用户怎么用

装完不用你启动（无人值守默认不启动面板）。请把下面三行原样转告用户：

1. 双击安装目录里的 `启动RVC.bat`，会自动打开控制台 <http://127.0.0.1:8899>
2. 控制台点「⚡ 一键全部启动」，等三个服务都变绿
3. 想加声线：控制台右上角「🧩 模型市场」搜关键词，选 `.pth` + `.index`，一键下载安装（装完重启 3333 后端）

## 参数表

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-Dir` | `%LOCALAPPDATA%\RVC-Cover-Station` | 安装目录，不能是盘根；已存在的安装会被原地升级 |
| `-Gpu` | `auto` | `auto` / `gpu`（CUDA torch）/ `cpu` |
| `-Tag` | 空 | 锁定版本，例如 `v1.1.0`；空 = main 最新（推荐） |
| `-Start` | 不启动 | 装完自动拉起面板（有桌面的机器才用） |
| `-DryRun` | 关 | 只验证，不安装 |
| `-Installer` | 空 | 本地已有 `一键安装.bat` 时直接用它（离线 / 测试） |

## 失败怎么处理

| JSON 里的 `error` / 现象 | 处理 |
| --- | --- |
| `下载安装器失败，所有线路都不通` | 网络问题。让用户连上网/关掉坏的代理后**重跑同一条命令** |
| `exit_code:1`，`Stage=download` | 下载代码失败，重跑会自动换线路 |
| `exit_code:1`，`Stage=deps` | pip 中断：**直接重跑**，venv 已存在会续装 |
| `exit_code:1`，`Stage=python` | Python 装失败：重跑一次；仍失败让用户手动装 3.12 到 `<Dir>\RVC-Data\runtime\python312` |
| `安装目录不能是盘根` | 换 `-Dir`，例如 `D:\RVC` |
| `missing` 里有 `webui` / `py_web` | 安装没完成，重跑；连续两次失败把 `error` 原文给用户 |

**升级**：重跑同一条命令即可（安装器是长效模式，拉 main 最新代码，只覆盖程序、不动模型和成品）。
**卸载**：让用户双击安装目录里的 `卸载程序.bat`（可选保留个人数据）。

## 给不想跑脚本的场景

如果当前环境不允许执行 PowerShell，就让用户自己双击仓库里的 `一键安装.bat` ——
它就是图形版的同一套流程（会弹文件夹选择框、问有没有 N 卡）。不要把 bat 的逻辑抄一遍重写。

机器可读的等价清单在 [`install.json`](install.json)（入口、参数、校验点、产物位置），
给人看的完整文档在 [`docs/使用说明.md`](docs/使用说明.md)。
