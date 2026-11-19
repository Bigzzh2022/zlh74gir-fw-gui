# -*- coding: utf-8 -*-
"""极简 ext4 就地编辑器（extents + 线性目录 + gdt_csum）。

支持：查找、列目录、读文件、覆盖写（容量允许时原地写）、新建文件、新建目录、
目录项改名（新名不长于旧名）。不支持 metadata_csum / 64bit / dir_index，
正好匹配 ZLH74GiR 的 system 镜像。

实现取自 work/ext4fs.py（已在真机镜像上验证），新增可选的块分配白名单
（allowed_ranges）：只有落在指定物理块区间内的块才允许分配。用于把新文件塞进
原厂 sparse 以 RAW 块存储的区间，避免写入 DONT_CARE（刷机时会被清零）区域。
"""
import struct
import time

EXT4_EXTENTS_FL = 0x80000
S_IFMT = 0o170000
S_IFDIR = 0o040000
S_IFREG = 0o100000
S_IFLNK = 0o120000


def _mk_crc16_table():
    tab = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        tab.append(c)
    return tab


_CRC16_TAB = _mk_crc16_table()


def crc16_arc(data, crc=0xFFFF):
    """CRC-16/ARC（poly 0x8005 反射，init 0xFFFF，无 xorout）。"""
    for b in data:
        crc = (crc >> 8) ^ _CRC16_TAB[(crc ^ b) & 0xFF]
    return crc


def _p2(x, align):
    return (x + align - 1) // align * align


def gdt_checksum(uuid, group, desc_raw):
    """组描述符校验和：CRC16/ARC(uuid) -> le32(group) -> desc[0:0x1E]。"""
    crc = crc16_arc(uuid, 0xFFFF)
    crc = crc16_arc(struct.pack("<I", group), crc)
    return crc16_arc(bytes(desc_raw[0:0x1E]), crc) & 0xFFFF


