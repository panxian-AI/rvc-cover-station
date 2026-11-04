# -*- coding: utf-8 -*-
"""数据备份与迁移（对外的唯一入口）。

v2.1.2 起从单文件 dataio.py（1246 行）拆开，分三层，依赖严格单向：

    core.py     常量、路径适配、锁、sha/体积/条数之类的小工具
    backup.py   打包导出、备份列表/删除/裁剪、选文件、打开目录
    restore.py  只读预检、合并、规整、落盘与失败回滚

    core <- backup <- restore

为什么路径写成函数：guest / settings / cover 可能还没加载，或者被测试换成
临时目录；写成常量会把「导入那一刻的路径」焊死，换机器就错位。

对外仍然是一个模块：`import dataio` 拿到的东西和拆分前一模一样
（dataio.py 只是这层的薄壳，并把赋值转发给真正的持有者）。
"""
from .backup import (backup_abs, delete_backup, export, list_backups, open_dir,
                     pick_zip, summary, _PICK_SRC, _audio_arc, _collect, _prune)
from .core import (APP, AUDIO_PREFIX, BACKUP_DIR, DATA_PREFIX, HERE, KIND,
                   MAX_KEEP_AUTO, MAX_KEEP_MANUAL, MAX_MEMBER, MAX_UNPACK, MAX_ZIP,
                   SAFE_ARCS, SCHEMA, ensure_dir, version, _LAST_EXPORT, _LOCK, _cap,
                   _counts, _cover, _cover_counts, _covers_dir, _cur_db, _db_path,
                   _dump, _guest, _human, _index_path, _loads, _models_manifest,
                   _new_path, _read_bytes, _replace_guest, _safe_backup_path,
                   _settings, _settings_path, _sha, _sha_file, _tag)
from .restore import (apply, check, _arc_ok, _by_id, _current_snapshot, _job_alive,
                      _merge_guest_db, _newer, _normalize, _reload, _restore,
                      _write_atomic)

__all__ = [
    "APP", "KIND", "SCHEMA", "BACKUP_DIR", "MAX_ZIP", "MAX_MEMBER", "MAX_UNPACK",
    "MAX_KEEP_AUTO", "MAX_KEEP_MANUAL", "AUDIO_PREFIX", "DATA_PREFIX", "SAFE_ARCS",
    "version", "ensure_dir", "list_backups", "delete_backup", "backup_abs",
    "summary", "export", "open_dir", "pick_zip", "check", "apply",
]
