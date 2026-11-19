# -*- coding: utf-8 -*-
"""system 镜像编辑：精简预装应用、ADB 属性、init 注入、APK 预置。

所有改动都就地写回 ext4 镜像（extents + 线性目录 + gdt_csum），
逻辑取自 work/make_system.py，已在真机刷写验证。
"""
import hashlib
import os
import re
import shutil

from .ext4fs import S_IFMT, S_IFREG, Ext4

ADB_PROPS = [
    ("persist.sys.usb.config", "adb"),
    ("ro.adb.secure", "0"),
    ("persist.service.adb.enable", "1"),
]

INIT_PATH = "/etc/init.bigfish.sh"
INIT_MARK = b"# --- custom firmware: adb props + root shell over LAN ---"

# 复用应用商店的 APK 槽位（该槽位在原厂 sparse 里是 RAW 块，刷机不会被清零）
PRESET_SLOT_DIR = "/app/com.changhong.appstore"
PRESET_PRIVAPP_DIR = "/priv-app"


def copy_image(src, dst, log=print):
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    if os.path.abspath(src) == os.path.abspath(dst):
        raise ValueError("输入与输出镜像不能是同一个文件")
    log("复制镜像: %s -> %s" % (src, dst))
    shutil.copyfile(src, dst)
    return dst


def raw_ranges(layout):
    """原厂 sparse 中用 RAW 块存储的物理块区间，作为安全分配白名单。"""
    return [(c["pos"], c["blocks"]) for c in layout["chunks"] if c["type"] == 0xCAC1]


def open_image(path, readonly=False, layout=None, log=print):
    ranges = None
    if layout is not None and not readonly:
        ranges = raw_ranges(layout)
        log("安全分配白名单: %d 段 RAW 区间（共 %d 块）" %
            (len(ranges), sum(n for _s, n in ranges)))
    return Ext4(path, readonly=readonly, allowed_ranges=ranges)


def _set_prop(txt, key, value):
    pat = re.compile(r"^%s=.*$" % re.escape(key), re.M)
    line = "%s=%s" % (key, value)
    if pat.search(txt):
        return pat.sub(lambda _m: line, txt)
    if not txt.endswith("\n"):
        txt += "\n"
    return txt + line + "\n"


def debloat(e, names, log=print, release=False):
    """把 /app/<应用> 下的 *.apk 改名为 *.apx 并把文件数据清零。

    release=True 时进一步把 .apx 截断为 0 字节并释放其块。释放的块仍位于原厂
    sparse 的 RAW 区间内，因此可以被 /priv-app 预置安全复用（刷机不会被清零）。
    返回 (处理数量, 清零字节数, 释放字节数)。
    """
    total_files = 0
    total_bytes = 0
    released = 0
    for name in names:
        name = name.strip()
        if not name or name.startswith("#"):
            continue
        path = name if name.startswith("/") else "/app/" + name
        try:
            ino = e.lookup(path)
        except FileNotFoundError:
            log("  跳过（不存在）: %s" % path)
            continue
        d = e.read_inode(ino)
        if not e.is_dir(d):
            base = os.path.basename(path)
            if not base.lower().endswith(".apk"):
                log("  跳过（不是 APK）: %s" % path)
                continue
            targets = [(os.path.dirname(path) or "/", base)]
        else:
            targets = [(path, n) for _i, n in e.listdir(path) if n.lower().endswith(".apk")]
            if not targets:
                log("  跳过（目录内无 .apk）: %s" % path)
                continue
        for dir_path, fname in targets:
            newname = fname[:-4] + ".apx"
            f_ino = e.lookup(dir_path + "/" + fname)
            fd = e.read_inode(f_ino)
            size = fd["size"] | (fd["size_high"] << 32)
            e.rename_entry(dir_path, fname, newname)
            e.overwrite_file(dir_path + "/" + newname, b"\x00" * size)
            if release:
                released += e.truncate_file(dir_path + "/" + newname)
            total_files += 1
            total_bytes += size
            log("  %-42s -> %-42s %10d B%s" %
                (fname, newname, size, "（已释放块）" if release else ""))
    log("精简完成: %d 个 APK，清零 %.1f MB%s" %
        (total_files, total_bytes / 1048576.0,
         "，释放 %.1f MB 空间" % (released / 1048576.0) if release else ""))
    return total_files, total_bytes, released


