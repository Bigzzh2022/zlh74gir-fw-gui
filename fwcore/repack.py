# -*- coding: utf-8 -*-
"""LOAD 重打包：只替换 system payload，其余字节逐字节照抄。

头部、17 项分区表、表后 256B 签名段与填充全部原样复制；每个 payload 的长度/CRC
头重新计算；输出总体积必须与原厂包完全相同。
基于 work/repack_load.py（已验证）。
"""
import hashlib
import os
import struct

from .zlh_load import Package, crc32_mpeg2


def repack(stock_path, new_payload_path, out_path, system_idx=12, log=print):
    pkg = Package(stock_path)
    stock_size = pkg.size
    new_len = os.path.getsize(new_payload_path)
    e = pkg.entries[system_idx]
    if not pkg.has_payload(e):
        raise ValueError("第 %d 项没有 payload" % system_idx)
    log("原厂包: %d 字节, %d 个表项, 首个 payload @0x%X" %
        (stock_size, pkg.count, pkg.first_payload_offset()))
    log("替换 payload #%d (%s): 表项长度 %d, 新 payload %d" %
        (system_idx, e["name"], e["length"], new_len))
    if new_len != e["length"]:
        raise ValueError("新 payload 长度 %d 与表项长度 %d 不一致，无法保持包结构" %
                         (new_len, e["length"]))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    first = pkg.first_payload_offset()
    with open(stock_path, "rb") as f, open(out_path, "wb") as o:
        # 1) 文件头 + 分区表 + 签名段 + 填充
        f.seek(0)
        left = first
        while left:
            b = f.read(min(1 << 22, left))
            o.write(b)
            left -= len(b)
        written = first

        # 2) 按文件顺序复制全部 payload
        with open(new_payload_path, "rb") as np:
            for idx, ent in enumerate(pkg.entries):
                off, ln = ent["offset"], ent["length"]
                if off == 0 or ln == 0 or off >= stock_size:
                    continue
                if off != written:
                    raise ValueError("表项 %d 偏移 0x%X 与预期 0x%X 不连续" %
                                     (idx, off, written))
                f.seek(off)
                blen, bcrc = struct.unpack(">2I", f.read(8))
                if blen != ln:
                    raise ValueError("表项 %d 存储长度 %d 与表长度 %d 不符" %
                                     (idx, blen, ln))
                if idx == system_idx:
                    hdr_pos = o.tell()
                    o.write(b"\x00" * 8)          # 先占位，边写边算，稍后回填
                    crc = 0xFFFFFFFF
                    h = hashlib.sha256()
                    left = ln
                    while left:
                        b = np.read(min(1 << 23, left))
                        if not b:
                            raise IOError("新 payload 提前结束")
                        crc = crc32_mpeg2(b, crc)
                        h.update(b)
                        o.write(b)
                        left -= len(b)
                    end = o.tell()
                    o.seek(hdr_pos)
                    o.write(struct.pack(">2I", ln, crc))
                    o.seek(end)
                    log("payload #%d 已替换: len=%d crc=%08X sha256=%s" %
                        (idx, ln, crc, h.hexdigest()))
                else:
                    o.write(struct.pack(">2I", blen, bcrc))
                    left = ln
                    while left:
                        b = f.read(min(1 << 22, left))
                        o.write(b)
                        left -= len(b)
                written += 8 + ln

    out_size = os.path.getsize(out_path)
    log("输出: %s (%d 字节, 原厂 %d, 差 %d)" %
        (out_path, out_size, stock_size, out_size - stock_size))
    if out_size != stock_size:
        raise ValueError("输出包大小 %d 与原厂 %d 不一致" % (out_size, stock_size))
    return out_size
