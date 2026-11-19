#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZLH74GiR（长虹 hi3751）固件改造工具 —— Tkinter 图形界面入口。

启动: python main.py
仅依赖 Python 3.11 标准库，无需安装第三方包。
"""
import base64
import os
import queue
import re
import sys
import threading
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, scrolledtext, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from fwcore import downloader, repack, sparse, system_edit, verify, zlh_load
    from fwcore.ext4fs import Ext4
except ImportError as _ex:  # 模块缺失时给出明确提示
    import tkinter.messagebox as _mb
    _r = tk.Tk()
    _r.withdraw()
    _mb.showerror(
        "无法加载核心模块",
        "导入 fwcore 失败:\n%s\n\n请确认 main.py 与 fwcore 目录在同一文件夹内，"
        "并使用 Python 3.11 运行。" % _ex)
    sys.exit(1)

APP_TITLE = "ZLH74GiR 固件改造工具"

if getattr(sys, "frozen", False):
    # 打包成 exe 后，资源 / 默认工作位置跟随 exe 所在目录（不是 PyInstaller 临时解包目录）
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = HERE

RESULT_STYLES = {
    "idle": ("#f2f3f5", "#444444"),
    "info": ("#e8f0fe", "#174ea6"),
    "ok": ("#e6f4ea", "#146c2e"),
    "fail": ("#fdecea", "#b3261e"),
    "warn": ("#fef7e0", "#8a5b00"),
}


def resource_path(*parts):
    """查找随程序分发的资源：先看 exe 同级目录，再看打包内部目录。"""
    bases = [APP_DIR]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and os.path.abspath(meipass) != os.path.abspath(APP_DIR):
        bases.append(meipass)
    for base in bases:
        path = os.path.join(base, *parts)
        if os.path.exists(path):
            return path
    return None


class ToolTip:
    """轻量悬停提示；content 可以是字符串或返回字符串的函数。"""

    def __init__(self, widget, content, delay=450):
        self.widget = widget
        self.content = content
        self.delay = delay
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self):
        self._after = None
        text = self.content() if callable(self.content) else self.content
        if not text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry("+%d+%d" % (x, y))
        tk.Label(self._tip, text=text, justify="left", bg="#2c2c2c", fg="#ffffff",
                 padx=9, pady=6, wraplength=520,
                 font=("Microsoft YaHei UI", 8)).pack()

    def _hide(self, _e=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class PathField(ttk.Frame):
    """路径输入框。

    - 空值时只显示“未选择”，说明文字走右侧问号提示，不塞进框里；
    - 空值呈中性灰，选定后边框转为正常色并出现 ✕ 清除按钮；
    - 未聚焦时路径过长则中间省略（保留盘符与文件名），悬停显示完整路径；
    - 聚焦时恢复完整路径，便于直接编辑。
    """

    BG_EMPTY = "#f6f7f9"
    BD_EMPTY = "#d6d9dd"
    BG_FILL = "#ffffff"
    BD_FILL = "#b7cdea"
    BD_FOCUS = "#2f7dfa"

    def __init__(self, parent, textvariable, purpose=""):
        super().__init__(parent)
        self.var = textvariable
        self.purpose = purpose
        self._editing = False
        self._sync = False
        self.dvar = tk.StringVar(value="")
        self.entry = tk.Entry(self, textvariable=self.dvar, relief="flat", bd=0,
                              bg=self.BG_EMPTY, fg="#202124",
                              insertbackground="#202124",
                              highlightthickness=1,
                              highlightbackground=self.BD_EMPTY,
                              highlightcolor=self.BD_FOCUS,
                              font=("Consolas", 9))
        self.entry.pack(fill="both", expand=True, ipady=4)
        self.hint = tk.Label(self.entry, text="未选择", fg="#9aa0a6",
                             bg=self.BG_EMPTY, anchor="w", cursor="xterm")
        self.clear = tk.Label(self.entry, text="✕", fg="#9aa0a6", bg=self.BG_FILL,
                              cursor="hand2", font=("Segoe UI", 8))
        self.hint.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.clear.bind("<Button-1>", self._clear)
        self.clear.bind("<Enter>", lambda _e: self.clear.configure(fg="#b3261e"))
        self.clear.bind("<Leave>", lambda _e: self.clear.configure(fg="#9aa0a6"))
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.entry.bind("<Configure>", lambda _e: self._refresh_display())
        self.var.trace_add("write", lambda *_a: self._var_changed())
        self.dvar.trace_add("write", lambda *_a: self._display_changed())
        ToolTip(self.entry, self._tip_text, delay=500)
        self._apply()

    # ---------------------------------------------------------------- 状态
    def _tip_text(self):
        val = self.var.get().strip()
        return val if val else self.purpose

    def _focus_in(self, _e=None):
        self._editing = True
        self._apply()

    def _focus_out(self, _e=None):
        self._editing = False
        self._apply()

    def _clear(self, _e=None):
        self.var.set("")
        self.entry.focus_set()
        return "break"

    def _var_changed(self):
        if not self._editing and not self._sync:
            self._refresh_display()
        self._apply()

    def _display_changed(self):
        if self._editing and not self._sync and self.var.get() != self.dvar.get():
            self.var.set(self.dvar.get())

    def _apply(self):
        filled = bool(self.var.get().strip())
        bg = self.BG_FILL if filled else self.BG_EMPTY
        bd = self.BD_FILL if filled else self.BD_EMPTY
        self.entry.configure(bg=bg, highlightbackground=bd)
        self.hint.configure(bg=bg)
        self.clear.configure(bg=bg)
        if filled:
            self.hint.place_forget()
            self.clear.place(relx=1.0, x=-7, rely=0.5, anchor="e")
        else:
            self.clear.place_forget()
            self.hint.place(x=7, rely=0.5, anchor="w")
        if self._editing:
            if self.dvar.get() != self.var.get():
                self._sync = True
                self.dvar.set(self.var.get())
                self._sync = False
        else:
            self._refresh_display()

    # ---------------------------------------------------------------- 省略
    def _refresh_display(self):
        if self._editing:
            return
        target = self._elide(self.var.get())
        if self.dvar.get() != target:
            self._sync = True
            self.dvar.set(target)
            self._sync = False

    def _elide(self, text):
        text = text.strip()
        if not text:
            return ""
        avail = self.entry.winfo_width() - 26
        if avail < 60:
            return text
        try:
            font = tkfont.Font(font=self.entry.cget("font"))
        except tk.TclError:
            return text
        if font.measure(text) <= avail:
            return text
        drive, tail = os.path.splitdrive(text)
        sep = "\\" if (drive or tail[:1] in ("\\", "/")) else ""
        name = os.path.basename(text) or text
        base, ext = os.path.splitext(name)
        prefix = drive + sep
        cand = prefix + "…" + sep + name
        if font.measure(cand) > avail:
            while base and font.measure(prefix + "…" + sep + base + "…" + ext) > avail:
                if len(base) <= 2:
                    break
                base = base[:-1]
            cand = prefix + "…" + sep + base + "…" + ext
        if font.measure(cand) > avail:
            cand = "…" + ext if ext else "…"
        return cand


class HintBar(tk.Frame):
    """页面提示条：无内容时自动隐藏；“尚未/缺少”用中性色，真正异常用红色。"""

    STYLES = {"info": ("#eef3fa", "#1f4e79"),
              "error": ("#fdecea", "#b3261e"),
              "warn": ("#fef7e0", "#8a5b00")}

    def __init__(self, parent, textvariable, kind="info"):
        bg, fg = self.STYLES.get(kind, self.STYLES["info"])
        super().__init__(parent, bg=bg)
        self.var = textvariable
        self._opts = None
        self._accent = tk.Frame(self, bg=fg, width=3)
        self._accent.pack(side="left", fill="y")
        self.label = tk.Label(self, textvariable=textvariable, bg=bg, fg=fg,
                              anchor="w", justify="left", padx=9, pady=5,
                              font=("Microsoft YaHei UI", 9))
        self.label.pack(side="left", fill="both", expand=True)
        self.bind("<Configure>", lambda e: self.label.configure(
            wraplength=max(200, e.width - 24)))
        self.var.trace_add("write", lambda *_a: self._sync())

    def grid(self, **kw):
        if kw:
            self._opts = dict(kw)
        if self._opts:
            super().grid(**self._opts)
        return None

    def _sync(self):
        text = self.var.get().strip()
        if not text:
            if self._opts:
                super().grid_remove()
            return
        if text.startswith(("尚未", "缺少")):
            bg, fg = self.STYLES["info"]
        else:
            bg, fg = self.STYLES["error"]
        self.configure(bg=bg)
        self._accent.configure(bg=fg)
        self.label.configure(bg=bg, fg=fg)
        if self._opts:
            super().grid(**self._opts)


def human(n):
    """字节数转可读文本。"""
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


class App:
    # ================================================================== 初始化
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1120x880")
        self.root.minsize(980, 720)
        self._set_window_icon()
        self._setup_style()

        self.log_queue = queue.Queue()
        self.busy = False
        self._buttons = []          # [(widget, predicate)]
        self._icons = {}            # 应用名 -> PhotoImage
        self._icon_cache = {}       # (应用名, apk, 大小) -> PNG bytes
        self._apps = {}             # 应用名 -> 扫描结果
        self._app_sel = set()       # 已勾选的应用名
        self._scanned_img = None    # 扫描时用的镜像路径
        self._apk_items = {}        # tree iid -> dict
        self._apk_seq = 0

        # 跨页共享路径（启动时全部为空，由用户选择后推导或手动填写）
        self.var_pkg = tk.StringVar(value="")
        self.var_extract = tk.StringVar(value="")
        self.var_raw = tk.StringVar(value="")
        self.var_slim = tk.StringVar(value="")
        self.var_final = tk.StringVar(value="")
        self.var_out = tk.StringVar(value="")
        self.var_verify_img = tk.StringVar(value="")

        # 各页“为什么不能执行”的提示
        self.var_hint_unpack = tk.StringVar(value="")
        self.var_hint_debloat = tk.StringVar(value="")
        self.var_hint_root = tk.StringVar(value="")
        self.var_hint_pack = tk.StringVar(value="")
        self.var_hint_verify = tk.StringVar(value="")

        # 精简页
        self.var_manual = tk.StringVar(value="")
        self.var_search = tk.StringVar(value="")
        self.var_release = tk.BooleanVar(value=True)
        self.var_apps_info = tk.StringVar(value="尚未扫描。")
        self.var_sel_info = tk.StringVar(value="已选 0 个应用")

        # root / 预置页
        self.var_adb = tk.BooleanVar(value=True)
        self.var_init = tk.BooleanVar(value=True)
        self.var_telnetd = tk.BooleanVar(value=True)
        self.var_mode = tk.StringVar(value="privapp")

        # 状态区
        self.var_status = tk.StringVar(value="就绪")
        self.var_steps = tk.StringVar(value="")

        self._build_top()
        self._build_bottom()
        self._build_notebook()

        self.var_pkg.trace_add("write", lambda *_a: self._on_pkg_changed())
        self.var_extract.trace_add("write", lambda *_a: self._on_extract_changed())
        self.var_raw.trace_add("write", lambda *_a: self._on_raw_changed())
        self.var_slim.trace_add("write", lambda *_a: self._on_slim_changed())
        self.var_search.trace_add("write", lambda *_a: self._refresh_debloat_tree())

        self._tick()
        self.root.after(100, self._drain_log)
        self.log("工具已就绪：Python %s，核心模块加载成功。" % sys.version.split()[0])
        self.log("所有路径为空；选定源升级包后按包所在目录推导工作路径，也可逐项手动指定。")
        self.log("可执行文件目录: %s" % APP_DIR)
        self.log("流程: ① 解包 → ② 系统精简 → ③ root/ADB/预置 → ④ 打包 → ⑤ 校验")

    def _set_window_icon(self):
        ico = resource_path("assets", "app.ico")
        if ico:
            try:
                self.root.iconbitmap(bitmap=ico)
                return
            except tk.TclError:
                pass
        png = resource_path("assets", "app_32.png")
        if png:
            try:
                self._icon_photo = tk.PhotoImage(file=png)
                self.root.iconphoto(True, self._icon_photo)
            except tk.TclError:
                pass

    def _setup_style(self):
        style = ttk.Style()
        style.configure("TNotebook.Tab", padding=(16, 7),
                        font=("Microsoft YaHei UI", 9))
        style.configure("TButton", padding=(11, 4), font=("Microsoft YaHei UI", 9))
        style.configure("TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("TCheckbutton", font=("Microsoft YaHei UI", 9))
        style.configure("TRadiobutton", font=("Microsoft YaHei UI", 9))

    # ================================================================== 布局
    def _build_top(self):
        wrap = ttk.Frame(self.root, padding=(16, 12, 16, 0))
        wrap.pack(fill="x")
        bar = ttk.Frame(wrap)
        bar.pack(fill="x")
        ttk.Label(bar, text="源升级包",
                  font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(0, 10))
        PathField(bar, self.var_pkg,
                  purpose="原厂 LOAD 升级包（.bin），用于 1:1 结构对照与重打包。\n"
                          "选定后以包所在目录为基准，自动推导 work\\extract、"
                          "work\\build、work\\out 等路径（只填空字段）。"
                  ).pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="浏览…", width=7, command=lambda: self._pick_file(
            self.var_pkg, [("固件包", "*.bin"), ("全部文件", "*.*")])).pack(
            side="left", padx=(8, 0))
        tip = tk.Label(bar, text="?", fg="#9aa0a6", cursor="hand2",
                       font=("Microsoft YaHei UI", 9, "bold"))
        tip.pack(side="left", padx=(7, 0))
        ToolTip(tip, "原厂 LOAD 升级包（.bin）。\n"
                     "选定后按包所在目录推导 work 下的工作路径，推导只填空字段。", delay=250)

        self.step_label = tk.Label(wrap, textvariable=self.var_steps, anchor="w",
                                   bg="#f4f7fc", fg="#33475b", padx=10, pady=6,
                                   font=("Microsoft YaHei UI", 9))
        self.step_label.pack(fill="x", pady=(10, 0))

        ttk.Separator(self.root, orient="horizontal").pack(fill="x", pady=(10, 0))

    def _build_bottom(self):
        bottom = ttk.Frame(self.root, padding=(16, 0, 16, 10))
        bottom.pack(side="bottom", fill="x")

        self.result_bar = tk.Label(bottom, text="尚未执行任何操作。",
                                   anchor="w", justify="left",
                                   padx=10, pady=7, font=("Microsoft YaHei UI", 9))
        self.result_bar.pack(fill="x")
        self.result_bar.bind("<Configure>", lambda e: self.result_bar.configure(
            wraplength=max(400, e.width - 20)))
        self._set_result("idle", "尚未执行任何操作。")

        bar = ttk.Frame(bottom)
        bar.pack(fill="x", pady=(6, 6))
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=170)
        self.status_label = ttk.Label(bar, textvariable=self.var_status)
        self.status_label.pack(side="left", padx=8)
        self.progress.pack_forget()
        ttk.Button(bar, text="清空日志", command=self._clear_log).pack(side="right")
        ttk.Button(bar, text="打开输出目录", command=self._open_out_dir).pack(
            side="right", padx=(0, 6))

        self.logbox = scrolledtext.ScrolledText(bottom, height=6, wrap="none",
                                                font=("Consolas", 9),
                                                relief="flat", bd=1,
                                                highlightthickness=1,
                                                highlightbackground="#d9dde3")
        self.logbox.pack(fill="both", expand=True)
        self.logbox.configure(state="disabled")

    def _build_notebook(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=16, pady=(8, 4))
        self.nb.add(self._tab_unpack(), text="① 解包")
        self.nb.add(self._tab_debloat(), text="② 系统精简")
        self.nb.add(self._tab_root(), text="③ root / ADB / 预置 APK")
        self.nb.add(self._tab_pack(), text="④ 打包")
        self.nb.add(self._tab_verify(), text="⑤ 校验")

    def _add_button(self, parent, text, cmd, pred=None, side="left", padx=4):
        b = ttk.Button(parent, text=text, command=cmd)
        b.pack(side=side, padx=padx, pady=3)
        self._buttons.append((b, pred))
        return b

    def _page_header(self, parent, title, desc):
        """页头：加粗标题 + 灰色说明 + 分隔线，返回起始行号。"""
        head = ttk.Frame(parent)
        head.grid(row=0, column=0, columnspan=4, sticky="we", pady=(0, 12))
        ttk.Label(head, text=title,
                  font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(head, text=desc, foreground="#6b6b6b", justify="left",
                  wraplength=960).pack(anchor="w", pady=(3, 0))
        ttk.Separator(parent, orient="horizontal").grid(
            row=1, column=0, columnspan=4, sticky="we", pady=(0, 10))
        return 2

    def _help_dot(self, parent, row, tip):
        if not tip:
            return
        dot = tk.Label(parent, text="?", fg="#9aa0a6", cursor="hand2",
                       font=("Microsoft YaHei UI", 9, "bold"))
        dot.grid(row=row, column=3, padx=(7, 0))
        ToolTip(dot, tip, delay=250)

    def _file_row(self, parent, row, label, var, filetypes, save=False, tip=""):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           padx=(0, 12), pady=7)
        PathField(parent, var, purpose=tip).grid(row=row, column=1, sticky="we",
                                                 pady=7)
        ttk.Button(parent, text="浏览…", width=7,
                   command=lambda: self._pick_file(var, filetypes, save)).grid(
            row=row, column=2, padx=(8, 0), pady=7)
        self._help_dot(parent, row, tip)
        parent.columnconfigure(0, minsize=112)
        parent.columnconfigure(1, weight=1)

    def _dir_row(self, parent, row, label, var, tip=""):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           padx=(0, 12), pady=7)
        PathField(parent, var, purpose=tip).grid(row=row, column=1, sticky="we",
                                                 pady=7)
        ttk.Button(parent, text="选择…", width=7,
                   command=lambda: self._pick_dir(var)).grid(
            row=row, column=2, padx=(8, 0), pady=7)
        self._help_dot(parent, row, tip)
        parent.columnconfigure(0, minsize=112)
        parent.columnconfigure(1, weight=1)

    def _fill(self, var, value):
        """仅在字段为空时填入推导值，绝不覆盖用户已填内容。"""
        if value and not var.get().strip():
            var.set(os.path.normpath(value))

    def _on_pkg_changed(self):
        pkg = self.var_pkg.get().strip()
        if pkg and os.path.isfile(pkg):
            self._derive_from_pkg()

    def _derive_from_pkg(self):
        """用户选定源升级包后，按该文件所在目录推导默认工作路径。"""
        pkg = os.path.abspath(self.var_pkg.get().strip())
        work = os.path.join(os.path.dirname(pkg), "work")
        stem = os.path.splitext(os.path.basename(pkg))[0]
        self._fill(self.var_slim, os.path.join(work, "build", "system_slim.img"))
        self._fill(self.var_final, os.path.join(work, "build", "system_final.img"))
        self._fill(self.var_out, os.path.join(work, "out", stem + "_custom.bin"))
        self._fill(self.var_verify_img, self.var_final.get())
        self._fill(self.var_extract, os.path.join(work, "extract"))
        self._fill(self.var_raw, os.path.join(work, "extract", "system.img"))

    def _on_extract_changed(self):
        d = self.var_extract.get().strip()
        if d and os.path.isdir(d):
            self._fill(self.var_raw, os.path.join(d, "system.img"))

    def _on_slim_changed(self):
        p = self.var_slim.get().strip()
        if p and os.path.isfile(p):
            self._fill(self.var_final,
                       os.path.join(os.path.dirname(os.path.abspath(p)), "system_final.img"))

    def _apk_download_dir(self):
        """下载目录跟随用户选定的输入文件所在位置，不使用任何写死路径。"""
        for var in (self.var_slim, self.var_pkg, self.var_raw):
            p = var.get().strip()
            if p and os.path.isfile(p):
                return os.path.join(os.path.dirname(os.path.abspath(p)), "work", "apk")
        return None

    def _initial_dir(self, var=None):
        for v in (var, self.var_pkg, self.var_raw, self.var_slim, self.var_final):
            if v is None:
                continue
            p = v.get().strip()
            d = p if os.path.isdir(p) else os.path.dirname(p)
            if d and os.path.isdir(d):
                return d
        return APP_DIR

    def _pick_file(self, var, filetypes, save=False):
        cur = var.get()
        init_dir = self._initial_dir(var)
        if save:
            path = filedialog.asksaveasfilename(initialdir=init_dir,
                                                initialfile=os.path.basename(cur) or "",
                                                filetypes=filetypes)
        else:
            path = filedialog.askopenfilename(initialdir=init_dir, filetypes=filetypes)
        if path:
            var.set(path.replace("/", "\\"))

    def _pick_dir(self, var):
        path = filedialog.askdirectory(initialdir=self._initial_dir(var), mustexist=False)
        if path:
            var.set(path.replace("/", "\\"))

    # ================================================================== ① 解包
    def _tab_unpack(self):
        f = ttk.Frame(self.nb, padding=(18, 16))
        r = self._page_header(
            f, "解包",
            "读取海思 LOAD 升级包，解析 17 项分区表并导出各分区 payload；"
            "system（第 12 项）可展开为原始 ext4 镜像。")
        self._dir_row(f, r, "输出目录", self.var_extract,
                      tip="分区 payload 的导出位置。选定源升级包后自动推导为 "
                          "<包目录>\\work\\extract，也可手动更改。")
        self._file_row(f, r + 1, "system 镜像", self.var_raw,
                       [("ext4 镜像", "*.img"), ("全部文件", "*.*")], save=True,
                       tip="展开后生成的 system ext4 镜像路径。")
        f.columnconfigure(0, minsize=112)
        f.columnconfigure(1, weight=1)

        bar = ttk.Frame(f)
        bar.grid(row=r + 2, column=0, columnspan=4, sticky="w", pady=(16, 0))
        self._add_button(bar, "一键解包（导出 + 展开）", self.on_unpack_all,
                         lambda: self._is_file(self.var_pkg) and
                         bool(self.var_extract.get().strip()) and
                         bool(self.var_raw.get().strip()))
        self._add_button(bar, "仅导出分区", self.on_extract,
                         lambda: self._is_file(self.var_pkg) and
                         bool(self.var_extract.get().strip()))
        self._add_button(bar, "仅展开 system", self.on_expand_system,
                         lambda: self._is_file(self.var_pkg) and
                         bool(self.var_raw.get().strip()))
        self._add_button(bar, "查看包信息", self.on_pkg_info,
                         lambda: self._is_file(self.var_pkg))

        hint = HintBar(f, self.var_hint_unpack)
        hint.grid(row=r + 3, column=0, columnspan=4, sticky="we", pady=(12, 0))
        ttk.Label(f, text="输出目录会生成 00_bootargs.bin … 15_hwcfg.bin 以及 "
                          "_trailer.bin（表后 256B 签名段）。",
                  foreground="#7a7a7a").grid(row=r + 4, column=0, columnspan=4,
                                             sticky="w", pady=(10, 0))
        return f

    @staticmethod
    def _is_file(var):
        try:
            return os.path.isfile(var.get())
        except Exception:
            return False

    def on_pkg_info(self):
        pkg = self.var_pkg.get()

        def work():
            p = zlh_load.Package(pkg)
            for line in p.info_lines():
                self.log(line)
            self.log("")
            self.log("逐项校验 payload 长度与 CRC32/MPEG-2:")
            ok = total = 0
            for idx, bcrc, calc, st in p.verify(log=self.log):
                if st == "empty":
                    continue
                total += 1
                ok += (st == "OK")
                self.log("  [%2d] %-11s %s  存=%s 算=%s" %
                         (idx, p.entries[idx]["name"], st,
                          "%08X" % bcrc if bcrc is not None else "-",
                          "%08X" % calc if calc is not None else "-"))
            if ok != total:
                raise RuntimeError("原厂包有 %d/%d 项 CRC 校验失败，包可能不完整" %
                                   (total - ok, total))
            return "%d 项分区，%d/%d 项 CRC 通过" % (p.count, ok, total)

        self.run_task("查看包信息", work)

    def on_unpack_all(self):
        outdir = self.var_extract.get()
        if os.path.isdir(outdir) and os.listdir(outdir):
            if not messagebox.askyesno(
                    "确认覆盖",
                    "输出目录已存在文件：\n%s\n\n继续解包会覆盖同名文件，是否继续？" % outdir):
                return
        self._run_unpack(export=True, expand=True)

    def on_extract(self):
        self._run_unpack(export=True, expand=False)

    def on_expand_system(self):
        self._run_unpack(export=False, expand=True)

    def _run_unpack(self, export, expand):
        pkg = self.var_pkg.get()
        outdir = self.var_extract.get()
        raw = self.var_raw.get()

        def work():
            p = zlh_load.Package(pkg)
            exported = 0
            if export:
                self.log("导出分区 -> %s" % outdir)
                exported = len(p.extract(outdir, log=self.log))
                self.log("共导出 %d 个 payload 文件" % exported)
            if expand:
                self.log("展开 system（第 %d 项）-> %s" % (zlh_load.SYSTEM_IDX, raw))
                n = sparse.expand_from_package(pkg, zlh_load.SYSTEM_IDX, raw, log=self.log)
                self.log("system ext4 镜像: %.1f MB" % (n / 1048576.0))
                e = Ext4(raw, readonly=True)
                try:
                    self.log("文件系统: %d 块 x %d B, %d 组, 空闲块 %d, 空闲 inode %d" %
                             (e.blocks_count, e.bs, e.groups, e.free_blocks, e.free_inodes))
                    ok, tot = e.verify_gdt()
                finally:
                    e.close()
                self.log("gdt_csum 校验: %d/%d" % (ok, tot))
                if ok != tot:
                    raise RuntimeError("gdt_csum 校验失败（%d/%d），请检查固件" % (ok, tot))
            parts = []
            if export:
                parts.append("导出 %d 个分区" % exported)
            if expand:
                parts.append("system 镜像 %s" % human(os.path.getsize(raw)))
            return "，".join(parts)

        self.run_task("解包", work)

    # ================================================================== ② 精简
    def _tab_debloat(self):
        f = ttk.Frame(self.nb, padding=(18, 16))
        f.columnconfigure(0, weight=1)
        f.rowconfigure(4, weight=1)
        self._page_header(
            f, "系统精简",
            "勾选 /app 下的应用：把对应 *.apk 改名为 *.apx 并清零数据，"
            "PackageManager 不再扫描；默认不预选任何应用。")
        form = ttk.Frame(f)
        form.grid(row=2, column=0, sticky="we")
        self._file_row(form, 0, "输入镜像", self.var_raw,
                       [("ext4 镜像", "*.img"), ("全部文件", "*.*")],
                       tip="要精简的 ext4 镜像，通常是解包得到的 system.img。")
        self._file_row(form, 1, "输出镜像", self.var_slim,
                       [("ext4 镜像", "*.img"), ("全部文件", "*.*")], save=True,
                       tip="精简后的镜像保存位置，默认与输入镜像同目录的 system_slim.img。")

        bar = ttk.Frame(f)
        bar.grid(row=3, column=0, sticky="we", pady=(12, 8))
        self._add_button(bar, "扫描镜像应用", self.on_scan_apps,
                         lambda: self._is_file(self.var_raw))
        self._add_button(bar, "全选", self._select_all, lambda: bool(self._apps))
        self._add_button(bar, "反选", self._invert_selection, lambda: bool(self._apps))
        self._add_button(bar, "清空选择", self._clear_selection,
                         lambda: bool(self._app_sel))
        ttk.Label(bar, text="筛选:").pack(side="left", padx=(14, 4))
        ttk.Entry(bar, textvariable=self.var_search, width=24).pack(side="left")

        tree_frame = ttk.Frame(f)
        tree_frame.grid(row=4, column=0, sticky="nsew")
        style = ttk.Style()
        style.configure("App.Treeview", rowheight=46)
        self.debloat_tree = ttk.Treeview(
            tree_frame, columns=("apk", "size"), show="tree headings",
            style="App.Treeview", selectmode="none")
        self.debloat_tree.heading("#0", text="应用目录（点击整行勾选 / 取消）")
        self.debloat_tree.heading("apk", text="APK")
        self.debloat_tree.heading("size", text="大小")
        self.debloat_tree.column("#0", width=560)
        self.debloat_tree.column("apk", width=70, anchor="center", stretch=False)
        self.debloat_tree.column("size", width=110, anchor="e", stretch=False)
        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self.debloat_tree.yview)
        self.debloat_tree.configure(yscrollcommand=vsb.set)
        self.debloat_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.debloat_tree.bind("<Button-1>", self._on_tree_click)
        self.debloat_tree.bind("<space>", self._on_tree_space)

        info = ttk.Frame(f)
        info.grid(row=5, column=0, sticky="we", pady=(6, 0))
        ttk.Label(info, textvariable=self.var_apps_info, foreground="#555").pack(side="left")
        ttk.Label(info, textvariable=self.var_sel_info,
                  font=("Microsoft YaHei UI", 9, "bold")).pack(side="right")

        hint = HintBar(f, self.var_hint_debloat)
        hint.grid(row=6, column=0, sticky="we", pady=(8, 0))

        bottom = ttk.Frame(f)
        bottom.grid(row=7, column=0, sticky="we", pady=(10, 0))
        ttk.Checkbutton(bottom, text="精简后释放 .apx 占用的块"
                                     "（为 /priv-app 预置腾出可安全分配的空间）",
                        variable=self.var_release).pack(side="left")
        ttk.Label(bottom, text="  手动补充（空格/逗号分隔，可留空）:").pack(side="left")
        ttk.Entry(bottom, textvariable=self.var_manual, width=32).pack(side="left", padx=4)
        self._add_button(bottom, "执行精简", self.on_debloat,
                         lambda: self._is_file(self.var_raw) and
                         bool(self.var_slim.get().strip()) and
                         self._debloat_has_input(),
                         side="right")
        return f

    def _debloat_has_input(self):
        if self._app_sel:
            return True
        return bool(self._manual_names())

    def _manual_names(self):
        raw = self.var_manual.get().strip()
        return [x for x in re.split(r"[,;\s]+", raw) if x] if raw else []

    def _collect_debloat_names(self):
        names = sorted(self._app_sel)
        for n in self._manual_names():
            if n not in names:
                names.append(n)
        return names

    def _on_raw_changed(self):
        img = self.var_raw.get().strip()
        if img and os.path.isfile(img):
            self._fill(self.var_slim, os.path.join(
                os.path.dirname(os.path.abspath(img)), "system_slim.img"))
        if self._scanned_img and img != self._scanned_img and self._apps:
            self._apps.clear()
            self._app_sel.clear()
            self._icons.clear()
            self._scanned_img = None
            self._refresh_debloat_tree()
            self.var_apps_info.set("镜像已更改，请重新点击【扫描镜像应用】。")
            self.var_sel_info.set("已选 0 个应用")

    def on_scan_apps(self):
        img = self.var_raw.get()

        def work():
            if not os.path.isfile(img):
                raise FileNotFoundError(img)
            e = Ext4(img, readonly=True)
            try:
                self.log("扫描 %s 的 /app 目录 …" % img)
                apps = system_edit.scan_apps(e, log=self.log,
                                             icon_cache=self._icon_cache)
            finally:
                e.close()
            self.root.after(0, lambda: self._apply_scan(img, apps))
            if not apps:
                raise RuntimeError("镜像 /app 下没有找到含 APK 的应用目录")
            return "共 %d 个含 APK 的应用，请在列表中勾选" % len(apps)

        self.run_task("扫描应用", work)

    def _apply_scan(self, img, apps):
        self._scanned_img = img
        self._apps = {a["name"]: a for a in apps}
        self._app_sel.clear()
        self._icons.clear()
        for name, a in self._apps.items():
            ph = self._photo(a.get("icon"))
            if ph is not None:
                self._icons[name] = ph
        self._refresh_debloat_tree()
        self._update_sel_label()
        with_icon = sum(1 for a in apps if a.get("icon"))
        self.var_apps_info.set("共 %d 个应用（%d 个成功读取图标）；点击行即可勾选。" %
                               (len(apps), with_icon))

    def _photo(self, png):
        if not png:
            return None
        try:
            ph = tk.PhotoImage(data=base64.b64encode(png))
        except Exception:
            return None
        w = ph.width()
        if w > 44:
            factor = max(1, int(round(w / 40.0)))
            ph = ph.subsample(factor, factor)
        return ph

    def _mark(self, name):
        return ("☑ " if name in self._app_sel else "☐ ") + name

    def _refresh_debloat_tree(self):
        tree = self.debloat_tree
        tree.delete(*tree.get_children())
        kw = self.var_search.get().strip().lower()
        for name in sorted(self._apps, key=str.lower):
            a = self._apps[name]
            if kw and kw not in name.lower() and \
                    not any(kw in n.lower() for n, _s in a["apks"]):
                continue
            tree.insert("", "end", iid=name, image=self._icons.get(name, ""),
                        text=self._mark(name),
                        values=(len(a["apks"]), human(a["size"])))

    def _update_sel_label(self):
        n = len(self._app_sel)
        extra = len(self._manual_names())
        txt = "已选 %d 个应用" % n
        if extra:
            txt += "（另有手动输入 %d 个）" % extra
        self.var_sel_info.set(txt)

    def _on_tree_click(self, evt):
        if self.busy:
            return
        iid = self.debloat_tree.identify_row(evt.y)
        region = self.debloat_tree.identify("region", evt.x, evt.y)
        if iid and region in ("tree", "cell", "image"):
            self._toggle_app(iid)

    def _on_tree_space(self, _evt):
        if self.busy:
            return
        for iid in self._visible_iids():
            if iid in self._app_sel:
                self._toggle_app(iid)
                break

    def _visible_iids(self):
        return self.debloat_tree.get_children()

    def _toggle_app(self, name):
        if name in self._app_sel:
            self._app_sel.discard(name)
        else:
            self._app_sel.add(name)
        self.debloat_tree.item(name, text=self._mark(name))
        self._update_sel_label()

    def _select_all(self):
        for iid in self._visible_iids():
            self._app_sel.add(iid)
            self.debloat_tree.item(iid, text=self._mark(iid))
        self._update_sel_label()

    def _invert_selection(self):
        for iid in self._visible_iids():
            if iid in self._app_sel:
                self._app_sel.discard(iid)
            else:
                self._app_sel.add(iid)
            self.debloat_tree.item(iid, text=self._mark(iid))
        self._update_sel_label()

    def _clear_selection(self):
        self._app_sel.clear()
        for iid in self._visible_iids():
            self.debloat_tree.item(iid, text=self._mark(iid))
        self._update_sel_label()

    def on_debloat(self):
        src = self.var_raw.get()
        dst = self.var_slim.get()
        names = self._collect_debloat_names()
        if not names:
            messagebox.showwarning("未选择应用", "请先勾选要精简的应用（或手动输入目录名）。")
            return
        if os.path.abspath(src) == os.path.abspath(dst):
            messagebox.showerror("路径错误", "输入与输出镜像不能是同一个文件。")
            return
        preview = "\n".join("  " + n for n in names[:12])
        if len(names) > 12:
            preview += "\n  …（共 %d 个）" % len(names)
        msg = ("即将精简以下 %d 个应用：\n%s\n\n"
               "会把 .apk 改名为 .apx 并清零数据%s，此操作不可撤销。\n"
               "输出镜像: %s" %
               (len(names), preview,
                "并释放块" if self.var_release.get() else "",
                dst))
        if os.path.exists(dst):
            msg += "\n\n输出镜像已存在，将被覆盖。"
        if not messagebox.askyesno("确认精简", msg):
            return
        release = self.var_release.get()

        def work():
            system_edit.copy_image(src, dst, log=self.log)
            e = system_edit.open_image(dst, log=self.log)
            try:
                ok, tot = e.verify_gdt()
                self.log("编辑前 gdt_csum: %d/%d" % (ok, tot))
                if ok != tot:
                    raise RuntimeError("gdt_csum 算法不匹配，已拒绝编辑")
                files, nbytes, released = system_edit.debloat(
                    e, names, release=release, log=self.log)
            finally:
                e.close()
            if files == 0:
                raise RuntimeError("没有找到任何可精简的 APK，请确认所选目录")
            v = Ext4(dst, readonly=True)
            try:
                ok, tot = v.verify_gdt()
            finally:
                v.close()
            self.log("编辑后 gdt_csum: %d/%d" % (ok, tot))
            if ok != tot:
                raise RuntimeError("编辑后 gdt_csum 校验失败")
            self.log("镜像已写回: %s" % dst)
            txt = "精简 %d 个 APK，清零 %.1f MB" % (files, nbytes / 1048576.0)
            if release:
                txt += "，释放 %.1f MB" % (released / 1048576.0)
            return txt + " → " + dst

        self.run_task("系统精简", work)

    # ================================================================== ③ root / ADB / 预置
    def _tab_root(self):
        f = ttk.Frame(self.nb, padding=(18, 16))
        f.columnconfigure(0, weight=1)
        f.rowconfigure(4, weight=1)
        self._page_header(
            f, "root / ADB / 预置 APK",
            "改写 build.prop 的 ADB 属性并在 /etc/init.bigfish.sh 注入 setprop，"
            "可同时预置一个或多个 APK（本地文件或下载链接）。")
        form = ttk.Frame(f)
        form.grid(row=2, column=0, sticky="we")
        self._file_row(form, 0, "输入镜像", self.var_slim,
                       [("ext4 镜像", "*.img"), ("全部文件", "*.*")],
                       tip="精简后的 system 镜像（上一步的输出）。")
        self._file_row(form, 1, "输出镜像", self.var_final,
                       [("ext4 镜像", "*.img"), ("全部文件", "*.*")], save=True,
                       tip="root / 预置后的镜像保存位置，默认与输入同目录的 system_final.img。")

        box = ttk.LabelFrame(f, text="系统选项", padding=(10, 6))
        box.grid(row=3, column=0, sticky="we", pady=(12, 6))
        cb1 = ttk.Checkbutton(box, text="改写 build.prop 的 ADB 属性",
                              variable=self.var_adb)
        cb1.pack(side="left")
        ToolTip(cb1, "persist.sys.usb.config=adb\nro.adb.secure=0\n"
                     "persist.service.adb.enable=1", delay=250)
        cb2 = ttk.Checkbutton(box, text="注入 init.bigfish.sh setprop",
                              variable=self.var_init)
        cb2.pack(side="left", padx=(24, 0))
        ToolTip(cb2, "在 /etc/init.bigfish.sh 头部注入 setprop，每次开机强制生效。",
                delay=250)
        cb3 = ttk.Checkbutton(box, text="启动 telnetd root shell（2323）",
                              variable=self.var_telnetd)
        cb3.pack(side="left", padx=(24, 0))
        ToolTip(cb3, "该机型固件没有 adbd，telnetd 是唯一可用的 root 通道。",
                delay=250)

        apkbox = ttk.LabelFrame(f, text="预置 APK（可添加多个）", padding=(10, 8))
        apkbox.grid(row=4, column=0, sticky="nsew", pady=(0, 6))
        apkbox.columnconfigure(0, weight=1)
        apkbox.rowconfigure(1, weight=1)
        abar = ttk.Frame(apkbox)
        abar.grid(row=0, column=0, sticky="we")
        self._add_button(abar, "添加本地 APK…", self.on_add_local_apk)
        dl = self._add_button(abar, "从链接下载…", self.on_add_url_apk)
        ToolTip(dl, "每行一个 http/https 直链；执行时下载到输入镜像同级的 work\\apk 再预置。",
                delay=250)
        self._add_button(abar, "移除选中", self.on_remove_apk,
                         lambda: bool(self._apk_items))
        self._add_button(abar, "清空", self.on_clear_apk,
                         lambda: bool(self._apk_items))

        atree = ttk.Frame(apkbox)
        atree.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self.apk_tree = ttk.Treeview(atree, columns=("src", "size", "status"),
                                     show="headings", height=5)
        self.apk_tree.heading("src", text="来源")
        self.apk_tree.heading("size", text="大小")
        self.apk_tree.heading("status", text="状态")
        self.apk_tree.column("#0", width=320)
        self.apk_tree.heading("#0", text="文件")
        self.apk_tree.column("src", width=100, anchor="center", stretch=False)
        self.apk_tree.column("size", width=100, anchor="e", stretch=False)
        self.apk_tree.column("status", width=160, anchor="center", stretch=False)
        asb = ttk.Scrollbar(atree, orient="vertical", command=self.apk_tree.yview)
        self.apk_tree.configure(yscrollcommand=asb.set)
        self.apk_tree.pack(side="left", fill="both", expand=True)
        asb.pack(side="right", fill="y")

        modebox = ttk.Frame(apkbox)
        modebox.grid(row=2, column=0, sticky="we", pady=(6, 0))
        rb1 = ttk.Radiobutton(modebox, text="复制到 /priv-app（可多个）", value="privapp",
                              variable=self.var_mode)
        rb1.pack(side="left")
        ToolTip(rb1, "只允许分配原厂 sparse 中 RAW 块覆盖的空闲块，否则刷机会被清零；"
                     "需在第 ② 步勾选「释放 .apx 占用的块」，空间不足会直接报错。", delay=250)
        ttk.Radiobutton(modebox, text="复用应用商店 APK 槽位（仅 1 个，无需新块）",
                        value="slot", variable=self.var_mode).pack(side="left", padx=16)
        self.var_slot_hint = tk.StringVar(value="")
        ttk.Label(modebox, textvariable=self.var_slot_hint,
                  foreground="#b3261e").pack(side="left", padx=8)

        hint = HintBar(f, self.var_hint_root)
        hint.grid(row=5, column=0, sticky="we", pady=(8, 0))

        bar = ttk.Frame(f)
        bar.grid(row=6, column=0, sticky="we", pady=(10, 0))
        self._add_button(bar, "执行 root / 预置", self.on_root_apply,
                         self._root_ready)
        return f

    def _root_ready(self):
        if not self._is_file(self.var_slim):
            return False
        if not self.var_final.get().strip():
            return False
        if not (self.var_adb.get() or self.var_init.get() or self._apk_items):
            return False
        if self.var_mode.get() == "slot" and len(self._apk_items) > 1:
            return False
        return True

    def _apk_add_local(self, path):
        size = os.path.getsize(path)
        self._apk_add_item(path=path, name=os.path.basename(path),
                           source="本地文件", size=size, status="就绪")

    def _apk_add_item(self, path=None, url=None, name="", source="", size=None,
                      status=""):
        iid = "apk%d" % self._apk_seq
        self._apk_seq += 1
        self._apk_items[iid] = dict(iid=iid, path=path, url=url, name=name,
                                    source=source, size=size, status=status)
        self.apk_tree.insert("", "end", iid=iid, text=name,
                             values=(source, human(size), status))
        return iid

    def on_add_local_apk(self):
        cur = self.apk_tree.get_children()
        init_dir = self._apk_download_dir() or self._initial_dir()
        paths = filedialog.askopenfilenames(
            title="选择一个或多个 APK", initialdir=init_dir,
            filetypes=[("APK", "*.apk"), ("全部文件", "*.*")])
        for p in paths:
            try:
                self._apk_add_local(p)
                self.log("添加本地 APK: %s (%s)" % (p, human(os.path.getsize(p))))
            except OSError as ex:
                messagebox.showerror("添加失败", "%s\n%s" % (p, ex))
        if paths and not cur:
            self.log("提示：可在【复用槽位】模式下只放 1 个 APK；多个 APK 请用 /priv-app 模式。")

    def on_add_url_apk(self):
        urls = self._ask_urls()
        for u in urls:
            base = os.path.basename(u.split("?")[0]) or "远程 APK"
            if not base.lower().endswith(".apk"):
                base += ".apk"
            self._apk_add_item(url=u, name=base, source="下载链接",
                               status="待下载")
            self.log("添加下载链接: %s" % u)

    def _ask_urls(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("从链接下载 APK")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(True, True)
        ttk.Label(dlg, text="每行一个 APK 直链（http/https），执行时会先下载再预置。",
                  padding=8).pack(anchor="w")
        txt = scrolledtext.ScrolledText(dlg, width=86, height=8, font=("Consolas", 9))
        txt.pack(fill="both", expand=True, padx=8)
        txt.focus_set()
        result = {"urls": []}

        def ok():
            lines = [x.strip() for x in txt.get("1.0", "end").splitlines() if x.strip()]
            good, bad = [], []
            for u in lines:
                (good if re.match(r"^https?://", u, re.I) else bad).append(u)
            if bad:
                messagebox.showwarning(
                    "已忽略无效链接",
                    "以下链接不是 http/https，已忽略：\n" + "\n".join(bad[:6]),
                    parent=dlg)
            result["urls"] = good
            dlg.destroy()

        def cancel():
            dlg.destroy()

        bf = ttk.Frame(dlg, padding=8)
        bf.pack(fill="x")
        ttk.Button(bf, text="取消", command=cancel).pack(side="right")
        ttk.Button(bf, text="添加", command=ok).pack(side="right", padx=6)
        txt.insert("1.0", "")
        self.root.wait_window(dlg)
        return result["urls"]

    def on_remove_apk(self):
        sel = self.apk_tree.selection()
        if not sel:
            messagebox.showinfo("未选中", "请先在列表中选择要移除的条目。")
            return
        for iid in sel:
            self.apk_tree.delete(iid)
            self._apk_items.pop(iid, None)

    def on_clear_apk(self):
        for iid in list(self._apk_items):
            self.apk_tree.delete(iid)
        self._apk_items.clear()

    def _apk_update(self, iid, **kw):
        def apply():
            it = self._apk_items.get(iid)
            if not it:
                return
            it.update(kw)
            if self.apk_tree.exists(iid):
                self.apk_tree.item(iid, text=it.get("name", ""),
                                   values=(it.get("source", ""),
                                           human(it.get("size")),
                                           it.get("status", "")))
            self._refresh_buttons()
        self.root.after(0, apply)

    def on_root_apply(self):
        src = self.var_slim.get()
        dst = self.var_final.get()
        stock = self.var_pkg.get()
        do_adb = self.var_adb.get()
        do_init = self.var_init.get()
        telnetd = self.var_telnetd.get()
        mode = self.var_mode.get()
        items = [dict(v) for v in self._apk_items.values()]

        if not (do_adb or do_init or items):
            messagebox.showwarning("没有操作", "请至少勾选一个系统选项或添加一个 APK。")
            return
        if not os.path.isfile(src):
            messagebox.showerror("找不到镜像", "输入镜像不存在：\n%s" % src)
            return
        if not dst.strip():
            messagebox.showerror("缺少路径", "请先指定输出 ext4 镜像的保存位置。")
            return
        if os.path.abspath(src) == os.path.abspath(dst):
            messagebox.showerror("路径错误", "输入与输出镜像不能是同一个文件。")
            return
        apkdir = self._apk_download_dir()
        if any(it.get("url") for it in items) and not apkdir:
            messagebox.showerror(
                "无法确定下载目录",
                "链接 APK 需要下载到本地。请先选择输入镜像或源升级包，"
                "以便在它们同级创建 work\\apk 目录。")
            return
        if mode == "slot" and len(items) > 1:
            messagebox.showerror(
                "槽位模式限制",
                "复用应用商店槽位一次只能预置 1 个 APK。\n"
                "请改用【复制到 /priv-app】模式，或只保留 1 个 APK。")
            return
        if mode == "privapp" and items and not os.path.isfile(stock):
            messagebox.showerror(
                "需要原厂包",
                "预置到 /priv-app 需要读取原厂 sparse 的 RAW 区间来确定安全分配位置，\n"
                "请先在顶部选择源升级包。")
            return
        if os.path.exists(dst):
            if not messagebox.askyesno("确认覆盖", "输出镜像已存在，是否覆盖？\n%s" % dst):
                return

        lines = []
        if do_adb:
            lines.append("· 改写 build.prop（ADB 属性）")
        if do_init:
            lines.append("· 注入 init.bigfish.sh%s" %
                         ("（含 telnetd root shell）" if telnetd else ""))
        if items:
            lines.append("· 预置 %d 个 APK → %s" %
                         (len(items), "/priv-app" if mode == "privapp" else "应用商店槽位"))
        if not messagebox.askyesno(
                "确认执行",
                "将执行以下操作：\n%s\n\n输入: %s\n输出: %s\n\n继续？" %
                ("\n".join(lines), src, dst)):
            return

        def work():
            local = []
            if items:
                os.makedirs(apkdir, exist_ok=True)
            for it in items:
                if it.get("url"):
                    self._apk_update(it["iid"], status="下载中…")
                    try:
                        p = downloader.download_apk(it["url"], apkdir, log=self.log)
                    except Exception:
                        self._apk_update(it["iid"], status="下载失败")
                        raise
                    self._apk_update(it["iid"], path=p, name=os.path.basename(p),
                                     size=os.path.getsize(p), status="下载完成")
                else:
                    p = it["path"]
                    if not p or not os.path.isfile(p):
                        self._apk_update(it["iid"], status="文件不存在")
                        raise FileNotFoundError(p or "(空路径)")
                    try:
                        downloader.validate_apk(p)
                    except Exception:
                        self._apk_update(it["iid"], status="不是有效 APK")
                        raise
                    self._apk_update(it["iid"], status="校验通过")
                local.append(p)

            layout = None
            if local and mode == "privapp":
                layout = sparse.layout_from_package(stock, zlh_load.SYSTEM_IDX)
                self.log("原厂 system sparse: " + sparse.summarize_layout(layout))

            system_edit.copy_image(src, dst, log=self.log)
            e = system_edit.open_image(dst, layout=layout, log=self.log)
            try:
                ok, tot = e.verify_gdt()
                self.log("编辑前 gdt_csum: %d/%d" % (ok, tot))
                if ok != tot:
                    raise RuntimeError("gdt_csum 算法不匹配，已拒绝编辑")
                if do_adb:
                    self.log("--- build.prop ---")
                    system_edit.set_adb_props(e, log=self.log)
                if do_init:
                    self.log("--- init.bigfish.sh ---")
                    system_edit.inject_init(e, log=self.log, telnetd=telnetd)
                for p in local:
                    self.log("--- 预置 APK: %s ---" % os.path.basename(p))
                    path = system_edit.preset_apk(e, p, mode=mode, log=self.log)
                    self.log("  目标路径: %s" % path)
            finally:
                e.close()

            v = Ext4(dst, readonly=True)
            try:
                ok, tot = v.verify_gdt()
            finally:
                v.close()
            self.log("编辑后 gdt_csum: %d/%d" % (ok, tot))
            if ok != tot:
                raise RuntimeError("编辑后 gdt_csum 校验失败")
            for it in items:
                self._apk_update(it["iid"], status="已预置")
            self.log("镜像已写回: %s" % dst)
            parts = []
            if do_adb:
                parts.append("ADB 属性")
            if do_init:
                parts.append("init 注入")
            if local:
                parts.append("预置 %d 个 APK" % len(local))
            return "、".join(parts) + " → " + dst

        self.run_task("root / ADB / 预置", work)

    # ================================================================== ④ 打包
    def _tab_pack(self):
        f = ttk.Frame(self.nb, padding=(18, 16))
        r = self._page_header(
            f, "打包",
            "稀疏重组 + LOAD 重打包：头部、17 项分区表、256B 签名段与其余分区逐字节照抄，"
            "只替换 system payload 并重算块 CRC，输出长度与原厂包一致。")
        self._file_row(f, r, "改造后镜像", self.var_final,
                       [("ext4 镜像", "*.img"), ("全部文件", "*.*")],
                       tip="最终要打进包里的 system ext4 镜像（root / 预置步骤的输出）。")
        self._file_row(f, r + 1, "输出固件包", self.var_out,
                       [("固件包", "*.bin"), ("全部文件", "*.*")], save=True,
                       tip="改造后的固件包路径，默认推导为 <包目录>\\work\\out\\<包名>_custom.bin。")
        bar = ttk.Frame(f)
        bar.grid(row=r + 2, column=0, columnspan=4, sticky="w", pady=(16, 0))
        self._add_button(bar, "执行打包", self.on_pack,
                         lambda: self._is_file(self.var_final) and
                         self._is_file(self.var_pkg) and
                         bool(self.var_out.get().strip()))
        hint = HintBar(f, self.var_hint_pack)
        hint.grid(row=r + 3, column=0, columnspan=4, sticky="we", pady=(12, 0))
        ttk.Label(f, text="按原厂 sparse chunk 布局 1:1 重组 system payload → 校验结构 → "
                          "重打包 LOAD。中间文件保存在 system_final 同目录。",
                  foreground="#7a7a7a").grid(row=r + 4, column=0, columnspan=4,
                                             sticky="w", pady=(10, 0))
        return f

    def on_pack(self):
        stock = self.var_pkg.get()
        img = self.var_final.get()
        out = self.var_out.get().strip()
        if not out:
            messagebox.showerror("缺少路径", "请先指定输出固件包的保存位置。")
            return
        if os.path.exists(out):
            if not messagebox.askyesno(
                    "确认覆盖",
                    "输出固件包已存在，是否覆盖？\n%s" % out):
                return

        def work():
            if not os.path.isfile(img):
                raise FileNotFoundError(img)
            layout = sparse.layout_from_package(stock, zlh_load.SYSTEM_IDX)
            self.log("原厂 system payload 布局: " + sparse.summarize_layout(layout))
            new_sparse = os.path.join(os.path.dirname(os.path.abspath(img)),
                                      "system_payload.sparse")
            self.log("--- 1/3 sparse 重组 ---")
            sparse.mirror_rebuild(img, layout, new_sparse, log=self.log)
            self.log("--- 2/3 结构校验 ---")
            sparse.check_mirror(new_sparse, layout, log=self.log)
            self.log("--- 3/3 LOAD 重打包 ---")
            repack.repack(stock, new_sparse, out, log=self.log)
            return "输出 %s，与原厂包长度一致" % human(os.path.getsize(out))

        self.run_task("打包固件", work)

    # ================================================================== ⑤ 校验
    def _tab_verify(self):
        f = ttk.Frame(self.nb, padding=(18, 16))
        r = self._page_header(
            f, "校验",
            "对改造包做全面校验：包大小 / 头部 / 分区表一致性、各块长度与 CRC32/MPEG-2、"
            "sparse chunk 结构、从新包反解 system 的 SHA256 比对。")
        self._file_row(f, r, "改造后固件包", self.var_out,
                       [("固件包", "*.bin"), ("全部文件", "*.*")],
                       tip="要校验的改造固件包。")
        self._file_row(f, r + 1, "对照镜像", self.var_verify_img,
                       [("ext4 镜像", "*.img"), ("全部文件", "*.*")],
                       tip="可选。填写后会与从新包反解出的 system 做 SHA256 比对。")
        bar = ttk.Frame(f)
        bar.grid(row=r + 2, column=0, columnspan=4, sticky="w", pady=(16, 0))
        self._add_button(bar, "开始校验", self.on_verify,
                         lambda: self._is_file(self.var_out) and
                         self._is_file(self.var_pkg))
        hint = HintBar(f, self.var_hint_verify)
        hint.grid(row=r + 3, column=0, columnspan=4, sticky="we", pady=(12, 0))
        ttk.Label(f, text="对照镜像可留空（此时只输出反解 system 的 SHA256）。"
                          "校验不通过时，红色结果栏给出首要原因，日志里列出逐项问题。",
                  foreground="#7a7a7a").grid(row=r + 4, column=0, columnspan=4,
                                             sticky="w", pady=(10, 0))
        return f

    def on_verify(self):
        stock = self.var_pkg.get()
        custom = self.var_out.get()
        img = self.var_verify_img.get().strip() or None

        def work():
            ok = verify.verify_package(stock, custom, zlh_load.SYSTEM_IDX,
                                       raw_image=img, log=self.log)
            if not ok:
                raise RuntimeError("校验未通过（问题清单见日志）")
            return "包结构 1:1、全部 payload CRC、chunk 序列与 SHA256 均通过"

        self.run_task("校验固件包", work)

    # ================================================================== 状态与任务
    def log(self, msg):
        self.log_queue.put(str(msg))

    def _drain_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.logbox.configure(state="normal")
                self.logbox.insert("end", "[%s] %s\n" %
                                   (time.strftime("%H:%M:%S"), msg))
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def _clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    def _open_out_dir(self):
        out = self.var_out.get().strip()
        if not out:
            messagebox.showinfo("未设置路径", "尚未指定输出固件包路径。")
            return
        path = os.path.dirname(os.path.abspath(out))
        if not os.path.isdir(path):
            messagebox.showerror("目录不存在", "输出目录还不存在：\n%s" % path)
            return
        try:
            os.startfile(path)
        except Exception as ex:
            messagebox.showerror("无法打开目录", str(ex))

    def _set_result(self, kind, text):
        bg, fg = RESULT_STYLES.get(kind, RESULT_STYLES["idle"])
        self.result_bar.configure(text=text, bg=bg, fg=fg)

    def _step_text(self):
        items = [
            ("① 解包", self._is_file(self.var_raw)),
            ("② 精简", self._is_file(self.var_slim)),
            ("③ root/预置", self._is_file(self.var_final)),
            ("④ 打包", self._is_file(self.var_out)),
        ]
        parts = ["%s %s" % (name, "✓" if done else "○") for name, done in items]
        parts.append("⑤ 校验 —")
        return "流程   " + "     ".join(parts)

    def _tick(self):
        self.var_steps.set(self._step_text())
        self._refresh_buttons()
        self._refresh_hints()
        self.root.after(500, self._tick)

    def _refresh_hints(self):
        pkg_ok = self._is_file(self.var_pkg)
        raw_ok = self._is_file(self.var_raw)
        slim_ok = self._is_file(self.var_slim)

        if not self.var_pkg.get().strip():
            self.var_hint_unpack.set("尚未选择源升级包（窗口顶部）。")
        elif not pkg_ok:
            self.var_hint_unpack.set("源升级包不存在，请重新选择。")
        elif not self.var_extract.get().strip():
            self.var_hint_unpack.set("缺少输出目录。")
        elif not self.var_raw.get().strip():
            self.var_hint_unpack.set("缺少 system 镜像路径。")
        else:
            self.var_hint_unpack.set("")

        if not self.var_raw.get().strip():
            self.var_hint_debloat.set("尚未选择输入镜像（例如解包得到的 system.img）。")
        elif not raw_ok:
            self.var_hint_debloat.set("输入镜像不存在。")
        elif not self.var_slim.get().strip():
            self.var_hint_debloat.set("缺少输出镜像路径。")
        elif not self._debloat_has_input():
            self.var_hint_debloat.set("尚未勾选任何应用（也可在下方手动补充）。")
        else:
            self.var_hint_debloat.set("")

        if not self.var_slim.get().strip():
            self.var_hint_root.set("尚未选择输入镜像（精简后的 system.img）。")
        elif not slim_ok:
            self.var_hint_root.set("输入镜像不存在。")
        elif not self.var_final.get().strip():
            self.var_hint_root.set("缺少输出镜像路径。")
        elif not (self.var_adb.get() or self.var_init.get() or self._apk_items):
            self.var_hint_root.set("尚未勾选系统选项，也未添加 APK。")
        else:
            self.var_hint_root.set("")

        if not self.var_final.get().strip():
            self.var_hint_pack.set("尚未选择改造后的镜像。")
        elif not self._is_file(self.var_final):
            self.var_hint_pack.set("改造后的镜像不存在，请先完成前面步骤。")
        elif not self.var_pkg.get().strip():
            self.var_hint_pack.set("尚未选择源升级包（窗口顶部）。")
        elif not self.var_out.get().strip():
            self.var_hint_pack.set("缺少输出固件包路径。")
        else:
            self.var_hint_pack.set("")

        if not self.var_out.get().strip():
            self.var_hint_verify.set("尚未选择要校验的固件包。")
        elif not self._is_file(self.var_out):
            self.var_hint_verify.set("固件包不存在，请先执行打包。")
        elif not self.var_pkg.get().strip():
            self.var_hint_verify.set("尚未选择源升级包（结构对照用）。")
        else:
            self.var_hint_verify.set("")

    def _refresh_buttons(self):
        for btn, pred in self._buttons:
            try:
                enabled = (not self.busy) and (pred is None or bool(pred()))
            except Exception:
                enabled = False
            try:
                btn.state(["!disabled"] if enabled else ["disabled"])
            except tk.TclError:
                pass
        if self.var_mode.get() == "slot" and len(self._apk_items) > 1:
            self.var_slot_hint.set("槽位模式仅支持 1 个 APK（当前 %d 个），请改用 /priv-app" %
                                   len(self._apk_items))
        else:
            self.var_slot_hint.set("")

    def run_task(self, title, fn):
        if self.busy:
            messagebox.showwarning("请稍候", "已有任务在运行，请等待完成。")
            return
        self.busy = True
        self.var_status.set("执行中：%s" % title)
        self._set_result("info", "正在执行：%s …" % title)
        self._refresh_buttons()
        self.progress.pack(side="left", before=self.status_label)
        self.progress.start(12)
        self.log("========== %s 开始 ==========" % title)
        t0 = time.time()

        def runner():
            ok, summary, err = True, "", ""
            try:
                summary = str(fn() or "")
            except Exception as ex:
                ok = False
                err = str(ex) or type(ex).__name__
                self.log("错误: %s" % err)
                self.log(traceback.format_exc())
            self.root.after(0, lambda: self._task_done(
                title, ok, summary, err, time.time() - t0))

        threading.Thread(target=runner, daemon=True).start()

    def _task_done(self, title, ok, summary, err, elapsed):
        self.progress.stop()
        self.progress.pack_forget()
        self.busy = False
        self._refresh_buttons()
        if ok:
            msg = "✓ %s 完成" % title
            if summary:
                msg += "：" + summary
            msg += "（%.1f 秒）" % elapsed
            self._set_result("ok", msg)
            self.var_status.set("就绪")
            self.log("========== %s 完成 ========== (%.1f 秒)" % (title, elapsed))
        else:
            msg = "✗ %s 失败：%s（完整堆栈见下方日志）" % (title, err or "未知错误")
            self._set_result("fail", msg)
            self.var_status.set("失败")
            self.log("========== %s 失败 ==========" % title)
            messagebox.showerror("%s 失败" % title, err or "未知错误")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