def scan_apps(e, log=None, with_icons=True, icon_cache=None):
    """扫描镜像 /app 下的应用目录，供界面勾选精简。

    返回列表，每项: dict(name, path, apks=[(文件名, 字节数)], size, icon)
    icon 是从最大的 APK 中提取的 PNG 字节（失败为 None）。
    icon_cache 用于跨次扫描复用，键为 (应用名, APK 文件名, 字节数)。
    """
    from .apkicon import extract_icon

    log = log or (lambda m: None)
    try:
        entries = e.listdir("/app")
    except FileNotFoundError:
        log("/app 不存在")
        return []

    apps = []
    for ino, name in sorted(entries, key=lambda x: x[1].lower()):
        d = e.read_inode(ino)
        apks = []
        if e.is_dir(d):
            dpath = "/app/" + name
            try:
                subs = e.listdir(dpath)
            except Exception:
                subs = []
            for sin, sname in sorted(subs, key=lambda x: x[1].lower()):
                if not sname.lower().endswith(".apk"):
                    continue
                sd = e.read_inode(sin)
                if (sd["mode"] & S_IFMT) == S_IFREG:
                    apks.append((sname, sd["size"] | (sd["size_high"] << 32)))
            if not apks:
                continue
            path = dpath
        elif name.lower().endswith(".apk"):
            apks.append((name, d["size"] | (d["size_high"] << 32)))
            path = "/app/" + name
            name = os.path.splitext(name)[0]
        else:
            continue
        apps.append(dict(name=name, path=path, apks=apks,
                         size=sum(s for _n, s in apks), icon=None))

    if not with_icons:
        return apps

    log("读取应用图标（共 %d 个）…" % len(apps))
    for i, a in enumerate(apps, 1):
        fname, fsize = max(a["apks"], key=lambda x: x[1])
        key = (a["name"], fname, fsize)
        if icon_cache is not None and key in icon_cache:
            a["icon"] = icon_cache[key]
        else:
            try:
                apk_path = a["path"] if a["path"].lower().endswith(".apk") \
                    else a["path"] + "/" + fname
                data = e.read_file(e.read_inode(e.lookup(apk_path)))
                a["icon"] = extract_icon(data)
            except Exception:
                a["icon"] = None
            if icon_cache is not None:
                icon_cache[key] = a["icon"]
        if i % 10 == 0 or i == len(apps):
            log("  图标 %d/%d" % (i, len(apps)))
    return apps


def set_adb_props(e, log=print):
    """改写 build.prop 中的 ADB 相关属性。"""
    data = e.read_file(e.read_inode(e.lookup("/build.prop")))
    txt = data.decode("utf-8", "replace")
    old = txt
    for key, value in ADB_PROPS:
        txt = _set_prop(txt, key, value)
        log("  build.prop: %s=%s" % (key, value))
    new = txt.encode("utf-8")
    if len(new) > 4096:
        raise ValueError("build.prop 超过 4096 字节，无法原地覆写")
    e.overwrite_file("/build.prop", new)
    log("build.prop: %d -> %d 字节" % (len(old.encode("utf-8")), len(new)))


