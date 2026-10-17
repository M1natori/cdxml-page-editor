#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cdxml_pages_gui.py — ChemDraw (CDXML) ページ編集ツールの GUI 版（tkinter）

・ファイルを開く → 左の一覧でページを選ぶ → 挿入／削除／移動 → 別名で保存
・編集はメモリ上で行い、「別名で保存」するまで元のファイルは変更しない（上書き保存は不可）
・ページ番号の領域は右側のプレビューに赤い破線で表示される
"""

import json
import math
import os
import shutil
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import cdxml_core as core

APP_TITLE = "CDXML ページ編集"
UNDO_LIMIT = 30

# 一覧などで使う色
BG_PREVIEW = "#d9dde3"
REGION_COLOR = "#e0312b"
OUTSIDE_PAGE_FG = "#8a8a8a"


def settings_dir():
    """設定とテンプレートを置くフォルダ（OS ごとの標準の場所）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "CDXMLPageEditor")


def bundled_path(name):
    """exe に同梱したファイル（PyInstaller）またはスクリプトと同じフォルダのファイル。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


SETTINGS_FILE = "settings.json"
TEMPLATE_FILE = "template.cdxml"


def compress_ranges(pages):
    """[2,3,4,7] → '2-4,7'"""
    pages = sorted(set(pages))
    out, i = [], 0
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1
        out.append(str(pages[i]) if i == j else "%d-%d" % (pages[i], pages[j]))
        i = j + 1
    return ",".join(out)


class App(tk.Tk):
    def __init__(self, initial_path=None):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x780")
        self.minsize(900, 600)
        self._setup_fonts()

        self.doc = None
        self.path = None
        self.dirty = False
        self.undo_stack = []
        self.redo_stack = []
        self.colors = []
        self.preview_page = None
        self.settings = self._load_settings()
        self.template = None          # core.Template

        self._build_toolbar()
        self._build_log()      # 下端のログ欄を先に確保してから、残りを本体に使う
        self._build_main()
        self._bind_keys()
        self._set_enabled(False)
        self._load_template()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if initial_path:
            self.after(100, lambda: self.open_file(initial_path))
        else:
            self.log("「開く…」から .cdxml ファイルを選んでください。"
                     "（.cdx は ChemDraw で CDXML 形式に保存し直してから使います）")

    # ------------------------------------------------------------------ UI
    def _setup_fonts(self):
        fams = set(tkfont.families(self))
        for name in ("Yu Gothic UI", "Meiryo UI", "Meiryo", "Hiragino Sans",
                     "Hiragino Kaku Gothic ProN", "Noto Sans CJK JP", "IPAexGothic"):
            if name in fams:
                for f in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                    try:
                        tkfont.nametofont(f).configure(family=name)
                    except tk.TclError:
                        pass
                break
        self.preview_family = "Arial" if "Arial" in fams else "Helvetica"
        style = ttk.Style(self)
        if "clam" in style.theme_names() and sys.platform.startswith("linux"):
            style.theme_use("clam")
        # 行の高さは文字の大きさから決める（高解像度ディスプレイで行が詰まらないように）
        f = tkfont.nametofont("TkDefaultFont")
        line = f.metrics("linespace")
        style.configure("Treeview", rowheight=int(line * 1.4) + 6)
        hf = tkfont.nametofont("TkHeadingFont")
        style.configure("Treeview.Heading", padding=(4, int(hf.metrics("linespace") * 0.3)))
        style.configure("Accent.TButton", padding=(10, 4))

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(bar, text="開く…", command=self.ask_open).pack(side=tk.LEFT)
        self.btn_save = ttk.Button(bar, text="別名で保存…", command=self.save_as,
                                   style="Accent.TButton")
        self.btn_save.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        self.btn_undo = ttk.Button(bar, text="元に戻す", command=self.undo)
        self.btn_undo.pack(side=tk.LEFT)
        self.btn_redo = ttk.Button(bar, text="やり直し", command=self.redo)
        self.btn_redo.pack(side=tk.LEFT, padx=(6, 0))
        self.file_label = ttk.Label(bar, text="ファイル未選択", foreground="#555")
        self.file_label.pack(side=tk.LEFT, padx=14)

    def _build_main(self):
        outer = ttk.PanedWindow(self, orient=tk.VERTICAL)
        outer.pack(fill=tk.BOTH, expand=True, padx=8)

        top = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        outer.add(top, weight=4)

        # --- ページ一覧
        left = ttk.Frame(top)
        top.add(left, weight=2)
        self.info_label = ttk.Label(left, text="", foreground="#555")
        self.info_label.pack(anchor=tk.W, pady=(0, 4))
        cols = ("page", "num", "objs", "title")
        tf = ttk.Frame(left)
        tf.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="extended")
        for c, t, w, a in (("page", "ページ", 56, tk.E), ("num", "番号", 56, tk.CENTER),
                           ("objs", "オブジェクト", 84, tk.E), ("title", "先頭のテキスト", 300, tk.W)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=(c == "title"))
        self.tree.tag_configure("outside", foreground=OUTSIDE_PAGE_FG)
        self.tree.tag_configure("nonum", background="#fff4e5")
        sb = ttk.Scrollbar(tf, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        ttk.Label(left, text="Shift / Ctrl＋クリックで複数選択（削除欄に自動入力）",
                  foreground="#777").pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(left, text="灰色の行＝設定ページ数より後ろ　／　橙色の行＝領域内に番号なし",
                  foreground="#777").pack(anchor=tk.W)

        # --- プレビュー
        right = ttk.Frame(top)
        top.add(right, weight=3)
        self.preview_title = ttk.Label(right, text="プレビュー")
        self.preview_title.pack(anchor=tk.W, pady=(0, 4))
        self.canvas = tk.Canvas(right, background=BG_PREVIEW, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda e: self.draw_preview())

        # --- 操作パネル
        bottom = ttk.Frame(outer, padding=(0, 8, 0, 0))
        outer.add(bottom, weight=0)
        self._build_ops(bottom)
        self._build_number_settings(bottom)

    def _build_ops(self, parent):
        lf = ttk.LabelFrame(parent, text="ページ操作", padding=8)
        lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        nb = ttk.Notebook(lf)
        nb.pack(fill=tk.BOTH, expand=True)
        self.ops_nb = nb

        # 挿入
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="  挿入  ")
        self.ins_pos = tk.StringVar(value="1")
        self.ins_count = tk.StringVar(value="1")
        self.ins_mode = tk.StringVar(value="blank")
        self.ins_tpl = tk.StringVar(value="")
        ttk.Label(f, text="挿入位置（新しいページが何ページ目になるか）").grid(row=0, column=0, sticky=tk.W)
        ttk.Spinbox(f, from_=1, to=9999, width=6, textvariable=self.ins_pos).grid(row=0, column=1, sticky=tk.W, padx=6)
        ttk.Label(f, text="ページ数").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Spinbox(f, from_=1, to=999, width=6, textvariable=self.ins_count).grid(row=1, column=1, sticky=tk.W, padx=6, pady=(6, 0))
        tf = ttk.Frame(f)
        tf.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(6, 0))
        self.rb_tpl = ttk.Radiobutton(tf, text="テンプレート:", value="template",
                                      variable=self.ins_mode, command=self._sync_ins)
        self.rb_tpl.pack(side=tk.LEFT)
        self.tpl_label = ttk.Label(tf, text="（未登録）", foreground="#555")
        self.tpl_label.pack(side=tk.LEFT, padx=6)
        ttk.Button(tf, text="登録…", command=self.register_template).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Radiobutton(f, text="空白ページ", value="blank", variable=self.ins_mode,
                        command=self._sync_ins).grid(row=3, column=0, sticky=tk.W)
        rf = ttk.Frame(f)
        rf.grid(row=4, column=0, columnspan=3, sticky=tk.W)
        ttk.Radiobutton(rf, text="次のページを複製:", value="copy", variable=self.ins_mode,
                        command=self._sync_ins).pack(side=tk.LEFT)
        self.ins_tpl_box = ttk.Spinbox(rf, from_=1, to=9999, width=6, textvariable=self.ins_tpl)
        self.ins_tpl_box.pack(side=tk.LEFT, padx=6)
        ttk.Label(rf, text="ページ目（挿入前の番号）", foreground="#555").pack(side=tk.LEFT)
        self.btn_ins = ttk.Button(f, text="挿入する", command=self.do_insert, style="Accent.TButton")
        self.btn_ins.grid(row=5, column=0, sticky=tk.W, pady=(10, 0))
        self._sync_ins()

        # 削除
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="  削除  ")
        self.del_pages = tk.StringVar(value="")
        ttk.Label(f, text="削除するページ（例: 4  /  4,7-9）").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(f, width=18, textvariable=self.del_pages).grid(row=0, column=1, sticky=tk.W, padx=6)
        ttk.Label(f, text="一覧で選んだページが自動で入ります。", foreground="#555").grid(
            row=1, column=0, columnspan=2, sticky=tk.W, pady=(4, 0))
        self.btn_del = ttk.Button(f, text="削除する", command=self.do_delete, style="Accent.TButton")
        self.btn_del.grid(row=2, column=0, sticky=tk.W, pady=(10, 0))

        # 移動
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="  移動  ")
        self.mv_src = tk.StringVar(value="")
        self.mv_dst = tk.StringVar(value="")
        ttk.Label(f, text="移動するページ").grid(row=0, column=0, sticky=tk.W)
        ttk.Spinbox(f, from_=1, to=9999, width=6, textvariable=self.mv_src).grid(row=0, column=1, sticky=tk.W, padx=6)
        ttk.Label(f, text="移動先（何ページ目にするか）").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Spinbox(f, from_=1, to=9999, width=6, textvariable=self.mv_dst).grid(row=1, column=1, sticky=tk.W, padx=6, pady=(6, 0))
        bf = ttk.Frame(f)
        bf.grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))
        self.btn_mv = ttk.Button(bf, text="移動する", command=self.do_move, style="Accent.TButton")
        self.btn_mv.pack(side=tk.LEFT)
        self.btn_up = ttk.Button(bf, text="▲ 1つ上へ", command=lambda: self.do_nudge(-1))
        self.btn_up.pack(side=tk.LEFT, padx=(12, 0))
        self.btn_down = ttk.Button(bf, text="▼ 1つ下へ", command=lambda: self.do_nudge(1))
        self.btn_down.pack(side=tk.LEFT, padx=(6, 0))

    def _build_number_settings(self, parent):
        lf = ttk.LabelFrame(parent, text="ページ番号", padding=8)
        lf.pack(side=tk.LEFT, fill=tk.BOTH, padx=(8, 0))
        st = self.settings
        self.num_auto = tk.BooleanVar(value=st.get("auto_renumber", True))
        self.num_region = tk.StringVar(value=st.get("region", "右下"))
        self.num_w = tk.StringVar(value=str(st.get("region_w", "100")))
        self.num_h = tk.StringVar(value=str(st.get("region_h", "60")))
        self.num_custom = [tk.StringVar(value=str(v)) for v in
                           st.get("region_custom", ("660", "460", "770", "523"))]
        self.num_first_page = tk.StringVar(value="1")
        self.num_first_number = tk.StringVar(value="1")
        self.num_add_missing = tk.BooleanVar(value=False)

        ttk.Checkbutton(lf, text="ページ操作のあとに自動で振り直す",
                        variable=self.num_auto).grid(row=0, column=0, columnspan=4, sticky=tk.W)
        ttk.Label(lf, text="番号を探す領域").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        cb = ttk.Combobox(lf, width=8, state="readonly", textvariable=self.num_region,
                          values=list(core.PRESETS) + ["カスタム"])
        cb.grid(row=1, column=1, sticky=tk.W, pady=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self._sync_region())

        self.preset_frame = ttk.Frame(lf)
        self.preset_frame.grid(row=2, column=0, columnspan=4, sticky=tk.W, pady=(4, 0))
        ttk.Label(self.preset_frame, text="大きさ  幅").pack(side=tk.LEFT)
        ttk.Spinbox(self.preset_frame, from_=5, to=2000, increment=5, width=5,
                    textvariable=self.num_w).pack(side=tk.LEFT, padx=3)
        ttk.Label(self.preset_frame, text="× 高さ").pack(side=tk.LEFT)
        ttk.Spinbox(self.preset_frame, from_=5, to=2000, increment=5, width=5,
                    textvariable=self.num_h).pack(side=tk.LEFT, padx=3)
        ttk.Label(self.preset_frame, text="pt").pack(side=tk.LEFT)

        self.custom_frame = ttk.Frame(lf)
        self.custom_frame.grid(row=3, column=0, columnspan=4, sticky=tk.W, pady=(4, 0))
        for i, lab in enumerate(("左", "上", "右", "下")):
            ttk.Label(self.custom_frame, text=lab).pack(side=tk.LEFT, padx=(6 if i else 0, 2))
            ttk.Entry(self.custom_frame, width=6, textvariable=self.num_custom[i]).pack(side=tk.LEFT)
        ttk.Label(self.custom_frame, text="pt（ページ左上から）", foreground="#555").pack(side=tk.LEFT, padx=4)

        nf = ttk.Frame(lf)
        nf.grid(row=4, column=0, columnspan=4, sticky=tk.W, pady=(8, 0))
        ttk.Spinbox(nf, from_=1, to=9999, width=5, textvariable=self.num_first_page).pack(side=tk.LEFT)
        ttk.Label(nf, text="ページ目を").pack(side=tk.LEFT, padx=3)
        ttk.Spinbox(nf, from_=-999, to=9999, width=5, textvariable=self.num_first_number).pack(side=tk.LEFT)
        ttk.Label(nf, text="として連番にする").pack(side=tk.LEFT, padx=3)
        ttk.Checkbutton(lf, text="番号のないページにも追加する（既存の番号をコピー）",
                        variable=self.num_add_missing).grid(row=5, column=0, columnspan=4, sticky=tk.W, pady=(6, 0))
        self.btn_renum = ttk.Button(lf, text="番号だけ振り直す", command=self.do_renumber)
        self.btn_renum.grid(row=6, column=0, sticky=tk.W, pady=(8, 0))

        for v in [self.num_w, self.num_h] + self.num_custom:
            v.trace_add("write", lambda *a: self._schedule_refresh())
        self._sync_region()

    def _build_log(self):
        f = ttk.Frame(self, padding=(8, 6, 8, 8))
        f.pack(side=tk.BOTTOM, fill=tk.X)
        self.logbox = tk.Text(f, height=5, wrap="word", relief=tk.FLAT, background="#f6f7f9",
                              state=tk.DISABLED)
        self.logbox.tag_configure("err", foreground="#b3261e")
        self.logbox.tag_configure("warn", foreground="#9a5b00")
        self.logbox.pack(fill=tk.X)

    def _bind_keys(self):
        mod = "Command" if sys.platform == "darwin" else "Control"
        self.bind_all("<%s-o>" % mod, lambda e: self.ask_open())
        self.bind_all("<%s-s>" % mod, lambda e: self.save_as())
        self.bind_all("<%s-z>" % mod, lambda e: self.undo())
        self.bind_all("<%s-y>" % mod, lambda e: self.redo())
        self.tree.bind("<Delete>", lambda e: self.do_delete())

    # ------------------------------------------------------------- helpers
    def log(self, msg, tag=None):
        self.logbox.configure(state=tk.NORMAL)
        self.logbox.insert(tk.END, msg + "\n", tag)
        self.logbox.see(tk.END)
        self.logbox.configure(state=tk.DISABLED)

    def _set_enabled(self, on):
        st = ["!disabled"] if on else ["disabled"]
        for b in (self.btn_save, self.btn_ins, self.btn_del, self.btn_mv, self.btn_up,
                  self.btn_down, self.btn_renum):
            b.state(st)
        self._update_undo_buttons()

    def _update_undo_buttons(self):
        self.btn_undo.state(["!disabled"] if self.undo_stack else ["disabled"])
        self.btn_redo.state(["!disabled"] if self.redo_stack else ["disabled"])

    def _update_title(self):
        name = os.path.basename(self.path) if self.path else ""
        mark = " ＊未保存の変更あり" if self.dirty else ""
        self.title("%s — %s%s" % (APP_TITLE, name, mark) if name else APP_TITLE)
        self.file_label.configure(text=(self.path or "ファイル未選択") + mark)

    def _sync_ins(self):
        self.ins_tpl_box.state(["!disabled"] if self.ins_mode.get() == "copy" else ["disabled"])

    def _sync_region(self):
        custom = self.num_region.get() == "カスタム"
        for w in self.custom_frame.winfo_children():
            if isinstance(w, ttk.Entry):
                w.state(["!disabled"] if custom else ["disabled"])
        for w in self.preset_frame.winfo_children():
            if isinstance(w, ttk.Spinbox):
                w.state(["disabled"] if custom else ["!disabled"])
        if custom and self.doc is not None:
            # 現在のプリセット位置をカスタム値の初期値にする
            try:
                r = self._number_options(for_custom_seed=True).rect(self.doc)
                for var, v in zip(self.num_custom, r):
                    var.set(core.fmt(v))
            except Exception:
                pass
        self._schedule_refresh()

    def _schedule_refresh(self):
        if getattr(self, "_refresh_job", None):
            self.after_cancel(self._refresh_job)
        self._refresh_job = self.after(250, self.refresh)

    def _int(self, var, name):
        try:
            return int(str(var.get()).strip())
        except ValueError:
            raise core.CdxmlError("「%s」には整数を入力してください。" % name)

    def _number_options(self, for_custom_seed=False):
        o = core.NumberOptions()
        reg = self.num_region.get()
        try:
            o.width, o.height = float(self.num_w.get()), float(self.num_h.get())
        except ValueError:
            raise core.CdxmlError("領域の幅・高さには数値を入力してください。")
        if reg == "カスタム" and not for_custom_seed:
            try:
                o.region = "custom"
                o.custom = tuple(float(v.get()) for v in self.num_custom)
            except ValueError:
                raise core.CdxmlError("カスタム領域には数値を入力してください。")
        else:
            o.region = "右下" if reg == "カスタム" else reg
        o.first_page = self._int(self.num_first_page, "開始ページ")
        o.first_number = self._int(self.num_first_number, "開始番号")
        o.add_missing = self.num_add_missing.get()
        return o

    def selected_pages(self):
        return sorted(int(self.tree.set(i, "page")) for i in self.tree.selection())

    # ---------------------------------------------------------------- file
    def ask_open(self):
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            title="CDXML ファイルを開く",
            filetypes=[("ChemDraw XML", "*.cdxml"), ("すべてのファイル", "*.*")])
        if path:
            self.open_file(path, confirmed=True)

    def open_file(self, path, confirmed=False):
        if not confirmed and not self._confirm_discard():
            return
        if path.lower().endswith(".cdx"):
            messagebox.showwarning(APP_TITLE, ".cdx（バイナリ形式）は直接扱えません。\n"
                                   "ChemDraw で「名前を付けて保存」→ CDXML 形式で保存してから開いてください。")
            return
        try:
            doc = core.Doc.load(path)
        except core.CdxmlError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.doc, self.path = doc, path
        self.colors = [(c.get("r"), c.get("g"), c.get("b")) for c in doc.root.iter("color")]
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.dirty = False
        self.preview_page = None
        self._set_enabled(True)
        self._update_title()
        self.log("開きました: %s" % path)
        if doc.content_pages() > doc.declared:
            self.log("注意: ChemDraw の設定上は %d ページですが、%d ページ目の位置までオブジェクトがあります。"
                     "これらもページとして扱います（一覧で灰色の行）。"
                     % (doc.declared, doc.content_pages()), "warn")
        self.refresh()
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])
            self.tree.see(kids[0])

    def _confirm_discard(self):
        if self.dirty:
            return messagebox.askyesno(APP_TITLE, "保存していない変更があります。破棄してよろしいですか？")
        return True

    def save_as(self):
        if self.doc is None:
            return
        d, b = os.path.split(core.default_output_path(self.path))
        out = filedialog.asksaveasfilename(
            title="別名で保存", initialdir=d, initialfile=b, defaultextension=".cdxml",
            filetypes=[("ChemDraw XML", "*.cdxml")])
        if not out:
            return
        try:
            self.doc.save_as(out)
        except core.CdxmlError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.dirty = False
        self._update_title()
        self.log("保存しました: %s（元のファイルは変更していません）" % out)

    def on_close(self):
        if self._confirm_discard():
            self._save_settings()
            self.destroy()

    # ------------------------------------------------------- settings / template
    def _load_settings(self):
        try:
            with open(os.path.join(settings_dir(), SETTINGS_FILE), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        st = {
            "insert_mode": self.ins_mode.get(),
            "auto_renumber": bool(self.num_auto.get()),
            "region": self.num_region.get(),
            "region_w": self.num_w.get(),
            "region_h": self.num_h.get(),
            "region_custom": [v.get() for v in self.num_custom],
            "template_name": self.settings.get("template_name"),
        }
        try:
            os.makedirs(settings_dir(), exist_ok=True)
            with open(os.path.join(settings_dir(), SETTINGS_FILE), "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def _load_template(self):
        """登録済みテンプレート → なければ同梱のテンプレートを読み込む。"""
        self.template = None
        for path, label in ((os.path.join(settings_dir(), TEMPLATE_FILE), "登録済み"),
                            (bundled_path(TEMPLATE_FILE), "標準")):
            if os.path.exists(path):
                try:
                    self.template = core.Template(path)
                    name = self.settings.get("template_name") if label == "登録済み" else None
                    if name:
                        self.template.name = name
                    self.tpl_label.configure(text="%s（%s, オブジェクト %d 個）" % (
                        name or TEMPLATE_FILE, label, len(self.template.elements)))
                    break
                except core.CdxmlError as e:
                    self.log("テンプレートを読み込めませんでした: %s" % e, "warn")
        if self.template is None:
            self.tpl_label.configure(text="（未登録：「登録…」から .cdxml を選ぶと使えます）")
        self.rb_tpl.state(["!disabled"] if self.template else ["disabled"])
        mode = self.settings.get("insert_mode", "template" if self.template else "blank")
        if mode == "template" and self.template is None:
            mode = "blank"
        self.ins_mode.set(mode)
        self._sync_ins()

    def register_template(self):
        path = filedialog.askopenfilename(
            title="テンプレートにする CDXML ファイルを選ぶ",
            filetypes=[("ChemDraw XML", "*.cdxml"), ("すべてのファイル", "*.*")])
        if not path:
            return
        try:
            core.Template(path)    # 読めるか確認
            os.makedirs(settings_dir(), exist_ok=True)
            dst = os.path.join(settings_dir(), TEMPLATE_FILE)
            if not core.same_file(path, dst):
                shutil.copyfile(path, dst)
        except core.CdxmlError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        except OSError as e:
            messagebox.showerror(APP_TITLE, "テンプレートを保存できませんでした: %s" % e)
            return
        self.settings["template_name"] = os.path.basename(path)
        self.settings["insert_mode"] = "template"
        self._save_settings()
        self._load_template()
        self.log("テンプレートを登録しました: %s（コピーを %s に保存）" % (path, settings_dir()))

    # ---------------------------------------------------------- operations
    def _apply(self, func, select_after=None, add_pages=()):
        """func(doc) -> メッセージ を実行。失敗したら元に戻す。
        add_pages: 番号がなければ番号を追加するページ（挿入したページ）"""
        if self.doc is None:
            return
        backup = self.doc.clone()
        try:
            msg = func(self.doc)
            warns = []
            if self.num_auto.get():
                m2, warns = core.renumber(self.doc, self._number_options(), add_pages=add_pages)
                msg += " " + m2
        except core.CdxmlError as e:
            self.doc = backup
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.undo_stack.append((backup, msg))
        del self.undo_stack[:-UNDO_LIMIT]
        self.redo_stack.clear()
        self.dirty = True
        self._update_title()
        self._update_undo_buttons()
        self.log(msg)
        for w in warns:
            self.log("注意: " + w, "warn")
        self.refresh(select=select_after)

    def do_insert(self):
        try:
            pos = self._int(self.ins_pos, "挿入位置")
            cnt = self._int(self.ins_count, "ページ数")
            mode = self.ins_mode.get()
            tpl = self._int(self.ins_tpl, "複製元ページ") if mode == "copy" else None
            tfile = None
            if mode == "template":
                if self.template is None:
                    raise core.CdxmlError("テンプレートが登録されていません。「登録…」から選んでください。")
                tfile = self.template
        except core.CdxmlError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        if tfile is not None and tfile.size_warning(self.doc):
            self.log("注意: " + tfile.size_warning(self.doc), "warn")
        pages = list(range(pos, pos + cnt))
        self._apply(lambda d: core.insert_pages(d, pos, cnt, tpl, tfile),
                    select_after=pages, add_pages=pages)

    def do_delete(self):
        if self.doc is None:
            return
        s = self.del_pages.get().strip()
        try:
            pages = core.parse_pages(s)
        except core.CdxmlError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        by = self.doc.objects_by_page()
        n = sum(len(by.get(p, [])) for p in pages)
        if not messagebox.askyesno(APP_TITLE, "ページ %s を削除します（オブジェクト %d 個）。\n"
                                   "よろしいですか？（「元に戻す」で取り消せます）"
                                   % (compress_ranges(pages), n)):
            return
        first = min(pages)
        self._apply(lambda d: core.delete_pages(d, pages),
                    select_after=[max(1, min(first, self.doc.total_pages - len(pages)))])

    def do_move(self):
        try:
            a = self._int(self.mv_src, "移動するページ")
            b = self._int(self.mv_dst, "移動先")
        except core.CdxmlError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self._apply(lambda d: core.move_page(d, a, b), select_after=[b])

    def do_nudge(self, delta):
        sel = self.selected_pages()
        if len(sel) != 1:
            messagebox.showinfo(APP_TITLE, "一覧でページを1つ選んでください。")
            return
        a = sel[0]
        b = a + delta
        if not 1 <= b <= self.doc.total_pages:
            return
        self._apply(lambda d: core.move_page(d, a, b), select_after=[b])

    def do_renumber(self):
        if self.doc is None:
            return
        backup = self.doc.clone()
        try:
            msg, warns = core.renumber(self.doc, self._number_options())
        except core.CdxmlError as e:
            self.doc = backup
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.undo_stack.append((backup, msg))
        self.redo_stack.clear()
        self.dirty = True
        self._update_title()
        self._update_undo_buttons()
        self.log(msg)
        for w in warns:
            self.log("注意: " + w, "warn")
        self.refresh(select=self.selected_pages())

    def undo(self):
        if not self.undo_stack:
            return
        prev, msg = self.undo_stack.pop()
        self.redo_stack.append((self.doc, msg))
        self.doc = prev
        self.dirty = True
        self._update_title()
        self._update_undo_buttons()
        self.log("元に戻しました: " + msg)
        self.refresh(select=self.selected_pages())

    def redo(self):
        if not self.redo_stack:
            return
        nxt, msg = self.redo_stack.pop()
        self.undo_stack.append((self.doc, msg))
        self.doc = nxt
        self.dirty = True
        self._update_title()
        self._update_undo_buttons()
        self.log("やり直しました: " + msg)
        self.refresh(select=self.selected_pages())

    # ------------------------------------------------------------ display
    def refresh(self, select=None):
        self._refresh_job = None
        if self.doc is None:
            return
        if select is None:
            select = self.selected_pages()
        try:
            opts = self._number_options()
            rect = opts.rect(self.doc)
        except core.CdxmlError:
            rect = None
        rows = core.page_summary(self.doc, rect or [0, 0, 0, 0])
        self.tree.delete(*self.tree.get_children())
        for p, n, num, title, inside in rows:
            tags = []
            if not inside:
                tags.append("outside")
            elif rect and not num and p >= 2:
                tags.append("nonum")
            self.tree.insert("", tk.END, iid=str(p), values=(p, num or "–", n, title), tags=tags)
        d = self.doc
        self.info_label.configure(
            text="設定ページ数 %d ／ 全体 %d ページ ／ 1ページ %.0f × %.0f pt"
                 % (d.declared, d.total_pages, d.PW, d.PH))
        keep = [str(p) for p in select if self.tree.exists(str(p))]
        if keep:
            self.tree.selection_set(keep)
            self.tree.see(keep[0])
        self.draw_preview()

    def on_select(self, event=None):
        sel = self.selected_pages()
        if not sel:
            return
        self.preview_page = sel[0]
        self.ins_pos.set(str(sel[0]))
        self.ins_tpl.set(str(sel[0]))
        self.del_pages.set(compress_ranges(sel))
        self.mv_src.set(str(sel[0]))
        if not self.mv_dst.get():
            self.mv_dst.set(str(sel[0]))
        self.draw_preview()

    def _color(self, idx, default="#000000"):
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return default
        if i == 0:
            return "#000000"
        if i == 1:
            return "#ffffff"
        if 2 <= i < len(self.colors) + 2:
            try:
                r, g, b = (float(x) for x in self.colors[i - 2])
                return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
            except (TypeError, ValueError):
                pass
        return default

    def draw_preview(self):
        c = self.canvas
        c.delete("all")
        if self.doc is None or self.preview_page is None:
            return
        d = self.doc
        p = min(self.preview_page, d.total_pages)
        cw, ch = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        s = min((cw - 24) / d.PW, (ch - 24) / d.PH)
        ox, oy = d.page_origin(p)
        mx, my = (cw - d.PW * s) / 2, (ch - d.PH * s) / 2

        def X(x):
            return mx + (x - ox) * s

        def Y(y):
            return my + (y - oy) * s

        self.preview_title.configure(text="プレビュー：%d ページ目%s" % (
            p, "（設定ページ数より後ろ）" if p > d.declared else ""))
        c.create_rectangle(mx + 3, my + 3, mx + d.PW * s + 3, my + d.PH * s + 3,
                           fill="#b9bec6", outline="")
        c.create_rectangle(mx, my, mx + d.PW * s, my + d.PH * s, fill="white", outline="#888")

        try:
            rect = self._number_options().rect(d)
        except core.CdxmlError:
            rect = None
        numbers = core.find_page_numbers(d, rect).get(p, []) if rect else []

        els = d.objects_by_page().get(p, [])
        els.sort(key=lambda e: int(e.get("Z", "0")) if e.get("Z", "0").isdigit() else 0)
        for el in els:
            try:
                self._draw_element(c, el, X, Y, s)
            except Exception:
                b = core.bbox_of(el)
                if b:
                    c.create_rectangle(X(b[0]), Y(b[1]), X(b[2]), Y(b[3]), outline="#bbb")
        for t in numbers:
            b = core.bbox_of(t)
            c.create_rectangle(X(b[0]) - 2, Y(b[1]) - 2, X(b[2]) + 2, Y(b[3]) + 2,
                               outline=REGION_COLOR, width=2)
        if rect:
            c.create_rectangle(mx + rect[0] * s, my + rect[1] * s, mx + rect[2] * s, my + rect[3] * s,
                               outline=REGION_COLOR, dash=(5, 3), width=1.5)

    def _draw_element(self, c, el, X, Y, s):
        tag = el.tag
        if tag == "t":
            self._draw_text(c, el, X, Y, s)
        elif tag == "fragment":
            self._draw_fragment(c, el, X, Y, s)
        elif tag == "graphic":
            if el.get("SupersededBy"):
                return   # 同じ図形を arrow 要素が持っている
            gt = el.get("GraphicType")
            b = core.nums(el.get("BoundingBox"))
            if not b:
                return
            col = self._color(el.get("color"), "#000000")
            if gt == "Rectangle":
                fill = col if el.get("RectangleType", "") in ("Filled", "RoundEdge Filled") else ""
                c.create_rectangle(X(min(b[0], b[2])), Y(min(b[1], b[3])),
                                   X(max(b[0], b[2])), Y(max(b[1], b[3])),
                                   outline=col, fill=fill)
            elif gt in ("Oval", "Orbital"):
                cen = core.nums(el.get("Center3D"))
                maj = core.nums(el.get("MajorAxisEnd3D"))
                if cen and maj:
                    r = math.hypot(maj[0] - cen[0], maj[1] - cen[1])
                    fill = col if "Filled" in el.get("OvalType", "") else ""
                    c.create_oval(X(cen[0] - r), Y(cen[1] - r), X(cen[0] + r), Y(cen[1] + r),
                                  outline=col, fill=fill)
            elif gt == "Line":
                c.create_line(X(b[0]), Y(b[1]), X(b[2]), Y(b[3]), fill=col)
            elif gt == "Bracket":
                c.create_line(X(b[0]), Y(b[1]), X(b[2]), Y(b[3]), fill=col)
        elif tag == "arrow":
            h = core.nums(el.get("Head3D"))
            t = core.nums(el.get("Tail3D"))
            if h and t:
                col = self._color(el.get("color"), "#000000")
                head = bool(el.get("ArrowheadHead"))
                tail = bool(el.get("ArrowheadTail"))
                arrow = tk.BOTH if head and tail else tk.LAST if head else tk.FIRST if tail else tk.NONE
                dash = (4, 3) if el.get("LineType") == "Dashed" else None
                c.create_line(X(t[0]), Y(t[1]), X(h[0]), Y(h[1]), fill=col, arrow=arrow,
                              dash=dash, arrowshape=(8, 10, 3))
        elif tag == "embeddedobject":
            b = core.bbox_of(el)
            c.create_rectangle(X(b[0]), Y(b[1]), X(b[2]), Y(b[3]), outline="#999",
                               fill="#eef0f3", stipple="gray25")
            c.create_text((X(b[0]) + X(b[2])) / 2, (Y(b[1]) + Y(b[3])) / 2, text="図",
                          fill="#777")
        else:
            b = core.bbox_of(el)
            if b and el.tag not in ("scheme", "bracketedgroup"):
                c.create_rectangle(X(b[0]), Y(b[1]), X(b[2]), Y(b[3]), outline="#bbb")

    def _draw_text(self, c, t, X, Y, s, color_default="#000000"):
        p = core.nums(t.get("p"))
        if not p:
            return
        txt = core.text_of(t)
        if not txt.strip():
            return
        ss = list(t.iter("s"))
        size = float(ss[0].get("size", "12")) if ss else 12.0
        col = self._color(ss[0].get("color"), color_default) if ss else color_default
        px = int(round(size * s))
        if px < 3:
            return
        just = t.get("Justification") or t.get("LabelJustification") or "Left"
        anchor = {"Right": "se", "Center": "s"}.get(just, "sw")
        try:
            lh = float(t.get("LineHeight", ""))
        except ValueError:
            lh = size * 1.18
        bold = any(int(x.get("face", "0") or 0) & 1 for x in ss if x.get("face", "0").isdigit())
        font = (self.preview_family, -px, "bold" if bold else "normal")
        for i, line in enumerate(txt.split("\n")):
            c.create_text(X(p[0]), Y(p[1] + i * lh) + px * 0.22, text=line, anchor=anchor,
                          font=font, fill=col)

    def _draw_fragment(self, c, frag, X, Y, s):
        pos = {}
        for n in frag.iter("n"):
            v = core.nums(n.get("p"))
            if v:
                pos[n.get("id")] = v
        for b in frag.findall("b"):
            a, e = pos.get(b.get("B")), pos.get(b.get("E"))
            if a and e:
                w = 2 if b.get("Order") in ("2", "3") else 1
                c.create_line(X(a[0]), Y(a[1]), X(e[0]), Y(e[1]), width=w)
        for n in frag.findall("n"):
            t = n.find("t")
            if t is not None:
                v = pos.get(n.get("id"))
                if v:   # ラベルの背景を白抜きにする
                    r = 6 * s
                    c.create_rectangle(X(v[0]) - r, Y(v[1]) - r, X(v[0]) + r, Y(v[1]) + r,
                                       fill="white", outline="")
                self._draw_text(c, t, X, Y, s)


def main():
    if sys.platform == "win32":
        try:   # 高解像度ディスプレイでぼやけないようにする
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    path = sys.argv[1] if len(sys.argv) > 1 else None   # exe にファイルをドロップしても開ける
    App(path).mainloop()


if __name__ == "__main__":
    main()
