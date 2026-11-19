# -*- coding: utf-8 -*-
"""成品包校验：结构一致性、块 CRC、包大小、反解 system 的 SHA256 比对。"""
import hashlib
import os
import shutil
import struct
import tempfile

from . import sparse
from .zlh_load import Package, crc_file_range


def _sha256_file(path, log=None, label=""):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                break
            h.update(b)
    if log:
        log("  %s sha256=%s" % (label, h.hexdigest()))
    return h.hexdigest()


def verify_package(stock_path, custom_path, system_idx=12, raw_image=None, log=print):
    """全面校验改造包。返回 True 表示全部通过。"""
    problems = []
    if not os.path.isfile(stock_path):
        raise FileNotFoundError(stock_path)
    if not os.path.isfile(custom_path):
        raise FileNotFoundError(custom_path)

    log("=== 1. 包级结构 ===")
    stock = Package(stock_path)
    custom = Package(custom_path)
    if os.path.getsize(stock_path) != os.path.getsize(custom_path):
        problems.append("包大小不一致: %d vs %d" %
                        (os.path.getsize(stock_path), os.path.getsize(custom_path)))
        log("  包大小不一致!")
    else:
        log("  包大小一致: %d 字节" % os.path.getsize(stock_path))
    if stock.hdr != custom.hdr:
        problems.append("文件头不一致")
        log("  文件头不一致!")
    else:
        log("  文件头 0x40 字节完全一致")
    table_same = len(stock.entries) == len(custom.entries) and all(
        a["raw"] == b["raw"] for a, b in zip(stock.entries, custom.entries))
    if not table_same:
        problems.append("分区表不一致")
        log("  分区表不一致!")
    else:
        log("  分区表 %d 项完全一致" % stock.count)
    first = stock.first_payload_offset()
    with open(stock_path, "rb") as f1, open(custom_path, "rb") as f2:
        a = f1.read(first)
        b = f2.read(first)
    if a != b:
        problems.append("头部+表+签名段区不一致")
        log("  头部+表+签名/填充区不一致!")
    else:
        log("  头部+表+签名/填充区 (%d 字节) 完全一致" % first)

    log("=== 2. 各 payload 长度与 CRC32/MPEG-2 ===")
    bad = 0
    stock_crc = {}
    with open(stock_path, "rb") as sf:
        for ent in stock.entries:
            if not stock.has_payload(ent):
                continue
            sf.seek(ent["offset"])
            blen, bcrc = struct.unpack(">2I", sf.read(8))
            stock_crc[ent["index"]] = (blen, bcrc)
    with open(custom_path, "rb") as f:
        for ent in custom.entries:
            if not custom.has_payload(ent):
                continue
            f.seek(ent["offset"])
            blen, bcrc = struct.unpack(">2I", f.read(8))

            def tick(done, total, name=ent["name"]):
                if total >= (64 << 20) and (done == total or done % (256 << 20) < (1 << 22)):
                    log("      %-11s CRC 进度 %.0f%% (%d/%d MB)" %
                        (name, done * 100.0 / total, done >> 20, total >> 20))

            calc = crc_file_range(f, ent["offset"] + 8, blen, tick=tick)
            ok = (calc == bcrc) and (blen == ent["length"])
            same = stock_crc.get(ent["index"]) == (blen, bcrc)
            if not ok:
                bad += 1
                problems.append("payload %d CRC/长度校验失败" % ent["index"])
                log("  [%2d] %-11s len=%d CRC 失败 (存 %08X / 算 %08X)" %
                    (ent["index"], ent["name"], blen, bcrc, calc))
            else:
                log("  [%2d] %-11s len=%d crc=%08X OK%s" %
                    (ent["index"], ent["name"], blen, bcrc,
                     "" if same else "  (已修改)"))
    if not bad:
        log("  全部 payload 校验通过")

    log("=== 3. system payload 的 sparse 结构 ===")
    try:
        stock_layout = sparse.layout_from_package(stock_path, system_idx)
        custom_layout = sparse.layout_from_package(custom_path, system_idx)
        log("  原厂: " + sparse.summarize_layout(stock_layout))
        log("  改造: " + sparse.summarize_layout(custom_layout))
        if stock_layout["header"] != custom_layout["header"]:
            problems.append("system sparse 头与原厂不一致")
            log("  sparse 头不一致!")
        if len(stock_layout["chunks"]) != len(custom_layout["chunks"]):
            problems.append("system chunk 数量不一致")
            log("  chunk 数量不一致!")
        else:
            diff = [i for i, (a, b) in enumerate(
                zip(stock_layout["chunks"], custom_layout["chunks"]))
                if a["hdr"] != b["hdr"] or a["extra"] != b["extra"]]
            if diff:
                problems.append("system chunk 头有 %d 处不一致" % len(diff))
                log("  chunk 头不一致: %s" % diff[:10])
            else:
                log("  %d 个 chunk 的类型/块数/顺序与原厂逐字节一致" %
                    len(stock_layout["chunks"]))
        if stock_layout["payload_len"] != custom_layout["payload_len"]:
            problems.append("system payload 长度不一致")
    except Exception as ex:
        problems.append("sparse 结构检查异常: %s" % ex)
        log("  sparse 结构检查异常: %s" % ex)

    log("=== 4. 从改造包反解 system 并比对 SHA256 ===")
    tmpdir = tempfile.mkdtemp(prefix="fwverify_")
    try:
        dec = os.path.join(tmpdir, "system_from_custom.img")
        h = hashlib.sha256()
        sparse.expand_from_package(custom_path, system_idx, dec, log=log, hasher=h)
        dec_sha = h.hexdigest()
        log("  反解镜像 sha256=%s" % dec_sha)
        if raw_image and os.path.isfile(raw_image):
            src_sha = _sha256_file(raw_image, log, "对照镜像")
            if dec_sha == src_sha:
                log("  反解结果与对照镜像 SHA256 一致")
            else:
                problems.append("反解 system 与对照镜像 SHA256 不一致")
                log("  SHA256 不一致!")
        else:
            log("  未提供对照镜像，仅输出反解镜像 SHA256")
    except Exception as ex:
        problems.append("反解 system 失败: %s" % ex)
        log("  反解 system 失败: %s" % ex)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    log("")
    if problems:
        log("校验结论: 失败 (%d 项问题)" % len(problems))
        for p in problems:
            log("  - " + p)
        return False
    log("校验结论: 全部通过")
    return True
