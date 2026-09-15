# -*- coding: utf-8 -*-
# 本文件改编自 GPL-3.0 项目 QQFavoriteExtract：
#   https://github.com/VanillaNahida/QQFavoriteExtract
#   （作者：香草味的纳西妲，nahida1027@126.com）
# 原始文件：main_gui.py（v1.4.3）
# 改动：移除 PyQt5 UI 依赖与 sys.exit/QMessageBox，进度/日志改为回调接口；
#       修复 read_file_with_correct_encoding 中 is_content_valid 的字符范围判断 bug；
#       扩展名修正改为纯魔数检测（兼容无扩展名文件）。
# 本文件按 GNU GPL v3 协议分发，完整协议见 https://www.gnu.org/licenses/gpl-3.0.txt

"""QQNT 本地收藏表情提取（可复用模块）

从 PC 版 QQ（QQNT）本地缓存提取收藏表情：定位用户数据目录 -> 复制表情文件 ->
按文件头魔数修正扩展名。进度/日志通过回调接口输出，不依赖任何 UI 框架；
复制逐文件容错，输出目录需显式 overwrite 才允许写入已存在的目录。
"""

import configparser
import os
import shutil
import threading
from copy import deepcopy

from ohmymeme.core.imports import ImportPath

# QQNT 用户数据配置文件（默认路径）
DEFAULT_INI_PATH = r"C:\Users\Public\Documents\Tencent\QQ\UserDataInfo.ini"

