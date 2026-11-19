# -*- coding: utf-8 -*-
"""Android sparse 镜像展开与 1:1 复刻重组。

关键约束（来自已验证的刷机经验）：
  * 重组时必须与原厂 chunk 序列（类型/块数/顺序）完全一致，长度一致，无尾部填充；
  * DONT_CARE 区间在修改后的 ext4 中必须仍全零，否则升级器行为不可预期；
  * FILL / CRC32 块整块照抄。
"""
import os
import struct
import zlib

SPARSE_MAGIC = 0xED26FF3A
CH_RAW = 0xCAC1
CH_FILL = 0xCAC2
CH_DONT = 0xCAC3
CH_CRC = 0xCAC4
CHUNK_NAMES = {CH_RAW: "RAW", CH_FILL: "FILL", CH_DONT: "DONT", CH_CRC: "CRC32"}
BUF = 1 << 22


def _parse_header(hdr):
    magic, major, minor, fhs, chs, blk, tblocks, tchunks, csum = struct.unpack("<IHHHHIIII", hdr[:28])
    if magic != SPARSE_MAGIC:
        raise ValueError("不是 Android sparse 镜像 (magic=0x%08X)" % magic)
    return dict(magic=magic, major=major, minor=minor, file_hdr_sz=fhs,
                chunk_hdr_sz=chs, blk_sz=blk, total_blks=tblocks,
                total_chunks=tchunks, checksum=csum)


def layout_from_sparse(sparse_path):
    """只读扫描 sparse 文件，返回 header + chunk 描述列表（不载入数据）。"""
    with open(sparse_path, "rb") as f:
        hdr = f.read(28)
        info = _parse_header(hdr)
        if info["file_hdr_sz"] != 28 or info["chunk_hdr_sz"] != 12:
            raise ValueError("仅支持 sparse v1.0 (fhs=28/chs=12)")
        f.seek(info["file_hdr_sz"])
        chunks = []
        pos = 0
        for _ in range(info["total_chunks"]):
            h = f.read(info["chunk_hdr_sz"])
            if len(h) < 12:
                raise ValueError("chunk 头读取失败")
            ctype, _res, csz, tsz = struct.unpack("<HHII", h[:12])
            extra = b""
            if ctype in (CH_FILL, CH_CRC):
                extra = f.read(4)
                data_len = 0
            elif ctype == CH_DONT:
                data_len = 0
            elif ctype == CH_RAW:
                data_len = csz * info["blk_sz"]
            else:
                raise ValueError("未知 chunk 类型 0x%04X" % ctype)
            chunks.append(dict(type=ctype, blocks=csz, total_sz=tsz,
                               hdr=h, extra=extra, pos=pos))
            if data_len:
                f.seek(data_len, 1)
            pos += csz
        if pos != info["total_blks"]:
            raise ValueError("chunk 覆盖 %d 块，与头声明 %d 不符" % (pos, info["total_blks"]))
        end = f.tell()
        return dict(header=hdr, info=info, chunks=chunks, payload_len=end,
                    tail_len=max(0, os.path.getsize(sparse_path) - end))


