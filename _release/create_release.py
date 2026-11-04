# -*- coding: utf-8 -*-
"""创建 GitHub Release 并上传一键安装包（token 只从凭据管理器读取，不落盘不回显）。

发布前会先跑一遍全量回归（dashboard/run_e2e.py --strict），不绿就不发。
历史上出过「宣称 19 套全绿、实际两红」的事：已装机器只看 latest，发错了没法撤回，
所以门禁写在最前面。确实需要跳过（比如只补一个资产）时显式设 RVC_SKIP_E2E=1。
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = "panxian-AI/rvc-cover-station"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG = "v2.1.2"
NAME = "RVC 翻唱站 v2.1.2 · 结构整理（功能同 v2.1.1）"
ASSET = r"E:\rvc-cover-station\_release\RVC翻唱站-一键安装包-v2.1.2.zip"
ASSET_NAME = "RVC-Cover-Station-Installer-v2.1.2.zip"
NOTES = open(r"E:\rvc-cover-station\_release\ReleaseNotes.md", encoding="utf-8").read()


def e2e_gate():
    """发布前跑全量回归。没跑通就不许往下走。

    测试要在**运行盘**的 dashboard 里跑（依赖真实盘数据与 torch），仓库副本会假红；
    代码同步由发版人负责，这里只保证「跑过、全绿」。
    """
    if os.environ.get("RVC_SKIP_E2E") == "1":
        print("!! [门禁] RVC_SKIP_E2E=1 —— 这次发布没有跑全量回归，出问题自己认")
        return

    data = os.environ.get("RVC_DATA_DIR") or r"E:\RVC-Data\RVCSVC"
    cand = [os.path.join(data, "dashboard"), os.path.join(REPO_DIR, "dashboard")]
    cwd = next((d for d in cand if os.path.isfile(os.path.join(d, "run_e2e.py"))), "")
    if not cwd:
        sys.exit("[门禁] 找不到 dashboard/run_e2e.py，没法做发布前回归。"
                 "确需跳过请设 RVC_SKIP_E2E=1")

    py = os.environ.get("RVC_PY") or os.path.join(data, "apienv", "Scripts", "python.exe")
    if not os.path.isfile(py):
        py = sys.executable

    print("[门禁] 全量回归：%s run_e2e.py --repo %s（cwd=%s）" % (py, REPO_DIR, cwd))
    rc = subprocess.run([py, os.path.join(cwd, "run_e2e.py"),
                         "--repo", REPO_DIR, "--strict", "-q"], cwd=cwd).returncode
    if rc != 0:
        sys.exit("[门禁] 回归没过（退出码 %s），不发布。修完再来，或在 dashboard 里手动跑 "
                 "run_e2e.py --repo %s 看明细" % (rc, REPO_DIR))
    print("[门禁] 回归全绿，继续发布")


e2e_gate()

if "--dry-run" in sys.argv:
    print("[dry-run] 门禁已过；没有创建 release、没有上传资产")
    sys.exit(0)


def gh_token():
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    sys.exit("没有可用的 GitHub 凭据")


TOKEN = gh_token()


def api(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "token " + TOKEN)
    req.add_header("User-Agent", "release-uploader")
    req.add_header("Accept", "application/vnd.github+json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=120)


# 已存在同名 release 则复用
rel = None
try:
    with api("https://api.github.com/repos/%s/releases/tags/%s" % (REPO, TAG)) as r:
        rel = json.load(r)
    print("release 已存在，复用 id=%s" % rel["id"])
except urllib.error.HTTPError as e:
    if e.code != 404:
        raise

if not rel:
    body = json.dumps({"tag_name": TAG, "target_commitish": "main",
                       "name": NAME, "body": NOTES, "draft": False,
                       "prerelease": False}).encode()
    with api("https://api.github.com/repos/%s/releases" % REPO, body,
             {"Content-Type": "application/json"}) as r:
        rel = json.load(r)
    print("release 创建成功 id=%s" % rel["id"])

existing = [a["name"] for a in rel.get("assets", [])]
if ASSET_NAME in existing:
    print("资产已存在，跳过上传")
else:
    data = open(ASSET, "rb").read()
    q = urllib.parse.urlencode({"name": ASSET_NAME})
    up = ("https://uploads.github.com/repos/%s/releases/%s/assets?%s"
          % (REPO, rel["id"], q))
    req = urllib.request.Request(up, data=data, method="POST")
    req.add_header("Authorization", "token " + TOKEN)
    req.add_header("Content-Type", "application/zip")
    req.add_header("User-Agent", "release-uploader")
    with urllib.request.urlopen(req, timeout=300) as r:
        asset = json.load(r)
    print("资产上传成功: %s (%d bytes, %d downloads)"
          % (asset["name"], asset["size"], asset["download_count"]))

print("RELEASE_URL=" + rel["html_url"])