# 文件头魔数 -> 扩展名
FILE_SIGNATURES = {
    "jpg": (b"\xff\xd8\xff", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1"),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "gif": (b"GIF87a", b"GIF89a"),
    "bmp": (b"BM",),
    "tiff": (b"II*\x00", b"MM\x00*"),
    "webp": (b"RIFF",),
    "ico": (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"),
    "psd": (b"8BPS",),
    "svg": (b"<?xml", b"<svg"),
    "heic": (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftyphevx"),
    "avif": (b"ftypavif", b"ftypavis"),
}

# 编码尝试顺序（UTF-8 优先；GB18030 覆盖 GBK）
_PRIORITY_ENCODINGS = [
    "utf-8-sig",
    "utf-8",
    "utf-16",
    "gb18030",
    "gbk",
    "big5",
    "latin-1",
    "ascii",
]


def is_content_valid(content, min_chinese=1):
    """内容是否包含至少一个中文字符（避免把乱码当有效解码）"""
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in content)
    return chinese >= min_chinese


def read_file_with_correct_encoding(file_path, target_string="[UserDataSet]"):
    """按候选编码逐一严格解码，返回首个命中目标字符串的有效编码，失败返回 None"""
    try:
        with open(file_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    for enc in _PRIORITY_ENCODINGS:
        try:
            content = data.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
        if target_string not in content:
            continue
        if is_content_valid(content) or all(ord(char) < 128 for char in content):
            return enc
    return None


def get_userdata_save_path(ini_path=DEFAULT_INI_PATH):
    """从配置文件读取 UserDataSavePath，失败返回 None"""
    enc = read_file_with_correct_encoding(ini_path)
    if not enc:
        return None
    config = configparser.ConfigParser()
    try:
        config.read(ini_path, encoding=enc)
        return config.get("UserDataSet", "UserDataSavePath", fallback=None)
    except (configparser.Error, OSError):
        return None


def get_numeric_subdirectories(parent_dir):
    """返回目录下纯数字命名的子目录列表"""
    try:
        return [
            name
            for name in os.listdir(parent_dir)
            if os.path.isdir(os.path.join(parent_dir, name)) and name.isdigit()
        ]
    except OSError:
        return []


def get_emoji_dir(userdata_save_path, qq_number):
    """返回指定 QQ 号的收藏表情缓存目录"""
    return os.path.join(
        userdata_save_path,
        str(qq_number),
        "nt_qq",
        "nt_data",
        "Emoji",
        "personal_emoji",
        "Ori",
    )


def get_available_qq_numbers(userdata_save_path=None, ini_path=DEFAULT_INI_PATH):
    """返回存在表情缓存目录的可用 QQ 号列表"""
    if userdata_save_path is None:
        userdata_save_path = get_userdata_save_path(ini_path)
    if not userdata_save_path:
        return []
    return [
        name
        for name in get_numeric_subdirectories(userdata_save_path)
        if os.path.isdir(get_emoji_dir(userdata_save_path, name))
    ]


def get_extract_status(
    ini_path=DEFAULT_INI_PATH, userdata_save_path=None, nickname_lookup=None
):
    """探测提取环境，返回 {ok,error,message,userdata_save_path,accounts}"""
    if userdata_save_path is None:
        userdata_save_path = get_userdata_save_path(ini_path)
    if not userdata_save_path:
        return {
            "ok": False,
            "error": "config",
            "message": "无法读取配置文件或缺少 UserDataSavePath: " + ini_path,
            "userdata_save_path": "",
            "accounts": [],
        }
    if not os.path.isdir(userdata_save_path):
        return {
            "ok": False,
            "error": "path_missing",
            "message": "用户数据目录不存在: " + userdata_save_path,
            "userdata_save_path": userdata_save_path,
            "accounts": [],
        }
    accounts = []
    for qq in get_numeric_subdirectories(userdata_save_path):
        emoji_dir = get_emoji_dir(userdata_save_path, qq)
        if not os.path.isdir(emoji_dir):
            continue
        count = sum(
            1
            for f in os.listdir(emoji_dir)
            if os.path.isfile(os.path.join(emoji_dir, f))
        )
        accounts.append(
            {
                "qq": qq,
                "nickname": nickname_lookup(qq) if nickname_lookup else "",
                "count": count,
            }
        )
    return {
        "ok": True,
        "error": "",
        "message": "",
        "userdata_save_path": userdata_save_path,
        "accounts": accounts,
    }


def sanitize_filename(name):
    """移除 Windows 文件名非法字符"""
    for char in '<>:"/\\|?*':
        name = name.replace(char, "")
    return name.strip()


def copy_directory_with_progress(
    src,
    dst,
    image_only=False,
    should_stop=None,
    on_progress=None,
    on_error=None,
    on_log=None,
):
    """递归复制目录，逐文件容错（失败跳过继续）；返回 {total,copied,failed,skipped}"""
    if not os.path.isdir(src):
        raise FileNotFoundError("源目录不存在: " + src)
    os.makedirs(dst, exist_ok=True)
    todo = []
    skipped = 0
    for root, dirs, fnames in os.walk(src):
        for name in fnames:
            path = os.path.join(root, name)
            if image_only and not get_actual_extension(path):
                skipped += 1
                continue
            todo.append((root, name))
    total = len(todo)
    done = failed = 0
    for root, name in todo:
        if should_stop and should_stop():
            if on_log:
                on_log("复制已取消")
            break
        rel = os.path.relpath(root, src)
        dest_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(dest_dir, exist_ok=True)
        src_file = os.path.join(root, name)
        dest_file = os.path.join(dest_dir, name)
        try:
            shutil.copy2(src_file, dest_file)
        except OSError as e:
            failed += 1
            try:
                os.unlink(dest_file)
            except OSError:
                pass
            if on_error:
                on_error(src_file, str(e))
            continue
        done += 1
        if on_progress:
            on_progress(done, total, src_file, dest_file)
    if on_log:
        on_log("复制完成：成功 %d，失败 %d，跳过 %d" % (done, failed, skipped))
    return {"total": total, "copied": done, "failed": failed, "skipped": skipped}


def get_actual_extension(file_path):
    """按文件头魔数判断真实扩展名，未知返回 None"""
    try:
        with open(file_path, "rb") as f:
            header = f.read(16)
    except OSError:
        return None
    for ext, sigs in FILE_SIGNATURES.items():
        for sig in sigs:
            if not header.startswith(sig):
                continue
            if ext == "webp" and header[8:12] != b"WEBP":
                continue
            return ext
    return None


def correct_file_extension(file_path, on_log=None):
    """按魔数修正单个文件扩展名（兼容无扩展名文件），重命名成功返回 True"""
    actual_ext = get_actual_extension(file_path)
    if not actual_ext:
        return False
    base, cur_ext = os.path.splitext(file_path)
    if cur_ext and cur_ext.lower() == "." + actual_ext:
        return False
    new_path = base + "." + actual_ext
    if os.path.exists(new_path):
        return False
    try:
        os.rename(file_path, new_path)
    except OSError as e:
        if on_log:
            on_log("重命名失败: %s" % e)
        return False
    if on_log:
        on_log(
            "重命名: %s -> %s"
            % (os.path.basename(file_path), os.path.basename(new_path))
        )
    return True


def batch_correct_extensions(directory, on_log=None):
    """递归修正目录内所有文件的扩展名，返回 {total,renamed,unrecognized}"""
    total = renamed = unrecognized = 0
    for root, dirs, files in os.walk(directory):
        for name in files:
            path = os.path.join(root, name)
            total += 1
            if not get_actual_extension(path):
                unrecognized += 1
                continue
            if correct_file_extension(path, on_log):
                renamed += 1
    return {"total": total, "renamed": renamed, "unrecognized": unrecognized}


def _clear_dir(path):
    """清空目录内容（保留目录本身）"""
    for entry in os.scandir(path):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def extract_qq_emojis(
    qq_number,
    output_dir,
    userdata_save_path=None,
    ini_path=DEFAULT_INI_PATH,
    image_only=False,
    overwrite=False,
    should_stop=None,
    on_progress=None,
    on_error=None,
    on_log=None,
):
    """提取指定 QQ 号的收藏表情到 output_dir，返回统计 dict；失败抛异常"""
    if userdata_save_path is None:
        userdata_save_path = get_userdata_save_path(ini_path)
    if not userdata_save_path:
        raise RuntimeError("无法从配置文件获取用户数据保存路径: " + ini_path)
    emoji_dir = get_emoji_dir(userdata_save_path, qq_number)
    if not os.path.isdir(emoji_dir):
        raise FileNotFoundError("未找到该账号的表情缓存目录: " + emoji_dir)
    if os.path.abspath(output_dir) == os.path.abspath(emoji_dir):
        raise ValueError("输出目录不能与源表情目录相同")
    if os.path.exists(output_dir) and not overwrite:
        try:
            non_empty = bool(os.listdir(output_dir))
        except OSError:
            non_empty = True
        if non_empty:
            raise FileExistsError("输出目录已存在且非空: " + output_dir)
    elif overwrite and os.path.isdir(output_dir):
        _clear_dir(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    if on_log:
        on_log("复制表情文件到: " + output_dir)
    copied = copy_directory_with_progress(
        emoji_dir,
        output_dir,
        image_only,
        should_stop,
        on_progress,
        on_error,
        on_log,
    )
    ext_res = batch_correct_extensions(output_dir, on_log)
    result = {
        "output_dir": output_dir,
        "total": copied["total"],
        "copied": copied["copied"],
        "failed": copied["failed"],
        "skipped": copied["skipped"],
        "renamed": ext_res["renamed"],
        "unrecognized": ext_res["unrecognized"],
    }
    if on_log:
        on_log(
            "完成：复制 %d/%d，失败 %d，修正扩展名 %d，未识别 %d"
            % (
                copied["copied"],
                copied["total"],
                copied["failed"],
                ext_res["renamed"],
                ext_res["unrecognized"],
            )
        )
    return result


_QQNT_STATE = {
    "status": "idle",
    "progress": 0,
    "message": "",
    "error": "",
    "log": [],
    "result": None,
}


class QQNTProvider:
    provider_id = "source.qqnt"
    api_version = 1

    def __init__(self):
        # Each factory owns its own state and cancellation flag.
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._running = False
        self._state = deepcopy(_QQNT_STATE)

    def start(self, context):
        # Accept only source choices, never a host output or cache path.
        allowed = {
            "qq_number",
            "userdata_save_path",
            "ini_path",
            "image_only",
            "submit",
        }
        unknown = set(context.request or {}) - allowed
        if unknown:
            raise ValueError("request: " + ", ".join(sorted(unknown)))
        with self._lock:
            if self._running:
                return False
            self._cancel.clear()
            self._running = True
            self._state = deepcopy(_QQNT_STATE)
            self._state.update(status="running", message="准备中")
        return True

    def stop(self):
        # Cancellation never clears a still-running worker's state.
        self._cancel.set()

    def finish(self):
        # Host calls this after drain, including rejected coordinator starts.
        with self._lock:
            self._running = False

    def get_progress(self):
        # The bridge receives detached progress including its bounded log.
        with self._lock:
            return deepcopy(self._state)

    def inspect(
        self, ini_path=DEFAULT_INI_PATH, userdata_save_path=None, nickname_lookup=None
    ):
        # Host supplies the nickname lookup, without its cache path.
        return get_extract_status(ini_path, userdata_save_path, nickname_lookup)

    def import_media(self, context):
        # Extract only into this operation's staging, then submit through its sink.
        operation = context.operation
        request = context.request or {}

        def cancelled():
            return self._cancel.is_set() or context.is_cancelled()

        def update(**changes):
            with self._lock:
                self._state.update(operation.serialize_progress(changes))
                context.progress(deepcopy(self._state))

        def log(message):
            update(
                log=(self.get_progress()["log"] + [operation.serialize_log(message)])[
                    -100:
                ]
            )

        try:
            operation.require("filesystem.read")
            staging = operation.temporary.path("qqnt")
            result = extract_qq_emojis(
                request["qq_number"],
                str(staging),
                userdata_save_path=request.get("userdata_save_path"),
                ini_path=request.get("ini_path") or DEFAULT_INI_PATH,
                image_only=request.get("image_only", False),
                overwrite=True,
                should_stop=cancelled,
                on_progress=lambda done, total, src, dst: update(
                    progress=int(done * 100 / total) if total else 0,
                    message="复制中 %d/%d" % (done, total),
                ),
                on_error=lambda src, message: log("失败: %s (%s)" % (src, message)),
                on_log=log,
            )
            if not cancelled() and request.get("submit", True):
                operation.require("import.submit")
                imported = context.sink.import_batch(
                    tuple(
                        ImportPath(path, path.stem)
                        for path in sorted(staging.rglob("*"))
                        if path.is_file()
                    ),
                    cancelled=cancelled,
                )
                result["copied"] = len(imported.imported_ids)
                result["skipped"] += imported.rejected
            if cancelled():
                update(status="cancelled", message="已取消", result=result)
            else:
                update(status="done", progress=100, message="提取完成", result=result)
            return result
        except Exception as error:
            update(status="error", message="提取失败", error=str(error))
            return None
        finally:
            with self._lock:
                self._running = False


def create_plugin():
    # Entry-point factory has no host argument or global provider instance.
    return QQNTProvider()