class Ext4:
    def __init__(self, path, readonly=False, allowed_ranges=None):
        self.path = path
        self.f = open(path, "r+b" if not readonly else "rb")
        self.readonly = readonly
        sb = self._read(1024, 1024)
        self.sb = sb
        self.inodes_count = self.u32(sb, 0x00)
        self.blocks_count = self.u32(sb, 0x04)
        self.free_blocks = self.u32(sb, 0x0C)
        self.free_inodes = self.u32(sb, 0x10)
        self.log_block_size = self.u32(sb, 0x18)
        self.bs = 1024 << self.log_block_size
        self.bpg = self.u32(sb, 0x20)
        self.ipg = self.u32(sb, 0x28)
        self.first_ino = self.u32(sb, 0x54)
        self.inode_size = self.u16(sb, 0x58) or 128
        self.feat_compat = self.u32(sb, 0x5C)
        self.feat_incompat = self.u32(sb, 0x60)
        self.feat_ro_compat = self.u32(sb, 0x64)
        self.uuid = sb[0x68:0x78]
        self.desc_size = self.u16(sb, 0xFE) or 32
        if self.feat_incompat & 0x80:
            raise NotImplementedError("不支持 64bit 文件系统")
        if self.feat_ro_compat & 0x400:
            raise NotImplementedError("不支持 metadata_csum 文件系统")
        if self.feat_incompat & 0x200:
            raise NotImplementedError("不支持 flex_bg 文件系统")
        self.groups = (self.blocks_count - 1) // self.bpg + 1
        self.gdt_off = (2 if self.bs == 1024 else 1) * self.bs
        self.gdt = []
        for g in range(self.groups):
            raw = bytearray(self._read(self.gdt_off + g * self.desc_size, self.desc_size))
            d = dict(g=g, raw=raw)
            d["block_bitmap"] = struct.unpack("<I", raw[0:4])[0]
            d["inode_bitmap"] = struct.unpack("<I", raw[4:8])[0]
            d["inode_table"] = struct.unpack("<I", raw[8:12])[0]
            d["free_blocks"] = struct.unpack("<H", raw[12:14])[0]
            d["free_inodes"] = struct.unpack("<H", raw[14:16])[0]
            d["used_dirs"] = struct.unpack("<H", raw[16:18])[0]
            d["csum"] = struct.unpack("<H", raw[0x1E:0x20])[0]
            self.gdt.append(d)
        # 分配白名单：{group: [(idx_start, idx_end), ...]}
        self.allowed = None
        if allowed_ranges:
            self.allowed = {}
            for start, count in allowed_ranges:
                end = start + count
                g = start // self.bpg
                while start < end:
                    g_end = min(end, (g + 1) * self.bpg)
                    i0 = start - g * self.bpg
                    i1 = g_end - g * self.bpg
                    self.allowed.setdefault(g, []).append((i0, i1))
                    start = g_end
                    g += 1

    # ---------- 基础读写 ----------
    def _read(self, off, ln):
        self.f.seek(off)
        return self.f.read(ln)

    def _write(self, off, data):
        assert not self.readonly
        self.f.seek(off)
        self.f.write(data)

    @staticmethod
    def u16(b, o):
        return struct.unpack("<H", b[o:o + 2])[0]

    @staticmethod
    def u32(b, o):
        return struct.unpack("<I", b[o:o + 4])[0]

    def dtype(self, ino):
        return (ino - 1) // self.ipg

    def inode_off(self, ino):
        g = self.dtype(ino)
        idx = (ino - 1) % self.ipg
        return self.gdt[g]["inode_table"] * self.bs + idx * self.inode_size

    def read_inode(self, ino):
        raw = bytearray(self._read(self.inode_off(ino), self.inode_size))
        d = dict(ino=ino, raw=raw)
        d["mode"] = struct.unpack("<H", raw[0:2])[0]
        d["uid"] = struct.unpack("<H", raw[2:4])[0]
        d["size"] = struct.unpack("<I", raw[4:8])[0]
        d["size_high"] = struct.unpack("<I", raw[0x6C:0x70])[0]
        d["atime"] = struct.unpack("<I", raw[8:12])[0]
        d["ctime"] = struct.unpack("<I", raw[12:16])[0]
        d["mtime"] = struct.unpack("<I", raw[16:20])[0]
        d["dtime"] = struct.unpack("<I", raw[20:24])[0]
        d["gid"] = struct.unpack("<H", raw[0x18:0x1A])[0]
        d["links"] = struct.unpack("<H", raw[0x1A:0x1C])[0]
        d["blocks"] = struct.unpack("<I", raw[0x1C:0x20])[0]
        d["flags"] = struct.unpack("<I", raw[0x20:0x24])[0]
        d["i_block"] = bytes(raw[0x28:0x28 + 60])
        d["extra_isize"] = struct.unpack("<H", raw[0x80:0x82])[0]
        return d

    def write_inode(self, d):
        self._write(self.inode_off(d["ino"]), bytes(d["raw"]))

    def set_inode_fields(self, d, **kw):
        r = bytearray(d["raw"])
        if "mode" in kw:
            struct.pack_into("<H", r, 0, kw["mode"])
        if "uid" in kw:
            struct.pack_into("<H", r, 2, kw["uid"])
        if "gid" in kw:
            struct.pack_into("<H", r, 0x18, kw["gid"])
        if "size" in kw:
            struct.pack_into("<I", r, 4, kw["size"] & 0xFFFFFFFF)
        if "blocks" in kw:
            struct.pack_into("<I", r, 0x1C, kw["blocks"])
        if "links" in kw:
            struct.pack_into("<H", r, 0x1A, kw["links"])
        if "flags" in kw:
            struct.pack_into("<I", r, 0x20, kw["flags"])
        if "i_block" in kw:
            r[0x28:0x64] = kw["i_block"]
        if "ctime" in kw:
            struct.pack_into("<I", r, 12, kw["ctime"])
            struct.pack_into("<I", r, 16, kw["ctime"])
        d["raw"] = r
        for k in ("mode", "size", "blocks", "links", "flags", "i_block"):
            if k in kw:
                d[k] = kw[k]

    # ---------- 位图与分配 ----------
    def _bitmap_off(self, g, which):
        return self.gdt[g][which] * self.bs

    def _block_allowed(self, g, i):
        if self.allowed is None:
            return True
        return any(a <= i < b for a, b in self.allowed.get(g, ()))

    def _alloc_bit(self, g, which):
        off = self._bitmap_off(g, which)
        bm = bytearray(self._read(off, self.bs))
        total = self.bpg if which == "block_bitmap" else self.ipg
        for i in range(total):
            byte, bit = i // 8, i % 8
            if not bm[byte] & (1 << bit):
                if which == "block_bitmap" and not self._block_allowed(g, i):
                    continue
                bm[byte] |= 1 << bit
                self._write(off, bytes(bm))
                return i
        return None

    def _free_bit(self, g, which, idx):
        off = self._bitmap_off(g, which)
        bm = bytearray(self._read(off, self.bs))
        bm[idx // 8] &= ~(1 << (idx % 8))
        self._write(off, bytes(bm))

    def alloc_block(self, hint_group=None):
        order = list(range(self.groups))
        if hint_group is not None:
            order = [hint_group] + [g for g in order if g != hint_group]
        for g in order:
            if self.gdt[g]["free_blocks"] <= 0:
                continue
            i = self._alloc_bit(g, "block_bitmap")
            if i is None:
                continue
            blk = g * self.bpg + i
            self.gdt[g]["free_blocks"] -= 1
            self.free_blocks -= 1
            return blk
        if self.allowed is not None:
            raise IOError("白名单区间内没有空闲块（无法安全分配）")
        raise IOError("没有空闲块")

    def alloc_inode(self):
        for g in range(self.groups):
            if self.gdt[g]["free_inodes"] <= 0:
                continue
            i = self._alloc_bit(g, "inode_bitmap")
            if i is None:
                continue
            ino = g * self.ipg + i + 1
            self.gdt[g]["free_inodes"] -= 1
            self.free_inodes -= 1
            return ino
        raise IOError("没有空闲 inode")

    # ---------- extents ----------
    def inode_blocks(self, d):
        ib = d["i_block"]
        if not (d["flags"] & EXT4_EXTENTS_FL):
            out = []
            for i in range(12):
                b = struct.unpack("<I", ib[i * 4:i * 4 + 4])[0]
                if b:
                    out.append((i, b, 1))
            return out
        return self._walk_extent_node(ib)

    def _walk_extent_node(self, node):
        magic, entries, _max_e, depth, _gen = struct.unpack("<HHHHI", node[:12])
        assert magic == 0xF30A, "extent magic 异常 %04X" % magic
        out = []
        for i in range(entries):
            e = node[12 + i * 12: 12 + (i + 1) * 12]
            if depth == 0:
                lblk, ln, hi, lo = struct.unpack("<IHHI", e)
                out.append((lblk, (hi << 32) | lo, ln))
            else:
                _lblk, lo, hi, _u = struct.unpack("<IIHH", e)
                child = self._read(((hi << 32) | lo) * self.bs, self.bs)
                out.extend(self._walk_extent_node(child))
        return out

    def read_file(self, d):
        size = d["size"] | (d["size_high"] << 32)
        out = bytearray()
        for _lblk, pblk, cnt in sorted(self.inode_blocks(d)):
            if len(out) >= size:
                break
            n = min(cnt * self.bs, size - len(out))
            out += self._read(pblk * self.bs, n)
        return bytes(out[:size])

    def _build_extent_tree(self, runs):
        if len(runs) <= 4:
            out = bytearray(struct.pack("<HHHHI", 0xF30A, len(runs), 4, 0, 0))
            lblk = 0
            for phys, ln in runs:
                out += struct.pack("<IHHI", lblk, ln, (phys >> 32) & 0xFFFF, phys & 0xFFFFFFFF)
                lblk += ln
            return bytes(out.ljust(60, b"\x00"))
        leaf = self.alloc_block()
        ents = []
        lblk = 0
        for phys, ln in runs:
            ents.append((lblk, phys, ln))
            lblk += ln
        if len(ents) > 340:
            raise NotImplementedError("extent 数量过多")
        buf = bytearray(self.bs)
        struct.pack_into("<HHHHI", buf, 0, 0xF30A, len(ents), 340, 0, 0)
        for i, (l, p, n) in enumerate(ents):
            struct.pack_into("<IHHI", buf, 12 + i * 12, l, n, (p >> 32) & 0xFFFF, p & 0xFFFFFFFF)
        self._write(leaf * self.bs, bytes(buf))
        node = bytearray(60)
        struct.pack_into("<HHHHI", node, 0, 0xF30A, 1, 4, 1, 0)
        struct.pack_into("<IIHH", node, 12, 0, leaf & 0xFFFFFFFF, (leaf >> 32) & 0xFFFF, 0)
        return bytes(node)

    def _alloc_runs(self, nbytes, hint_group=None):
        need = _p2(nbytes, self.bs) // self.bs
        runs = []
        while need > 0:
            start = self.alloc_block(hint_group)
            cnt = 1
            while cnt < need:
                nxt = start + cnt
                g = nxt // self.bpg
                i = nxt % self.bpg
                if not self._block_allowed(g, i):
                    break
                off = self._bitmap_off(g, "block_bitmap")
                bm = self._read(off + (i // 8), 1)
                if bm and not (bm[0] & (1 << (i % 8))):
                    self._alloc_bit_exact(g, "block_bitmap", i)
                    self.gdt[g]["free_blocks"] -= 1
                    self.free_blocks -= 1
                    cnt += 1
                else:
                    break
            runs.append((start, cnt))
            need -= cnt
        return runs

    def _alloc_bit_exact(self, g, which, i):
        off = self._bitmap_off(g, which)
        bm = bytearray(self._read(off, self.bs))
        bm[i // 8] |= 1 << (i % 8)
        self._write(off, bytes(bm))

    # ---------- 目录 ----------
    def _dir_entries(self, d, block_data):
        pos = 0
        while pos + 8 <= len(block_data):
            ino, rec_len, name_len, ftype = struct.unpack("<IHBB", block_data[pos:pos + 8])
            if rec_len < 8 or pos + rec_len > len(block_data):
                break
            name = block_data[pos + 8:pos + 8 + name_len]
            yield pos, ino, rec_len, name_len, ftype, name
            pos += rec_len

    def lookup(self, path):
        assert path.startswith("/")
        parts = [p for p in path.split("/") if p]
        ino = 2
        for p in parts:
            d = self.read_inode(ino)
            if not self.is_dir(d):
                raise NotADirectoryError(path)
            found = None
            for _lblk, pblk, cnt in sorted(self.inode_blocks(d)):
                for c in range(cnt):
                    blk = self._read((pblk + c) * self.bs, self.bs)
                    for _pos, e_ino, _rec, _nl, _ft, nm in self._dir_entries(d, blk):
                        if nm == p.encode():
                            found = e_ino
                            break
                    if found:
                        break
                if found:
                    break
            if not found:
                raise FileNotFoundError(path)
            ino = found
        return ino

    @staticmethod
    def is_dir(d):
        return (d["mode"] & S_IFMT) == S_IFDIR

    def listdir(self, path):
        d = self.read_inode(self.lookup(path))
        out = []
        for _lblk, pblk, cnt in sorted(self.inode_blocks(d)):
            for c in range(cnt):
                blk = self._read((pblk + c) * self.bs, self.bs)
                for _pos, e_ino, _rec, _nl, _ft, nm in self._dir_entries(d, blk):
                    if e_ino and nm not in (b".", b".."):
                        out.append((e_ino, nm.decode("utf-8", "replace")))
        return out

    def _add_dir_entry(self, dir_ino, name, child_ino, ftype=2):
        d = self.read_inode(dir_ino)
        nb = len(name.encode())
        need = _p2(8 + nb, 4)
        blocks = sorted(self.inode_blocks(d))
        for _lblk, pblk, cnt in blocks:
            for c in range(cnt):
                boff = (pblk + c) * self.bs
                blk = bytearray(self._read(boff, self.bs))
                for pos, _e_ino, rec, nl, _ft, _nm in self._dir_entries(d, bytes(blk)):
                    used = _p2(8 + nl, 4)
                    if rec - used >= need:
                        new_pos = pos + used
                        struct.pack_into("<IHBB", blk, new_pos, child_ino, rec - used, nb, ftype)
                        blk[new_pos + 8:new_pos + 8 + nb] = name.encode()
                        struct.pack_into("<H", blk, pos + 4, used)
                        self._write(boff, bytes(blk))
                        return
        newblk = self.alloc_block(self.dtype(dir_ino))
        blk = bytearray(self.bs)
        struct.pack_into("<IHBB", blk, 0, child_ino, self.bs, nb, ftype)
        blk[8:8 + nb] = name.encode()
        self._write(newblk * self.bs, bytes(blk))
        runs = [(p, c) for _l, p, c in blocks] + [(newblk, 1)]
        self._set_data_blocks(d, runs, (d["size"] or 0) + self.bs)
        self.write_inode(d)

    def _set_data_blocks(self, d, runs, new_size):
        ib = self._build_extent_tree(runs)
        total_blocks = sum(c for _, c in runs)
        self.set_inode_fields(d, i_block=ib, size=new_size,
                              blocks=total_blocks * (self.bs // 512),
                              flags=d["flags"] | EXT4_EXTENTS_FL)

    def create_dir(self, parent_path, name, mode=0o040755):
        parent = self.lookup(parent_path)
        ino = self.alloc_inode()
        blk = self.alloc_block(self.dtype(parent))
        buf = bytearray(self.bs)
        struct.pack_into("<IHBB", buf, 0, ino, 12, 1, 2)
        buf[8:9] = b"."
        struct.pack_into("<IHBB", buf, 12, parent, self.bs - 12, 2, 2)
        buf[20:22] = b".."
        self._write(blk * self.bs, bytes(buf))
        now = int(time.time())
        raw = bytearray(self.inode_size)
        struct.pack_into("<H", raw, 0, mode)
        struct.pack_into("<I", raw, 4, self.bs)
        struct.pack_into("<I", raw, 12, now)
        struct.pack_into("<I", raw, 16, now)
        struct.pack_into("<H", raw, 0x1A, 2)
        struct.pack_into("<I", raw, 0x1C, self.bs // 512)
        struct.pack_into("<I", raw, 0x20, EXT4_EXTENTS_FL)
        if self.inode_size > 128:
            struct.pack_into("<H", raw, 0x80, 32)
        d = dict(ino=ino, raw=raw, flags=EXT4_EXTENTS_FL, size=self.bs, mode=mode)
        ib = self._build_extent_tree([(blk, 1)])
        raw[0x28:0x64] = ib
        self.write_inode(d)
        self._add_dir_entry(parent, name, ino, ftype=2)
        pd = self.read_inode(parent)
        self.set_inode_fields(pd, links=pd["links"] + 1)
        self.write_inode(pd)
        self.gdt[self.dtype(parent)]["used_dirs"] += 1
        self.gdt[self.dtype(ino)]["used_dirs"] += 1
        return ino

    def create_file(self, parent_path, name, data, mode=0o100644, uid=0, gid=0, ftype=1):
        parent = self.lookup(parent_path)
        ino = self.alloc_inode()
        hint = self.dtype(parent)
        runs = []
        if data:
            runs = self._alloc_runs(len(data), hint)
            pos = 0
            for phys, cnt in runs:
                n = min(cnt * self.bs, len(data) - pos)
                self._write(phys * self.bs, data[pos:pos + n])
                if n < cnt * self.bs:
                    self._write(phys * self.bs + n, b"\x00" * (cnt * self.bs - n))
                pos += n
        now = int(time.time())
        raw = bytearray(self.inode_size)
        struct.pack_into("<H", raw, 0, mode)
        struct.pack_into("<H", raw, 2, uid)
        struct.pack_into("<I", raw, 4, len(data))
        struct.pack_into("<I", raw, 8, now)
        struct.pack_into("<I", raw, 12, now)
        struct.pack_into("<I", raw, 16, now)
        struct.pack_into("<H", raw, 0x18, gid)
        struct.pack_into("<H", raw, 0x1A, 1)
        total = sum(c for _, c in runs)
        struct.pack_into("<I", raw, 0x1C, total * (self.bs // 512))
        struct.pack_into("<I", raw, 0x20, EXT4_EXTENTS_FL)
        if len(data) > 0xFFFFFFFF:
            struct.pack_into("<I", raw, 0x6C, len(data) >> 32)
        if self.inode_size > 128:
            struct.pack_into("<H", raw, 0x80, 32)
        ib = self._build_extent_tree(runs) if runs else b"\x00" * 60
        raw[0x28:0x64] = ib
        self.write_inode(dict(ino=ino, raw=raw, flags=EXT4_EXTENTS_FL, size=len(data)))
        self._add_dir_entry(parent, name, ino, ftype=ftype)
        return ino

    def overwrite_file(self, path, data):
        """覆盖已有普通文件；容量不足时重新分配（受白名单约束）。"""
        ino = self.lookup(path)
        d = self.read_inode(ino)
        if (d["mode"] & S_IFMT) != S_IFREG:
            raise ValueError("不是普通文件: %s" % path)
        cur_runs = [(p, c) for _l, p, c in sorted(self.inode_blocks(d))]
        cur_bytes = sum(c for _, c in cur_runs) * self.bs
        if len(data) <= cur_bytes:
            pos = 0
            for phys, cnt in cur_runs:
                n = min(cnt * self.bs, len(data) - pos)
                if n > 0:
                    self._write(phys * self.bs, data[pos:pos + n])
                    pos += n
            if pos < len(data):
                raise IOError("写入超出文件容量")
            self.set_inode_fields(d, size=len(data))
            self.write_inode(d)
        else:
            for phys, cnt in cur_runs:
                for k in range(cnt):
                    b = phys + k
                    g = b // self.bpg
                    self._free_bit(g, "block_bitmap", b % self.bpg)
                    self.gdt[g]["free_blocks"] += 1
                    self.free_blocks += 1
            runs = self._alloc_runs(len(data), self.dtype(ino))
            pos = 0
            for phys, cnt in runs:
                n = min(cnt * self.bs, len(data) - pos)
                self._write(phys * self.bs, data[pos:pos + n])
                pos += n
            self._set_data_blocks(d, runs, len(data))
            self.write_inode(d)

    def truncate_file(self, path):
        """把文件截断为 0 字节并释放其占用的块（用于精简后腾出空间）。"""
        ino = self.lookup(path)
        d = self.read_inode(ino)
        if (d["mode"] & S_IFMT) != S_IFREG:
            raise ValueError("不是普通文件: %s" % path)
        runs = [(p, c) for _l, p, c in self.inode_blocks(d)]
        for phys, cnt in runs:
            for k in range(cnt):
                b = phys + k
                g = b // self.bpg
                self._free_bit(g, "block_bitmap", b % self.bpg)
                self.gdt[g]["free_blocks"] += 1
                self.free_blocks += 1
        empty_ib = struct.pack("<HHHHI", 0xF30A, 0, 4, 0, 0).ljust(60, b"\x00")
        self.set_inode_fields(d, size=0, blocks=0, i_block=empty_ib)
        self.write_inode(d)
        return sum(c for _p, c in runs) * self.bs

    def rename_entry(self, dir_path, old, new):
        """目录项改名，新名不能长于旧名。"""
        old_b, new_b = old.encode(), new.encode()
        if len(new_b) > len(old_b):
            raise ValueError("新文件名(%d)长于旧名(%d): %s -> %s" %
                             (len(new_b), len(old_b), old, new))
        d = self.read_inode(self.lookup(dir_path))
        for _lblk, pblk, cnt in sorted(self.inode_blocks(d)):
            for c in range(cnt):
                boff = (pblk + c) * self.bs
                blk = bytearray(self._read(boff, self.bs))
                for pos, _e_ino, _rec, nl, _ft, nm in self._dir_entries(d, bytes(blk)):
                    if nm == old_b:
                        blk[pos + 8:pos + 8 + nl] = b"\x00" * nl
                        blk[pos + 8:pos + 8 + len(new_b)] = new_b
                        struct.pack_into("<B", blk, pos + 6, len(new_b))
                        self._write(boff, bytes(blk))
                        return _e_ino
        raise FileNotFoundError("%s/%s" % (dir_path, old))

    # ---------- 计数与校验和 ----------
    def update_gdt_checksums(self):
        for g in range(self.groups):
            d = self.gdt[g]
            raw = bytearray(d["raw"])
            struct.pack_into("<H", raw, 12, d["free_blocks"])
            struct.pack_into("<H", raw, 14, d["free_inodes"])
            struct.pack_into("<H", raw, 16, d["used_dirs"])
            if self.feat_ro_compat & 0x10:
                crc = gdt_checksum(self.uuid, g, raw)
                struct.pack_into("<H", raw, 0x1E, crc)
            d["raw"] = raw
            self._write(self.gdt_off + g * self.desc_size, bytes(raw))

    def flush_superblock(self):
        sb = bytearray(self.sb)
        struct.pack_into("<I", sb, 0x0C, self.free_blocks)
        struct.pack_into("<I", sb, 0x10, self.free_inodes)
        struct.pack_into("<I", sb, 0x30, int(time.time()))
        self.sb = bytes(sb)
        self._write(1024, bytes(sb))

    def verify_gdt(self):
        """返回 (正确组数, 总组数)。"""
        ok = 0
        for g in range(self.groups):
            raw = bytes(self.gdt[g]["raw"])
            want = struct.unpack("<H", raw[0x1E:0x20])[0]
            if gdt_checksum(self.uuid, g, raw) == want:
                ok += 1
        return ok, self.groups

    def close(self):
        if not self.readonly:
            self.update_gdt_checksums()
            self.flush_superblock()
        self.f.close()
