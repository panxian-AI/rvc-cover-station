# -*- coding: utf-8 -*-
"""创建 GitHub Release v1.0.0 并上传一键安装包（token 只从凭据管理器读取，不落盘不回显）。"""
import json
import subprocess
import sys
import urllib.parse
import urllib.request

REPO = "panxian-AI/rvc-cover-station"
TAG = "v1.2.2"
NAME = "RVC 翻唱站 v1.2.2 · 公网开关进控制台"
ASSET = r"E:\rvc-cover-station\_release\RVC翻唱站-一键安装包-v1.2.2.zip"
ASSET_NAME = "RVC-Cover-Station-Installer-v1.2.2.zip"
NOTES = open(r"E:\rvc-cover-station\_release\ReleaseNotes.md", encoding="utf-8").read()


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
