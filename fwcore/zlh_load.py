# -*- coding: utf-8 -*-
"""海思 LOAD 升级包（ZLH74GiR / hi3751）解析、校验与导出。

容器结构（全部大端）::

    HEADER 0x40
      0x00 char[4] "LOAD"
      0x04 u32 构建标签
      0x08 u32 table_size
      0x10 u32 整包大小
      0x30 u32 表项数
      0x34 u32 头部/引导区大小
    TABLE  表项数 * 32 字节
      u32 flash_start, 0, flash_end, type, 0, payload_len, payload_offset, 0
    (表后 256B RSA 签名 + 填充，逐字节保留)
    PAYLOAD
      u32 len, u32 crc32_mpeg2(payload), payload

实现源自 work/zlh_load.py，仅做参数化与日志回调改造。
"""
import os
import struct
import zlib

HDR_LEN = 0x40
ENTRY_LEN = 32
MAGIC = b"LOAD"

# CRC32/MPEG-2: poly 0x04C11DB7, init 0xFFFFFFFF, MSB-first, no xorout。
# 用 zlib（C 实现）计算：把每个字节按位反转后喂给 zlib（其内部为反射算法），
# 结果再做 32 位全反转即可，速度约 500MB/s（纯 Python 查表实现仅 ~10MB/s）。
_BITREV = bytes(int("{:08b}".format(i)[::-1], 2) for i in range(256))


def _brev32(v):
    b = v.to_bytes(4, "big")
    return int.from_bytes(bytes((_BITREV[b[3]], _BITREV[b[2]],
                                _BITREV[b[1]], _BITREV[b[0]])), "big")


def crc32_mpeg2(data, crc=0xFFFFFFFF):
    z = zlib.crc32(bytes(data).translate(_BITREV), _brev32(crc) ^ 0xFFFFFFFF) ^ 0xFFFFFFFF
    return _brev32(z)

# 17 项分区表对应的分区名（依据固件分析报告，idx 16 为终止项）
PART_NAMES = [
    "bootargs", "recovery", "panelparam", "deviceinfo", "logo", "kernel",
    "dtb", "atf", "trustedcore", "misc", "tconparam", "bluetooth",
    "system", "cache", "chperm", "hwcfg", "terminator",
]

SYSTEM_IDX = 12


def crc_file_range(f, off, length, bufsize=1 << 22, tick=None):
    """流式计算文件区间 CRC32/MPEG-2。"""
    f.seek(off)
    crc = 0xFFFFFFFF
    left = length
    done = 0
    while left > 0:
        b = f.read(min(bufsize, left))
        if not b:
            break
        crc = crc32_mpeg2(b, crc)
        left -= len(b)
        done += len(b)
        if tick:
            tick(done, length)
    return crc


