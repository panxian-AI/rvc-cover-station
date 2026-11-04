# -*- coding: utf-8 -*-
"""给 CI（以及任何干净检出）造一份最小可用的运行夹具。

为什么需要它
------------
回归里有 8 套断言要看「硬盘上真有什么」：声线 pth、成品 mp3、面板解释器。
本机跑之所以全绿，是因为 `E:\\RVC-Data` 里本来就有用户自己的声线和成品；
把仓库单独检出到 CI 上，这些断言全变成红 —— 但它们查的不是代码坏了，
而是「这台机器还没装东西」。

所以 CI 先跑这个脚本，按 paths.py 推导出的真实布局摆一份**最小**夹具：

    <DATA>\\RVC-WebUI\\assets\\weights\\ci_smoke.pth   一个结构合法的声线
    <DATA>\\RVCSVC\\covers\\ci_smoke.mp3 + index.json  一条成品
    <DATA>\\runtime\\python312\\python[w].exe          面板解释器（复制当前解释器）

安全闸：发现 covers/index.json 或 weights 目录里已经有东西，说明这是**真的
安装目录**（用户自己的数据），直接退出、不写任何文件。所以它只在仓库检出
（CI / 新克隆）里干活，不会污染用户盘。

用法：
    python ci_fixture.py          # 造夹具（已有数据则拒绝）
    python ci_fixture.py --check  # 只检查、不写
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths  # noqa: E402

MP3_BYTES = 48 * 1024          # 要 > 1024（_alive_file 的门槛）
VOICE = "ci_smoke.pth"
COVER = "ci_smoke.mp3"


def _say(msg):
    print("[fixture] " + msg)


def _real_install():
    """这个目录里已经有用户数据吗？有就一个字都不许写。"""
    idx = os.path.join(paths.COVERS_DIR, "index.json")
    if os.path.isfile(idx):
        return "已有成品索引 %s" % idx
    wdir = os.path.join(paths.RVC_ROOT, "assets", "weights")
    if os.path.isdir(wdir):
        for f in os.listdir(wdir):
            if f.lower().endswith(".pth"):
                return "已有声线 %s" % os.path.join(wdir, f)
    return ""


def link_webui():
    """真实安装里 WebUI 在 <DATA>\\RVC-WebUI，和 RVCSVC 平级；仓库里它在
    RVCSVC 内部。e2e_perf 要读 <DATA>\\RVC-WebUI\\infer\\vc\\pipeline.py，
    所以在这儿接一个目录联接（junction）——它不复制文件、也不需要管理员权限。
    """
    dst, src = paths.RVC_ROOT, os.path.join(paths.PROJ, "RVC-WebUI")
    if os.path.isdir(dst):
        _say("WebUI 已就位：%s" % dst)
        return dst
    if not os.path.isdir(src):
        _say("警告：%s 不存在，e2e_perf 的 WebUI 断言会失败" % src)
        return ""
    if not _link_dir(src, dst):
        return ""
    _say("接上 WebUI：%s -> %s" % (dst, src))
    return dst


def make_voice(force):
    import torch
    wdir = os.path.join(paths.RVC_ROOT, "assets", "weights")
    os.makedirs(wdir, exist_ok=True)
    p = os.path.join(wdir, VOICE)
    if os.path.isfile(p) and not force:
        _say("声线已存在，跳过：" + p)
        return p
    big = torch.zeros(300000)      # >1MB：过「太小不像模型」的闸
    d = {"weight": {"enc_p.emb_phone.weight": torch.zeros(4, 4),
                    "enc_p.emb_phone.bias": torch.zeros(4),
                    "pad": big},
         "sr": "40k", "f0": 1, "version": "v2", "info": "30epoch"}
    torch.save(d, p)
    _say("造了声线：%s（%.1f MB）" % (p, os.path.getsize(p) / 1048576.0))
    return p


def make_cover(force):
    os.makedirs(paths.COVERS_DIR, exist_ok=True)
    idx_path = os.path.join(paths.COVERS_DIR, "index.json")
    mp3 = os.path.join(paths.COVERS_DIR, COVER)
    if not os.path.isfile(mp3) or force:
        with open(mp3, "wb") as f:
            f.write(b"\xff\xfb\x90\x00" + b"\x00" * MP3_BYTES)
        _say("造了成品：%s（%d B）" % (mp3, os.path.getsize(mp3)))
    if os.path.isfile(idx_path) and not force:
        _say("成品索引已存在，跳过：" + idx_path)
        return idx_path
    ent = [{"name": COVER, "title": "CI 冒烟曲", "artist": "CI",
            "model": VOICE, "key_shift": 0, "acapella": False,
            "song_id": "1000001", "created": 1700000000.0,
            "size": os.path.getsize(mp3)}]
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(ent, f, ensure_ascii=False, indent=1)
    _say("写了成品索引：%s" % idx_path)
    return idx_path


def _link_dir(src, dst):
    """把 src 接到 dst（目录联接）。返回是否成功。

    不用复制：解释器目录里有 python312.dll / Lib，只拷 exe 会「文件在但跑不起来」。
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=True)
        return True
    except OSError:
        pass
    import subprocess
    r = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return True
    # 联接只能同卷（CI 的解释器在 C:、检出在 D:），symlink 又要特权，
    # 两条都走不通时老老实实复制一份 —— 慢几秒，但一定跑得起来。
    _say("接不上（%s），改为复制 %s" % ((r.stderr or r.stdout).strip(), src))
    try:
        import shutil
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return True
    except Exception as e:  # noqa: BLE001
        _say("警告：复制 %s 失败：%r" % (src, e))
        return False


