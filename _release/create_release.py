# -*- coding: utf-8 -*-
"""创建 GitHub Release 并上传一键安装包（token 只从凭据管理器读取，不落盘不回显）。

两步门禁都在最前面，因为「已装机器只看 latest」，发错了撤不回来：

  1. 发布前跑一遍全量回归（dashboard/run_e2e.py --strict），不绿就不发。
     历史上出过「宣称 19 套全绿、实际两红」的事。确实要跳过时设 RVC_SKIP_E2E=1。
  2. 上传完资产后，把**在线更新真正下载的那个包**（tag 源码 zip）的 SHA-256
     算出来写进 Release 正文。客户端拿它逐字节核对，对不上就拒装。
     正文里的值来自 api.github.com（HTTPS），加速镜像改不了；没有它，
     更新器的 7 个第三方加速前缀就等于可信源。
     写完还会再下一次、再算一次，确认 codeload 给的字节是稳定的。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = "panxian-AI/rvc-cover-station"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG = "v2.1.3"
NAME = "RVC 翻唱站 v2.1.3 · 更新包加校验（功能同 v2.1.2）"
ASSET = r"E:\rvc-cover-station\_release\RVC翻唱站-一键安装包-v2.1.3.zip"
ASSET_NAME = "RVC-Cover-Station-Installer-v2.1.3.zip"
NOTES = open(r"E:\rvc-cover-station\_release\ReleaseNotes.md", encoding="utf-8").read()

CK_MARK = "<!-- rvc-checksums -->"
SHA_IN_BODY = re.compile(
    r"(?:update[ _-]?zip[ _-]?sha-?256|更新包\s*sha-?256)[^0-9a-fA-F]{0,24}([0-9a-fA-F]{64})",
    re.I)


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

def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _fetch(url, tries=5, timeout=180) -> bytes:
    """带重试地下载。tag 刚推上去时 codeload 可能还没生成好。"""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "release-uploader"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:      # noqa: BLE001 - 逐次重试，最后一次才报
            last = e
            time.sleep(2 + 2 * i)
    raise SystemExit("下载失败：%s（%s）" % (url, last))


def _src_url(tag: str) -> str:
    return "https://codeload.github.com/%s/zip/refs/tags/%s" % (REPO, tag)


def _section(installer_sha: str, src_sha: str) -> str:
    return """
{CK_MARK}

---

### 安装包校验（发布时自动写入，请勿手改）

**一键安装包** `{an}`
SHA-256：`{ish}`

**在线更新用的 tag 源码包**（控制台点「立即更新」下载的就是这个）
update_zip_sha256: `{ssh}`

控制台在把文件写进磁盘之前会逐字节核对上面那行；对不上就换线路，所有线路都对不上
就拒绝安装 —— 因为下载可以走第三方加速镜像，只有这个值来自 GitHub 接口，镜像改不了。
手动下载安装包时也可以自己核一遍：`certutil -hashfile {an} SHA256`
""".format(CK_MARK=CK_MARK, an=ASSET_NAME, ish=installer_sha, ssh=src_sha)


# ---- 1) 资产完整性：GitHub 回的 digest 要和本地文件一致 ----
local_asset = open(ASSET, "rb").read()
installer_sha = _sha(local_asset)
if ASSET_NAME in existing:
    with api("https://api.github.com/repos/%s/releases/assets/%s"
             % (REPO, next(a["id"] for a in rel["assets"] if a["name"] == ASSET_NAME))) as r:
        asset = json.load(r)
    if asset.get("size") and asset["size"] != len(local_asset):
        sys.exit("远端资产大小 %d ≠ 本地 %d，先查清楚再发"
                 % (asset["size"], len(local_asset)))
digest = asset.get("digest") or ""
if digest.startswith("sha256:"):
    if digest.split(":", 1)[1].lower() != installer_sha:
        sys.exit("远端资产的 digest 与本地不一致：%s vs %s" % (digest, installer_sha))
    print("资产 digest 与本地一致")
else:
    print("（GitHub 没返回 asset digest，跳过该项，靠下面的源码包校验）")

# ---- 2) 在线更新包：算两次，确认 codeload 给的字节稳定 ----
print("下载 tag 源码包算校验值：%s" % _src_url(TAG))
z1 = _fetch(_src_url(TAG))
if len(z1) < 1024 or z1[:2] != b"PK":
    sys.exit("tag 源码包不像 zip（%d 字节），先确认 tag 已推送" % len(z1))
src_sha = _sha(z1)
src_sha2 = _sha(_fetch(_src_url(TAG)))
if src_sha != src_sha2:
    sys.exit("两次下载的字节不一致（%s… vs %s…）：codeload 对这个 tag 不稳定，"
             "写进正文会让所有已装机器拒装，先别发" % (src_sha[:12], src_sha2[:12]))
print("源码包 %d 字节，SHA-256 = %s（两次一致）" % (len(z1), src_sha))

# ---- 3) 写进正文（重跑时替换旧块，不叠加） ----
body = rel.get("body") or NOTES
body = body.split(CK_MARK)[0].rstrip() + _section(installer_sha, src_sha)
with api("https://api.github.com/repos/%s/releases/%s" % (REPO, rel["id"]),
         json.dumps({"body": body}).encode(), {"Content-Type": "application/json"},
         method="PATCH") as r:
    rel = json.load(r)
print("校验值已写入 Release 正文")

# ---- 4) 回读确认客户端真能取到 ----
with api("https://api.github.com/repos/%s/releases/tags/%s" % (REPO, TAG)) as r:
    back = json.load(r)
m = SHA_IN_BODY.search(back.get("body") or "")
got = (m.group(1).lower() if m else "")
if got != src_sha:
    sys.exit("回读不一致：正文里是 %r，应为 %s" % (got, src_sha))
print("回读校验：正文里的 update_zip_sha256 = %s…（与源码包一致）" % got[:12])

print("RELEASE_URL=" + rel["html_url"])
