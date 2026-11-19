# -*- coding: utf-8 -*-
"""从 APK 中提取应用图标（纯标准库）。

流程：
  AndroidManifest.xml (AXML) -> <application android:icon> 的资源 ID
  resources.arsc -> 资源 ID 对应的文件路径
  zip -> 取最高密度的那张 PNG

图标若是 adaptive icon（XML），则按资源名同名的 PNG 回退。解析失败返回 None。
"""
import os
import re
import struct
import zipfile

DPI_RANK = {"ldpi": 120, "mdpi": 160, "hdpi": 240, "xhdpi": 320,
            "xxhdpi": 480, "xxxhdpi": 640, "nodpi": 0, "anydpi": 0, "tvdpi": 213}


def _len8(data, p):
    n = data[p]
    if n & 0x80:
        return ((n & 0x7F) << 8) | data[p + 1], p + 2
    return n, p + 1


def parse_string_pool(data, off):
    """解析 ResStringPool，返回字符串列表。"""
    typ, hsz, size = struct.unpack_from("<HHI", data, off)
    count, _style, flags, strings_start, _styles = struct.unpack_from("<IIIII", data, off + 8)
    utf8 = bool(flags & 0x100)
    offsets = struct.unpack_from("<%dI" % count, data, off + hsz)
    base = off + strings_start
    out = []
    for o in offsets:
        p = base + o
        try:
            if utf8:
                _n, p = _len8(data, p)
                ln, p = _len8(data, p)
                out.append(data[p:p + ln].decode("utf-8", "replace"))
            else:
                ln = struct.unpack_from("<H", data, p)[0]
                p += 2
                if ln & 0x8000:
                    ln = ((ln & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0]
                    p += 2
                out.append(data[p:p + ln * 2].decode("utf-16-le", "replace"))
        except Exception:
            out.append("")
    return out


def iter_start_elements(axml):
    """遍历 AXML 的起始标签，yield (标签名, [(属性名, dataType, data, raw)])。"""
    if len(axml) < 8:
        return
    strings = []
    pos = 8
    while pos + 8 <= len(axml):
        typ, _hsz, size = struct.unpack_from("<HHI", axml, pos)
        if size < 8:
            break
        if typ == 0x0001:
            strings = parse_string_pool(axml, pos)
        elif typ == 0x0102:
            try:
                name_idx = struct.unpack_from("<I", axml, pos + 20)[0]
                attr_start, attr_size, attr_count = struct.unpack_from("<HHH", axml, pos + 24)
                name = strings[name_idx] if name_idx < len(strings) else ""
                attrs = []
                apos = pos + 16 + attr_start
                step = attr_size or 20
                for _ in range(attr_count):
                    _ns_i, nm_i, raw_i = struct.unpack_from("<III", axml, apos)
                    _dsize, _res0, dtype, dval = struct.unpack_from("<HBBI", axml, apos + 12)
                    aname = strings[nm_i] if nm_i < len(strings) else ""
                    raw = strings[raw_i] if (raw_i != 0xFFFFFFFF and raw_i < len(strings)) else None
                    attrs.append((aname, dtype, dval, raw))
                    apos += step
                yield name, attrs
            except Exception:
                return
        pos += size


def manifest_icon(axml):
    """返回 (dataType, data, rawString) 或 None。"""
    for name, attrs in iter_start_elements(axml):
        if name == "application":
            for aname, dtype, dval, raw in attrs:
                if aname == "icon":
                    return dtype, dval, raw
    return None


def resolve_reference(arsc, resid, depth=0):
    """在 resources.arsc 中把资源 ID 解析为文件路径（TYPE_STRING 值）。"""
    if depth > 3:
        return None
    try:
        typ, hsz, _size = struct.unpack_from("<HHI", arsc, 0)
        if typ != 0x0002:
            return None
        pkg_id = (resid >> 24) & 0xFF
        type_id = (resid >> 16) & 0xFF
        entry_id = resid & 0xFFFF
        pos = hsz
        gstr = parse_string_pool(arsc, pos)
        pos += struct.unpack_from("<I", arsc, pos + 4)[0]
        while pos + 8 <= len(arsc):
            t, h, s = struct.unpack_from("<HHI", arsc, pos)
            if s < 8:
                break
            if t == 0x0200 and struct.unpack_from("<I", arsc, pos + 8)[0] == pkg_id:
                cpos = pos + h
                end = pos + s
                while cpos + 8 <= end:
                    ct, ch, cs = struct.unpack_from("<HHI", arsc, cpos)
                    if cs < 8:
                        break
                    if ct == 0x0201:  # TYPE
                        cid, _flags, _r = struct.unpack_from("<BBH", arsc, cpos + 8)
                        count, e_start = struct.unpack_from("<II", arsc, cpos + 12)
                        if cid == type_id and entry_id < count:
                            offs = struct.unpack_from("<I", arsc, cpos + ch + entry_id * 4)[0]
                            if offs != 0xFFFFFFFF:
                                epos = cpos + e_start + offs
                                esize, eflags = struct.unpack_from("<HH", arsc, epos)
                                if not (eflags & 0x0001):  # 非 bag
                                    _vs, _r0, dtype, dval = struct.unpack_from("<HBBI", arsc, epos + esize)
                                    if dtype == 0x03:
                                        return gstr[dval]
                                    if dtype == 0x01:
                                        sub = resolve_reference(arsc, dval, depth + 1)
                                        if sub:
                                            return sub
                    cpos += cs
            pos += s
    except Exception:
        return None
    return None


def _png_size(data):
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    return None


def _dpi_rank(path):
    base = path.lower()
    for k, v in DPI_RANK.items():
        if "-%s" % k in base:
            return v
    return -1


def extract_icon(apk):
    """提取 APK 图标 PNG 字节；apk 可以是路径或 bytes；失败返回 None。"""
    try:
        if isinstance(apk, (bytes, bytearray)):
            import io
            zf = zipfile.ZipFile(io.BytesIO(apk))
            close = zf.close
        else:
            zf = zipfile.ZipFile(apk)
            close = zf.close
        try:
            return _extract(zf)
        finally:
            close()
    except Exception:
        return None


def _extract(z):
    names = z.namelist()
    try:
        xml = z.read("AndroidManifest.xml")
    except KeyError:
        return None
    found = manifest_icon(xml)
    path = None
    if found:
        dtype, dval, raw = found
        if dtype == 0x03 and raw:
            path = raw
        elif dtype == 0x01:
            try:
                arsc = z.read("resources.arsc")
                path = resolve_reference(arsc, dval)
            except KeyError:
                path = None
    if path and path.lower().endswith(".xml"):
        # adaptive icon / vector drawable：从 XML 里继续找引用的图片
        try:
            png = _png_from_icon_xml(z, path)
            if png:
                return png
        except Exception:
            pass
    if path and path.lower().endswith(".png"):
        stems = {os.path.splitext(os.path.basename(path))[0]}
        cands = [n for n in names
                 if n.lower().endswith(".png") and
                 os.path.splitext(os.path.basename(n))[0] in stems]
        cands.sort(key=lambda n: -_dpi_rank(n))
        for n in cands[:4]:
            try:
                data = z.read(n)
            except KeyError:
                continue
            if _png_size(data):
                return data
    # 回退 1：常见启动图标名
    cands = [n for n in names
             if n.lower().endswith(".png") and
             ("ic_launcher" in n.lower() or "app_icon" in n.lower())]
    cands.sort(key=lambda n: -_dpi_rank(n))
    for n in cands[:4]:
        try:
            data = z.read(n)
        except KeyError:
            continue
        if _png_size(data):
            return data
    # 回退 2：res/ 下面积最大的正方形 PNG（资源名被混淆时最可靠）
    return _largest_square_png(z)


def _png_from_icon_xml(z, path):
    """adaptive-icon 等 XML：解析内部引用，返回第一张可用 PNG。"""
    try:
        arsc = z.read("resources.arsc")
    except KeyError:
        return None
    xml = z.read(path)
    for _name, attrs in iter_start_elements(xml):
        for aname, dtype, dval, raw in attrs:
            p = None
            if dtype == 0x01:
                p = resolve_reference(arsc, dval)
            elif dtype == 0x03 and raw:
                p = raw
            if p and p.lower().endswith(".png"):
                try:
                    data = z.read(p)
                except KeyError:
                    continue
                if _png_size(data):
                    return data
            elif p and p.lower().endswith(".xml"):
                sub = _png_from_icon_xml(z, p)
                if sub:
                    return sub
    return None


def _largest_square_png(z):
    best = None
    best_area = 0
    for n in z.namelist():
        low = n.lower()
        if not low.startswith("res/") or not low.endswith(".png") or low.endswith(".9.png"):
            continue
        try:
            with z.open(n) as f:
                head = f.read(24)
        except Exception:
            continue
        size = _png_size(head)
        if not size:
            continue
        w, h = size
        if w < 64 or h < 64 or max(w, h) / float(min(w, h)) > 1.35:
            continue
        if w * h > best_area:
            best_area, best = w * h, n
    if best:
        return z.read(best)
    return None


def icon_dimensions(png):
    return _png_size(png) or (0, 0)
