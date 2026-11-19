# -*- coding: utf-8 -*-
"""APK 下载与校验（纯标准库）。"""
import os
import re
import urllib.parse
import urllib.request
import zipfile


def _safe_name(name):
    name = urllib.parse.unquote(name or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = "download.apk"
    if not name.lower().endswith(".apk"):
        name += ".apk"
    return name


def _pick_name(resp, url):
    cd = resp.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
    if m:
        return _safe_name(m.group(1))
    path = urllib.parse.urlparse(url).path
    return _safe_name(os.path.basename(path) or "download.apk")


def _unique(dest_dir, name):
    base, ext = os.path.splitext(name)
    out = os.path.join(dest_dir, name)
    i = 1
    while os.path.exists(out):
        out = os.path.join(dest_dir, "%s-%d%s" % (base, i, ext))
        i += 1
    return out


def validate_apk(path):
    """基本合法性检查：zip + AndroidManifest.xml 可读。"""
    try:
        with zipfile.ZipFile(path) as z:
            data = z.read("AndroidManifest.xml")
            if not data:
                raise ValueError("AndroidManifest.xml 为空")
    except zipfile.BadZipFile:
        raise ValueError("不是有效的 APK（zip 结构损坏）: %s" % os.path.basename(path))
    except KeyError:
        raise ValueError("不是有效的 APK（缺少 AndroidManifest.xml）: %s" % os.path.basename(path))
    return True


def download_apk(url, dest_dir, log=print, timeout=60):
    """下载 APK 到 dest_dir，返回本地路径；失败抛异常。"""
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("仅支持 http/https 链接: %s" % url)
    os.makedirs(dest_dir, exist_ok=True)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) fw-tool/1.0",
        "Accept": "*/*",
    })
    log("下载: %s" % url)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except Exception as ex:
        raise RuntimeError("下载失败: %s" % ex)
    with resp:
        total = int(resp.headers.get("Content-Length") or 0)
        name = _pick_name(resp, url)
        dst = _unique(dest_dir, name)
        done = 0
        last = 0
        with open(dst, "wb") as f:
            while True:
                b = resp.read(1 << 20)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if done - last >= (8 << 20):
                    last = done
                    if total:
                        log("  %.0f%% (%d/%d MB)" % (done * 100.0 / total,
                                                     done >> 20, total >> 20))
                    else:
                        log("  已下载 %d MB" % (done >> 20))
    actual = os.path.getsize(dst)
    if total and actual != total:
        raise RuntimeError("下载不完整: %d/%d 字节" % (actual, total))
    validate_apk(dst)
    log("  已保存: %s (%d 字节)" % (dst, actual))
    return dst