def layout_from_package(pkg_path, idx):
    """扫描 LOAD 包中第 idx 项 payload 内的 sparse 布局。"""
    from .zlh_load import Package
    pkg = Package(pkg_path)
    e = pkg.entries[idx]
    if not pkg.has_payload(e):
        raise ValueError("第 %d 项没有 payload" % idx)
    with open(pkg_path, "rb") as f:
        f.seek(e["offset"] + 8)
        hdr = f.read(28)
        info = _parse_header(hdr)
        if info["file_hdr_sz"] != 28 or info["chunk_hdr_sz"] != 12:
            raise ValueError("仅支持 sparse v1.0")
        f.seek(e["offset"] + 8 + info["file_hdr_sz"])
        chunks = []
        pos = 0
        end = 28
        for _ in range(info["total_chunks"]):
            h = f.read(info["chunk_hdr_sz"])
            ctype, _res, csz, tsz = struct.unpack("<HHII", h[:12])
            extra = b""
            if ctype in (CH_FILL, CH_CRC):
                extra = f.read(4)
                data_len = 0
            elif ctype == CH_DONT:
                data_len = 0
            elif ctype == CH_RAW:
                data_len = csz * info["blk_sz"]
            else:
                raise ValueError("未知 chunk 类型 0x%04X" % ctype)
            chunks.append(dict(type=ctype, blocks=csz, total_sz=tsz,
                               hdr=h, extra=extra, pos=pos))
            if data_len:
                f.seek(data_len, 1)
            pos += csz
            end += len(h) + len(extra) + data_len
        if pos != info["total_blks"]:
            raise ValueError("chunk 覆盖 %d 块，与头声明 %d 不符" % (pos, info["total_blks"]))
    if end != e["length"]:
        raise ValueError("sparse 结构长度 %d 与表项长度 %d 不符" % (end, e["length"]))
    return dict(header=hdr, info=info, chunks=chunks, payload_len=end, tail_len=0)


def summarize_layout(layout):
    info = layout["info"]
    cnt = {}
    raw_blocks = 0
    for c in layout["chunks"]:
        cnt[c["type"]] = cnt.get(c["type"], 0) + 1
        if c["type"] == CH_RAW:
            raw_blocks += c["blocks"]
    return ("sparse v%d.%d blk=%d total_blks=%d chunks=%d "
            "RAW=%d DONT=%d FILL=%d CRC=%d raw_blocks=%d checksum=0x%08X" % (
                info["major"], info["minor"], info["blk_sz"], info["total_blks"],
                info["total_chunks"], cnt.get(CH_RAW, 0), cnt.get(CH_DONT, 0),
                cnt.get(CH_FILL, 0), cnt.get(CH_CRC, 0), raw_blocks, info["checksum"]))


def iter_chunks_stream(f, layout, blk=4096):
    """遍历 sparse 文件，yield (chunk_dict, data_bytes)；RAW 数据流式读取。"""
    f.seek(28)
    for c in layout["chunks"]:
        nbytes = c["blocks"] * blk
        if c["type"] == CH_RAW:
            left = nbytes
            while left:
                b = f.read(min(BUF, left))
                if not b:
                    raise IOError("RAW 块数据提前结束")
                yield c, b
                left -= len(b)
        else:
            if c["type"] in (CH_FILL, CH_CRC):
                f.seek(4, 1)
            yield c, b""


