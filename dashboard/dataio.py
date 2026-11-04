# -*- coding: utf-8 -*-
"""数据备份与迁移：导出 / 导入（对外薄壳）。

实现已拆到 dataiokit/（core → backup → restore，单向依赖），这里只负责两件事：
让 `import dataio` 的老代码一个字都不用改；以及把**赋值**转发给真正持有者。

转发为什么必要：测试和运维会直接改这些模块级名字，例如

    dataio._write_atomic = boom      # 验「写盘失败要整体回滚」
    dataio._settings = lambda: ...   # 验「设置写不进去时要警告」

拆分后真正被调用的是 restore.py / core.py 里那一份，只改门面等于没改
（v2.0.0 拆 panelkit 时就踩过这个：只改 panel.X，跑的还是旧值）。

导出内容（按需勾选，细节见 dataiokit/backup.py 的说明）：
    data/guest_db.json           访客 / 申请 / 队列 / 白名单（必含）
    data/advanced_settings.json  高级设置
    data/covers_index.json       成品库索引
    covers/*.mp3                 成品音频（体积大，默认不含）
    models/manifest.json         已装声线清单（只记名字与大小）
    manifest.json                版本、时间、条目大小与 sha256、各项条数
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from dataiokit import (APP, AUDIO_PREFIX, BACKUP_DIR, DATA_PREFIX, KIND,  # noqa: E402,F401
                       MAX_KEEP_AUTO, MAX_KEEP_MANUAL, MAX_MEMBER, MAX_UNPACK,
                       MAX_ZIP, SAFE_ARCS, SCHEMA, apply, backup_abs, check,
                       delete_backup, ensure_dir, export, list_backups, open_dir,
                       pick_zip, summary, version, _LAST_EXPORT, _LOCK, _PICK_SRC,
                       _arc_ok, _audio_arc, _by_id, _cap, _collect, _counts, _cover,
                       _cover_counts, _covers_dir, _cur_db, _current_snapshot,
                       _db_path, _dump, _guest, _human, _index_path, _job_alive,
                       _loads, _merge_guest_db, _models_manifest, _new_path,
                       _newer, _normalize, _prune, _read_bytes, _reload,
                       _replace_guest, _restore, _safe_backup_path, _settings,
                       _settings_path, _sha, _sha_file, _tag, _write_atomic)
from dataiokit import backup as _backup_mod      # noqa: E402
from dataiokit import core as _core_mod          # noqa: E402
from dataiokit import restore as _restore_mod    # noqa: E402

_PKG = (_core_mod, _backup_mod, _restore_mod)


class _Facade(types.ModuleType):
    """把 `dataio.X = v` 同步到所有持有 X 的子模块。"""

    def __setattr__(self, name, value):
        for m in _PKG:
            if hasattr(m, name):
                setattr(m, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Facade
