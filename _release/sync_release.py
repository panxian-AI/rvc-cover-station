# -*- coding: utf-8 -*-
"""把已存在的 Release 的说明和资产同步成最新的（tag 已重指后跑一次）。

凭据只从 Windows 凭据管理器读，不落盘、不回显。
"""
import json
import subprocess
import sys
import urllib.parse
import urllib.request

REPO = "panxian-AI/rvc-cover-station"
TAG = "v2.2.0"
NAME = "RVC 翻唱站 v2.2.0 · 音源支持网易云 / QQ音乐 / 酷狗，自己选"
ASSET = r"E:\rvc-cover-station\_release\RVC翻唱站-一键安装包-v2.2.0.zip"
ASSET_NAME = "RVC-Cover-Station-Installer-v2.2.0.zip"
NOTES = open(r"E:\rvc-cover-station\_release\ReleaseNotes.md", encoding="utf-8").read()


def gh_token():
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    sys.exit("没有可用的 GitHub 凭据")


TOKEN = gh_token()


def call(url, data=None, method=None, ctype="application/json"):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "token " + TOKEN)
    req.add_header("User-Agent", "release-uploader")
    req.add_header("Accept", "application/vnd.github+json")
    if ctype:
        req.add_header("Content-Type", ctype)
    return urllib.request.urlopen(req, timeout=300)


with call("https://api.github.com/repos/%s/releases/tags/%s" % (REPO, TAG)) as r:
    rel = json.load(r)
print("release id=%s target=%s" % (rel["id"], rel.get("target_commitish")))

body = json.dumps({"name": NAME, "body": NOTES}).encode()
with call("https://api.github.com/repos/%s/releases/%s" % (REPO, rel["id"]), body, "PATCH") as r:
    rel = json.load(r)
print("说明已更新，字数", len(rel.get("body") or ""))

for a in rel.get("assets", []):
    if a["name"] == ASSET_NAME:
        with call("https://api.github.com/repos/%s/releases/assets/%s" % (REPO, a["id"]),
                  method="DELETE") as r:
            pass
        print("旧资产已删除 id=%s" % a["id"])

data = open(ASSET, "rb").read()
q = urllib.parse.urlencode({"name": ASSET_NAME})
with call("https://uploads.github.com/repos/%s/releases/%s/assets?%s" % (REPO, rel["id"], q),
          data, "POST", "application/zip") as r:
    asset = json.load(r)
print("资产上传成功: %s (%d bytes)" % (asset["name"], asset["size"]))
print("RELEASE_URL=" + rel["html_url"])
