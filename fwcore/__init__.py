# -*- coding: utf-8 -*-
"""ZLH74GiR(hv3751) 固件改造核心模块。

模块划分：
  zlh_load  海思 LOAD 升级包解析/校验/导出
  sparse    Android sparse 展开与 1:1 复刻重组
  ext4fs    极简 ext4 就地编辑器（extents + 线性目录 + gdt_csum）
  system_edit 系统精简 / build.prop / init 注入 / APK 预置 / /app 应用扫描
  apkicon   从 APK 提取应用图标（AXML + resources.arsc）
  downloader APK 链接下载与合法性校验
  repack    LOAD 重打包（仅替换 system payload）
  verify    成品包全面校验
"""

__all__ = ["zlh_load", "sparse", "ext4fs", "system_edit", "apkicon",
           "downloader", "repack", "verify"]
