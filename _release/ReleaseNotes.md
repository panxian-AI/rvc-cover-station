## RVC 翻唱站 v1.1.3 · 修好「生成报 KeyError: 'data'」

**症状**：模型市场里下好的声线，点生成就报

```
File "app_rvc.py", line 545, in convert
    data = response["data"][1]["name"]
KeyError: 'data'
```

换哪个声线都一样。**不是模型坏了，所有模型都跑不动。**

---

### 原因：基础模型的配置写错了

RVC 推理前要先用 hubert 提取语音特征。`assets/hubert_base/config.json`
里 `conv_kernel` 被写成了 `[10,3,3,3,3,3,3]`，而权重最后两层实际是 `2`：

```
RuntimeError: Error(s) in loading state_dict for HubertModelWithFinalProj:
	size mismatch for feature_extractor.conv_layers.5.conv.weight:
	copying a param with shape torch.Size([512, 512, 2]) from checkpoint,
	the shape in current model is torch.Size([512, 512, 3]).
```

这个 `config.json` 是安装器写出来的，所以**每一台新装的机器都会踩到**。
gradio 的推理接口出错时只回 `HTTP 500` + `{"error": null}`，真实堆栈留在
WebUI 日志里；后端又直接取 `response["data"]`，于是你在界面上只能看到
一句和真正原因毫无关系的 `KeyError`。

### 修法：三重保险

1. **安装器改对**：`setup/下载基础模型.py` 写出的 `conv_kernel` 改为
   `[10,3,3,3,3,2,2]`，与权重逐层一致；
2. **老安装自动纠正**：重跑 `一键安装.bat` 时，会读出已有 `config.json`
   并比对，不一致就就地改正（原文件留 `.bak`）；
3. **运行时自愈**：`infer/hubert.py` 加载时若再遇到 `size mismatch`，会从
   `pytorch_model.bin` 反推每层真实卷积核，改好 `config.json` 后自动重试
   一次。只处理这一类错误，显存不足、文件损坏等原样抛出。

### 同时：报错不再是天书

`RVCSVC-API/app_rvc.py` 不再把失败吞成 `KeyError`，而是从
`RVC-WebUI/logs/webui_service.log` 尾部捞出**最后一条真实异常**（含尺寸
明细），直接显示在监控台，并附三条最可能的排查方向：

- 基础模型配置不匹配 → 重跑一键安装.bat
- 显存不足 → 换短一点的歌，或关掉占显存的程序
- 声线与当前 RVC 版本不兼容 → 换个模型试

非 JSON 响应（WebUI 进程崩了）也会单独提示，并给出日志路径。

---

**已经装好的用户**：重跑一次 `一键安装.bat` 就好（只改配置，不重下模型）；
或者等程序自更新到 1.1.3。监控台看不到错误详情的话，重启一次 3333 后端。
