# RVC 翻唱站 v1.3.1 · 启动前体检（网易云配置 + 日志目录）

## 有用户遇到了什么

装在 `D:/RVC` 后，面板点「一键开启」直接弹出：

    WebUI 启动失败: 启动失败: [Errno 2] No such file or directory:
    'D:/RVC/RVC-Data/RVC-WebUI/logs/webui_service.log'

原因：面板开服务时要把输出写进 `RVC-WebUI\logs\webui_service.log`，
但**全新安装时 logs 目录还不存在**——WebUI 第一次跑才会建它，而面板抢在
前面打开这个文件，直接 Errno 2 崩掉。

## 这版怎么改（三层兜住）

1. **面板**：开任何服务前先把日志目录补好（`os.makedirs(exist_ok=True)`），
   目录被删了也能正常起；WebUI 缺 `webui.py` 或目录整个缺失时，
   报「安装可能没完成，重新双击一键安装」——不再抛系统错误码。
2. **安装器**：装完自动创建 `RVC-WebUI\logs`；并且**当场检测网易云 Cookie**
   有没有配置——没配就在完成页写明「点歌搜索会失败」和配置方法
   （双击 `RVCSVC\ncmapi\设置网易云Cookie.bat`）。
3. **控制台**：「服务状态」里网易云后面挂「⚠ 未配 Cookie」标记（悬停看指引），
   服务状态卡片里一行写清配置步骤；「一键开启」的完成提示也带上这条。

网易云没配 Cookie 时服务照样能起（不拦启动），但面板会一直明示这件事，
不会再让你等到搜索失败才发现原因。

## 老用户怎么办

照常自更新即可。更新完控制台还是旧界面：关掉面板窗口，重新双击「启动RVC.bat」。

## 测试

`e2e_advanced.py` 新增 8 项（第 8 节「启动前体检」）：日志目录 makedirs 兜底、
WebUI 缺文件/缺目录的人话报错、网易云 Cookie 三种状态判定、
`/api/status` 带 `ncm_preflight`。全套 **14 套 632 项**全部通过。