def inject_init(e, log=print, telnetd=False):
    """在 init.bigfish.sh 头部注入 setprop（每次开机强制生效）。"""
    data = e.read_file(e.read_inode(e.lookup(INIT_PATH)))
    if INIT_MARK in data:
        log("init.bigfish.sh 已注入过，跳过")
        return False
    lines = [b"", INIT_MARK,
             b"setprop persist.sys.usb.config adb",
             b"setprop persist.service.adb.enable 1",
             b"setprop service.adb.tcp.port 5555",
             b"setprop sys.usb.config adb"]
    if telnetd:
        # 该机型固件没有 adbd，telnetd 是唯一可用的 root 通道
        lines.append(b"/system/bin/busybox telnetd -l /system/bin/sh -p 2323")
    inject = b"\n".join(lines) + b"\n"
    nl = data.find(b"\n")
    if nl < 0:
        new = inject + data
    else:
        new = data[:nl + 1] + inject + data[nl + 1:]
    if len(new) > 4096:
        raise ValueError("init.bigfish.sh 超过 4096 字节，无法原地覆写")
    e.overwrite_file(INIT_PATH, new)
    log("init.bigfish.sh: %d -> %d 字节%s" %
        (len(data), len(new), "（含 telnetd root shell，端口 2323）" if telnetd else ""))
    return True


def preset_apk(e, apk_path, mode="privapp", target_name=None, log=print):
    """预置 APK。

    mode="privapp" : 写入 /priv-app/<目标名>（新分配块限制在原厂 RAW 区间内，可多个）
    mode="slot"    : 覆写 /app/com.changhong.appstore 下现有的 APK 槽位（仅单个，
                     保持原文件名，避免目录项改名失败）
    """
    with open(apk_path, "rb") as f:
        data = f.read()
    if not data:
        raise ValueError("APK 文件为空: %s" % apk_path)
    digest = hashlib.sha256(data).hexdigest()
    log("APK: %s (%d 字节, sha256=%s)" % (apk_path, len(data), digest))

    if mode == "slot":
        try:
            names = [n for _i, n in e.listdir(PRESET_SLOT_DIR) if n.lower().endswith(".apk")]
        except FileNotFoundError:
            raise FileNotFoundError("镜像中不存在槽位目录 %s" % PRESET_SLOT_DIR)
        if not names:
            raise FileNotFoundError("槽位目录中没有可复用的 APK: %s" % PRESET_SLOT_DIR)
        slot = None
        best = -1
        for n in names:
            ino = e.read_inode(e.lookup(PRESET_SLOT_DIR + "/" + n))
            cap = sum(c for _l, _p, c in e.inode_blocks(ino)) * e.bs
            if cap > best:
                best, slot = cap, n
        path = PRESET_SLOT_DIR + "/" + slot
        log("  复用槽位: %s（容量 %d 字节）" % (path, best))
        if len(data) > best:
            raise ValueError(
                "APK %d 字节，超出槽位容量 %d 字节；请改用 /priv-app 模式预置" %
                (len(data), best))
        e.overwrite_file(path, data)
        log("  已写入 %s" % path)
    else:
        name = os.path.basename(target_name or apk_path)
        if not name.lower().endswith(".apk"):
            name += ".apk"
        path = PRESET_PRIVAPP_DIR + "/" + name
        try:
            e.lookup(PRESET_PRIVAPP_DIR)
        except FileNotFoundError:
            try:
                e.create_dir("/", "priv-app")
                log("  /priv-app 不存在，已创建")
            except Exception as ex:
                raise IOError("/priv-app 不存在且无法创建（%s）" % ex)
        try:
            e.lookup(path)
            e.overwrite_file(path, data)
            log("  已覆盖 %s" % path)
        except FileNotFoundError:
            try:
                e.create_file(PRESET_PRIVAPP_DIR, name, data, mode=0o100644)
                log("  已新建 %s" % path)
            except IOError as ex:
                raise IOError(
                    "无法在 /priv-app 写入（%s）。原厂 sparse 的 RAW 区间内没有可用空闲块，"
                    "请先在【2. 系统精简】勾选“释放 .apx 占用的块”并重新生成镜像。" % ex)
        log("  注意: /priv-app 新文件占用的是原厂 RAW 区间内的空闲块，"
            "若空间不足会直接报错")
    return path
