<div align="center">

# 🎤 rvc-cover-station · RVC 翻唱点歌台

**开箱即用的 AI 翻唱站：访客点歌 → 站长审批排队 → GPU 制作 → 在线下载**

[![Release](https://img.shields.io/badge/Release-v1.0.1-blue)](https://github.com/panxian-AI/rvc-cover-station/releases)
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
- 🎛 控制台：服务一键开关、公网隧道地址、制作队列（置顶/上移/下移/移出）
- ⚙️ 高级设置：**同时制作数量**（1~6 单并行，随时改随时生效）+ 成品默认翻唱参数
  （音高算法 / 声线检索强度 / 滤波半径 / 混响 / 回声 / 分离聚合度 / 人声与伴奏音量）
- 📥 模型导入：控制台点按钮弹 Windows 文件选择框，自动识别 `.pth` 是否为 RVC 声线、
  自动配对 `.index` 索引，确认后直接装进 WebUI
- 🔄 版本更新：打开控制台自动查最新版，有新版本弹窗显示更新内容，一键更新代码
  （旧文件自动备份；模型、成品、访客记录、白名单、设置都不动）
- ✅ 审批：批准入队（先到先来，可手动调整顺序）、拒绝、失败自动重试与冷却
- ⭐ 白名单：访客申请（附理由）→ 站长批准 → 该设备+该网络后续点歌**免审批直通**；
  三要素（访客 id + 设备指纹 + IP）全匹配才生效，多层防批量刷
- 👥 申请记录 + 访客记录（IP / 地区 / 设备 / 访问搜索申请次数）

**安全**
- 🛡 管理接口只认本机直连，公网访问一律 403，并记入 `security.log`
- 验证码阶梯锁、取图/搜索/申请多层限流、全局取图闸、请求体上限、安全响应头
- 慢连接耗光防护（并发连接上限）、写盘节流、路径穿越/目录清理防护
- 附带 8 套回归测试共 318 项断言（`dashboard/e2e_*.py`）

## 🚀 三步上手（推荐给完全不懂代码的人）

1. 到 [**Releases**](https://github.com/panxian-AI/rvc-cover-station/releases) 下载 **一键安装包**（zip）并解压
2. 双击 `一键安装.bat`，剩下全自动：
   先对「直连 GitHub + 7 个加速前缀」**测速自动选最快线路**（可手动换线）→
   下载本仓库 → 安装内置 Python 3.12（只装在安装目录里，不动系统）→
   按显卡选装 CUDA/CPU 版依赖 → 拉取基础模型与 ffmpeg → 生成启动入口
3. 双击 `启动RVC.bat` → 控制台点「⚡ 一键开启服务」→ 本机打开 `http://127.0.0.1:8899/`

不想用了？双击安装目录里的 `卸载程序.bat`（可选保留翻唱成品、访客记录与 Cookie）。

> 🔄 安装器每次运行都会**自动查询并安装当前最新发布版本**；查不到版本号或该版本包拉不动时，
> 自动回落 `main` 分支最新代码。想固定版本就在 bat 顶部填 `RVC_TAG=v1.0.2`；想固定线路就填 `GH_PREFIX`。

> 🔧 安装器顶部有「下载地址配置区」：GitHub 加速前缀、Python 安装包地址、
> pip 镜像源等都可以自行定义；所有下载按字节数校验。
> 要求：Windows 10 1803+（自带 curl）；重跑安装器安全（断点续装）。

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
  小肥鱼声线可用脚本一键拉取。

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
- 翻唱作品仅供个人学习娱乐，请勿用于商业用途；请遵守当地法律法规与平台条款。

## 📖 更多文档

完整的运行与维护细节见 [docs/使用说明.md](docs/使用说明.md)。

## 📄 许可证

[AGPL-3.0](LICENSE)。3333 后端衍生自
[astrbot_plugin_matsuko_cover](https://github.com/sdfsfsk/matsuko_cover)（Matsuko），
后者基于 [CCYellowStar2/astrbot_plugin_rvc_svc](https://github.com/CCYellowStar2/astrbot_plugin_rvc_svc) 扩展——感谢原作者。