def link_runtime():
    """面板解释器：launch_silent.stack_dirs() 只认 <DATA>\\runtime\\python312。

    真安装里那是个完整解释器；仓库检出里没有，所以把当前解释器所在目录接过去
    （CI 上是 setup-python 的完整安装）。只复制 exe 不行 —— 缺 python312.dll
    与 Lib，python_ok() 会判 False。
    """
    dstdir = os.path.join(paths.DATA, "runtime", "python312")
    if os.path.isfile(os.path.join(dstdir, "python.exe")):
        _say("面板解释器已就位：%s" % dstdir)
        return dstdir
    src = getattr(sys, "base_prefix", "") or os.path.dirname(sys.executable)
    if not os.path.isfile(os.path.join(src, "python.exe")):
        src = os.path.dirname(sys.executable)
    if not _link_dir(src, dstdir):
        return ""
    _say("接上面板解释器：%s -> %s" % (dstdir, src))
    return dstdir


def make_ncm_cookie():
    """e2e_advanced 会断言「本机已配置 → cookie True」。

    这条查的是 ncm_preflight() 的判定（文件里有 MUSIC_U=），不拿 cookie 发请求。
    新检出里没有这个文件，所以放一个明显不是真凭据的占位串。
    """
    p = os.path.join(paths.PROJ, "ncmapi", "ncm_cookie.txt")
    if os.path.isfile(p):
        _say("ncm cookie 已存在，跳过：" + p)
        return p
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("MUSIC_U=ci-fixture-not-a-real-cookie\n")
    _say("放了占位 ncm cookie：%s（不是真凭据）" % p)
    return p


def main(argv):
    check_only = "--check" in argv
    force = "--force" in argv

    _say("STACK  = %s" % paths.STACK)
    _say("DATA   = %s" % paths.DATA)
    _say("PROJ   = %s" % paths.PROJ)
    _say("RVC_ROOT = %s" % paths.RVC_ROOT)

    why = _real_install()
    if why:
        _say("这是真实安装目录（%s），不造夹具。" % why)
        _say("夹具只用于干净检出；本机回归请直接用真实数据。")
        return 0
    if check_only:
        _say("干净检出，可以造夹具。")
        return 0

    try:
        link_runtime()
        link_webui()
        make_ncm_cookie()
        make_voice(force)
        make_cover(force)
    except Exception as e:  # noqa: BLE001 - 造不出来要让 CI 立刻看见
        _say("失败：%r" % (e,))
        return 1
    _say("就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