def expand_sparse(sparse_path, dst, log=None, progress=None):
    """把 sparse 镜像展开为完整的 raw ext4 镜像。"""
    log = log or (lambda m: None)
    layout = layout_from_sparse(sparse_path)
    info = layout["info"]
    blk = info["blk_sz"]
    total = info["total_blks"] * blk
    log("展开 sparse: %s" % summarize_layout(layout))
    zeros = bytes(blk)
    written = 0
    with open(sparse_path, "rb") as f, open(dst, "wb") as o:
        f.seek(28)
        for c in layout["chunks"]:
            nbytes = c["blocks"] * blk
            if c["type"] == CH_RAW:
                left = nbytes
                while left:
                    b = f.read(min(BUF, left))
                    if not b:
                        raise IOError("RAW 块数据提前结束")
                    o.write(b)
                    left -= len(b)
            elif c["type"] == CH_FILL:
                v = f.read(4)
                pat = (v * ((blk + 3) // 4))[:blk]
                left = nbytes
                while left:
                    n = min(len(pat), left)
                    o.write(pat[:n])
                    left -= n
            elif c["type"] == CH_DONT:
                left = nbytes
                while left:
                    n = min(len(zeros), left)
                    o.write(zeros[:n])
                    left -= n
            else:  # CRC32
                f.seek(4, 1)
            written += nbytes
            if progress:
                progress(written, total)
    if written != total:
        raise ValueError("展开结果 %d 字节，预期 %d" % (written, total))
    log("展开完成: %s (%d 字节, %d 块)" % (dst, written, info["total_blks"]))
    return written


def expand_from_package(pkg_path, idx, dst, log=None, progress=None, hasher=None):
    """从 LOAD 包第 idx 项直接展开 sparse payload 到 raw 镜像。

    hasher 传入 hashlib 对象时同步计算展开结果的摘要，避免二次读盘。
    """
    from .zlh_load import Package
    log = log or (lambda m: None)
    pkg = Package(pkg_path)
    e = pkg.entries[idx]
    if not pkg.has_payload(e):
        raise ValueError("第 %d 项没有 payload" % idx)
    with open(pkg_path, "rb") as f:
        f.seek(e["offset"] + 8)
        hdr = f.read(28)
        info = _parse_header(hdr)
        if info["file_hdr_sz"] != 28 or info["chunk_hdr_sz"] != 12:
            raise ValueError("仅支持 sparse v1.0")
        blk = info["blk_sz"]
        total = info["total_blks"] * blk
        log("展开 payload #%d: total_blks=%d blk=%d -> %d 字节" %
            (idx, info["total_blks"], blk, total))
        zero_buf = bytes(BUF)
        written = 0
        with open(dst, "wb") as o:
            pos = 0
            for _ in range(info["total_chunks"]):
                h = f.read(12)
                ctype, _res, csz, _tsz = struct.unpack("<HHII", h[:12])
                nbytes = csz * blk
                if ctype == CH_RAW:
                    left = nbytes
                    while left:
                        b = f.read(min(BUF, left))
                        if not b:
                            raise IOError("RAW 数据提前结束 (块 %d)" % pos)
                        o.write(b)
                        if hasher:
                            hasher.update(b)
                        left -= len(b)
                elif ctype == CH_FILL:
                    v = f.read(4)
                    pat = (v * ((blk + 3) // 4))[:blk]
                    left = nbytes
                    while left:
                        n = min(len(pat), left)
                        o.write(pat[:n])
                        if hasher:
                            hasher.update(pat[:n])
                        left -= n
                elif ctype == CH_DONT:
                    left = nbytes
                    while left:
                        n = min(BUF, left)
                        o.write(zero_buf[:n])
                        if hasher:
                            hasher.update(zero_buf[:n])
                        left -= n
                elif ctype == CH_CRC:
                    f.seek(4, 1)
                else:
                    raise ValueError("未知 chunk 类型 0x%04X" % ctype)
                pos += csz
                written += nbytes
                if progress:
                    progress(written, total)
        if written != total:
            raise ValueError("展开结果 %d 字节，预期 %d" % (written, total))
    log("展开完成: %s (%d 字节)" % (dst, written))
    return written


def mirror_rebuild(raw_path, layout, out_path, log=None):
    """按原厂 chunk 布局 1:1 重组 sparse（数据取自修改后的 raw 镜像）。"""
    log = log or (lambda m: None)
    info = layout["info"]
    blk = info["blk_sz"]
    total_blks = info["total_blks"]
    raw_size = os.path.getsize(raw_path)
    if raw_size != total_blks * blk:
        raise ValueError("raw 镜像 %d 字节与 sparse 总块数 %d*%d 不一致" %
                         (raw_size, total_blks, blk))
    stock_len = layout["payload_len"]
    imgcrc = info["checksum"]
    nz = 0
    nraw = ndont = nfill = ncrc = 0
    written = len(layout["header"])
    with open(raw_path, "rb") as rf, open(out_path, "wb") as out:
        out.write(layout["header"])
        crc = 0
        pos = 0
        for c in layout["chunks"]:
            nbytes = c["blocks"] * blk
            if c["type"] == CH_RAW:
                if c["total_sz"] != 12 + nbytes:
                    raise ValueError("chunk@%d RAW 尺寸字段异常" % c["pos"])
                out.write(c["hdr"])
                rf.seek(pos * blk)
                left = nbytes
                while left:
                    b = rf.read(min(BUF, left))
                    if not b:
                        raise IOError("raw 镜像在块 %d 处提前结束" % pos)
                    out.write(b)
                    if imgcrc:
                        crc = zlib.crc32(b, crc)
                    left -= len(b)
                written += 12 + nbytes
                nraw += 1
            elif c["type"] == CH_DONT:
                if c["total_sz"] != 12:
                    raise ValueError("chunk@%d DONT 尺寸字段异常" % c["pos"])
                out.write(c["hdr"])
                rf.seek(pos * blk)
                left = nbytes
                while left:
                    b = rf.read(min(BUF, left))
                    if not b:
                        raise IOError("raw 镜像在块 %d 处提前结束" % pos)
                    if b.strip(b"\x00"):
                        nz += 1
                    if imgcrc:
                        crc = zlib.crc32(b, crc)
                    left -= len(b)
                written += 12
                ndont += 1
            elif c["type"] == CH_FILL:
                if c["total_sz"] != 16:
                    raise ValueError("chunk@%d FILL 尺寸字段异常" % c["pos"])
                out.write(c["hdr"] + c["extra"])
                if imgcrc:
                    crc = zlib.crc32(c["extra"] * (nbytes // 4), crc)
                written += 16
                nfill += 1
            elif c["type"] == CH_CRC:
                out.write(c["hdr"] + c["extra"])
                written += 16
                ncrc += 1
            else:
                raise ValueError("未知 chunk 类型 0x%04X" % c["type"])
            pos += c["blocks"]
        if pos != total_blks:
            raise ValueError("块覆盖 %d != %d" % (pos, total_blks))
        if written != stock_len:
            raise ValueError("输出 %d 字节，与原厂 payload 长度 %d 不符" % (written, stock_len))
        if nz:
            raise ValueError("DONT_CARE 区间出现 %d 个非零字节，无法 1:1 复刻" % nz)
        if imgcrc:
            out.seek(24)
            out.write(struct.pack("<I", crc & 0xFFFFFFFF))
    sz = os.path.getsize(out_path)
    log("sparse 重组完成: %s (%d 字节, RAW=%d DONT=%d FILL=%d CRC=%d)" %
        (out_path, sz, nraw, ndont, nfill, ncrc))
    return sz


def check_mirror(sparse_path, layout, log=None):
    """校验重组结果与原厂 chunk 头逐字节一致且长度相同。"""
    log = log or (lambda m: None)
    with open(sparse_path, "rb") as f:
        hdr = f.read(28)
        if hdr != layout["header"]:
            raise ValueError("sparse 头与原厂不一致")
        pos = 0
        for ci, c in enumerate(layout["chunks"]):
            a = f.read(12)
            if a != c["hdr"]:
                raise ValueError("第 %d 个 chunk 头与原厂不一致" % ci)
            ctype = c["type"]
            if ctype == CH_RAW:
                f.seek(c["blocks"] * layout["info"]["blk_sz"], 1)
            elif ctype in (CH_FILL, CH_CRC):
                if f.read(4) != c["extra"]:
                    raise ValueError("第 %d 个 chunk 数据值与原厂不一致" % ci)
            pos += c["blocks"]
        if f.read(1):
            raise ValueError("chunk 结束后存在多余字节")
    if pos != layout["info"]["total_blks"]:
        raise ValueError("块覆盖不符")
    if os.path.getsize(sparse_path) != layout["payload_len"]:
        raise ValueError("长度与原厂 payload 不一致")
    log("mirror 校验通过: %d 个 chunk / %d 块 / %d 字节，结构与原厂逐字节一致" %
        (len(layout["chunks"]), pos, layout["payload_len"]))