class Package:
    """LOAD 升级包只读视图。"""

    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        with open(path, "rb") as f:
            hdr = f.read(HDR_LEN)
            if len(hdr) < HDR_LEN or hdr[:4] != MAGIC:
                raise ValueError("不是 LOAD 升级包: %r" % hdr[:4])
            self.hdr = hdr
            (self.magic, self.pkg_id, self.table_size, self.reserved,
             self.total_size, self.ver_a, self.ver_b) = struct.unpack(">4sIIIIII", hdr[:28])
            self.flags = hdr[0x1C:0x30]
            (self.count, self.region_size, self.table_size2,
             self.reserved2) = struct.unpack(">4I", hdr[0x30:0x40])
            self.entries = []
            for i in range(self.count):
                f.seek(HDR_LEN + i * ENTRY_LEN)
                raw = f.read(ENTRY_LEN)
                if len(raw) < ENTRY_LEN:
                    break
                start, _r0, end, typ, _r1, ln, off, _r2 = struct.unpack(">8I", raw)
                name = PART_NAMES[i] if i < len(PART_NAMES) else "part%02d" % i
                self.entries.append(dict(index=i, start=start, end=end, type=typ,
                                         length=ln, offset=off, raw=raw, name=name))

    # ---------- 结构信息 ----------
    def table_end(self):
        return HDR_LEN + self.count * ENTRY_LEN

    def has_payload(self, e):
        return e["offset"] != 0 and e["length"] != 0 and e["offset"] < self.size

    def first_payload_offset(self):
        offs = [e["offset"] for e in self.entries if self.has_payload(e)]
        return min(offs) if offs else self.table_end()

    def trailer(self):
        """表结束到第一个 payload 之间的字节（RSA 签名 + 填充）。"""
        te = self.table_end()
        first = self.first_payload_offset()
        if first <= te:
            return b""
        with open(self.path, "rb") as f:
            f.seek(te)
            return f.read(first - te)

    def info_lines(self):
        out = [
            "文件          : %s" % self.path,
            "文件大小      : %d 字节 (0x%X)" % (self.size, self.size),
            "包标识 @0x04  : 0x%08X" % self.pkg_id,
            "整包大小 @0x10: %d (%s)" % (self.total_size,
                                       "与文件一致" if self.total_size == self.size else "不一致!"),
            "版本 @0x14/18 : 0x%08X / 0x%08X" % (self.ver_a, self.ver_b),
            "表项数        : %d" % self.count,
            "头部区大小    : 0x%X" % self.region_size,
            "表后签名/填充 : %d 字节" % len(self.trailer()),
            "",
            "%-3s %-12s %-12s %-12s %-12s %-11s %s" %
            ("idx", "分区", "flash_start", "flash_end", "reserved", "payload_len", "file_off"),
        ]
        for e in self.entries:
            out.append("%-3d %-12s 0x%08X   0x%08X   %10d B %12d B  0x%08X" % (
                e["index"], e["name"], e["start"], e["end"],
                e["end"] - e["start"], e["length"], e["offset"]))
        return out

    # ---------- 校验 ----------
    def verify(self, progress=None, log=None):
        """逐项校验 payload 的存储长度与 CRC32/MPEG-2。"""
        res = []
        with open(self.path, "rb") as f:
            for e in self.entries:
                if not self.has_payload(e):
                    res.append((e["index"], None, None, "empty"))
                    if progress:
                        progress(e["index"], None)
                    continue
                f.seek(e["offset"])
                blen, bcrc = struct.unpack(">2I", f.read(8))

                def tick(done, total, name=e["name"]):
                    if log and total >= (64 << 20) and (
                            done == total or done % (256 << 20) < (1 << 22)):
                        log("      %-11s CRC 进度 %.0f%% (%d/%d MB)" %
                            (name, done * 100.0 / total, done >> 20, total >> 20))

                calc = crc_file_range(f, e["offset"] + 8, blen, tick=tick)
                ok = (calc == bcrc and blen == e["length"])
                res.append((e["index"], bcrc, calc, "OK" if ok else "FAIL"))
                if progress:
                    progress(e["index"], ok)
        return res

    # ---------- 导出 ----------
    def entry_file_name(self, e):
        return "%02d_%s.bin" % (e["index"], e["name"])

    def extract_payload(self, idx, dst, progress=None):
        """导出第 idx 项 payload（不含 8 字节长度/CRC 头）。"""
        e = self.entries[idx]
        if not self.has_payload(e):
            raise ValueError("第 %d 项没有 payload" % idx)
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        with open(self.path, "rb") as f, open(dst, "wb") as o:
            f.seek(e["offset"] + 8)
            left = e["length"]
            done = 0
            while left > 0:
                b = f.read(min(1 << 22, left))
                if not b:
                    break
                o.write(b)
                left -= len(b)
                done += len(b)
                if progress:
                    progress(done, e["length"])
        return dst

    def extract(self, outdir, progress=None, log=None):
        """导出全部分区 payload 以及签名/填充段。"""
        os.makedirs(outdir, exist_ok=True)
        log = log or (lambda m: None)
        files = []
        for e in self.entries:
            if not self.has_payload(e):
                log("  [%2d] %-11s 无 payload，跳过" % (e["index"], e["name"]))
                continue
            dst = os.path.join(outdir, self.entry_file_name(e))
            log("  [%2d] %-11s %12d B -> %s" % (e["index"], e["name"], e["length"],
                                              os.path.basename(dst)))
            self.extract_payload(e["index"], dst,
                                 progress=(lambda d, t: None))
            files.append(dst)
        tr = self.trailer()
        with open(os.path.join(outdir, "_trailer.bin"), "wb") as o:
            o.write(tr)
        log("  [签名] 表后签名/填充 %d B -> _trailer.bin" % len(tr))
        return files
