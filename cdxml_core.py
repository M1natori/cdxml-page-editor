# -*- coding: utf-8 -*-
"""
cdxml_core.py — ChemDraw の CDXML ファイルでページを追加・削除・移動し、
ページ番号を振り直す処理の本体（標準ライブラリのみ）。

コマンド版 (cdxml_pages.py) と GUI 版 (cdxml_pages_gui.py) の両方から使う。
エラーは CdxmlError で通知し、print や終了処理はしない。
"""

import copy
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

__version__ = "1.0.0"


class CdxmlError(Exception):
    """利用者に見せるエラー。"""


# ---------------------------------------------------------------------------
# 座標・参照を持つ属性
# ---------------------------------------------------------------------------
COORD2_ATTRS = {"p", "BoundingBox", "CurvePoints", "Points", "BezierPoints"}
COORD3_ATTRS = {"xyz", "3D", "Center3D", "MajorAxisEnd3D", "MinorAxisEnd3D",
                "Head3D", "Tail3D", "CurvePoints3D"}
REF_ATTRS = {"B", "E", "SupersededBy", "BondCircularOrdering", "CrossingBonds",
             "BracketedObjectIDs", "GraphicID", "BondID", "InnerAtomID",
             "ConnectionOrder", "BondOrdering", "Attachments", "BeginAttach",
             "EndAttach", "ReactionStepReactants", "ReactionStepProducts",
             "ReactionStepArrows", "ReactionStepPlusses",
             "ReactionStepObjectsAboveArrow", "ReactionStepObjectsBelowArrow",
             "ReactionStepAtomMap", "BasisObjects", "LinkedTo"}
STEP_ATTRS = ["ReactionStepReactants", "ReactionStepProducts", "ReactionStepArrows",
              "ReactionStepPlusses", "ReactionStepObjectsAboveArrow",
              "ReactionStepObjectsBelowArrow"]
SENTINELS = {-32768.0, 32767.0}   # ChemDraw が「値なし」に使う数値は動かさない
DIGIT_EM = 0.556                  # Arial の数字1文字の幅（em）
NUM_RE = re.compile(r"\d+")


def fmt(v):
    s = ("%.2f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def nums(s):
    if not s:
        return None
    try:
        return [float(x) for x in s.split()]
    except ValueError:
        return None


def bbox_of(el):
    """要素（とその子孫）の外接矩形 [x1,y1,x2,y2]。座標がなければ None。"""
    v = nums(el.get("BoundingBox"))
    if v and len(v) == 4:
        return [min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3])]
    xs, ys = [], []
    for e in el.iter():
        for k in ("p", "BoundingBox"):
            v = nums(e.get(k))
            if v:
                xs += v[0::2]
                ys += v[1::2]
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def translate(el, dx, dy):
    for e in el.iter():
        for k, v in list(e.attrib.items()):
            if k in COORD2_ATTRS:
                dim = 2
            elif k in COORD3_ATTRS:
                dim = 3
            else:
                continue
            vals = nums(v)
            if vals is None:
                continue
            out = []
            for i, x in enumerate(vals):
                axis = i % dim
                if x in SENTINELS:
                    out.append(x)
                elif axis == 0:
                    out.append(x + dx)
                elif axis == 1:
                    out.append(x + dy)
                else:
                    out.append(x)
            e.set(k, " ".join(fmt(x) for x in out))


def text_of(t):
    return "".join(t.itertext())


def same_file(a, b):
    na = os.path.normcase(os.path.realpath(os.path.abspath(a)))
    nb = os.path.normcase(os.path.realpath(os.path.abspath(b)))
    if na == nb:
        return True
    try:
        return os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return False


def default_output_path(src):
    base, ext = os.path.splitext(src)
    return base + "_edited" + (ext or ".cdxml")


# ---------------------------------------------------------------------------
# ドキュメント
# ---------------------------------------------------------------------------
class Doc:
    def __init__(self, raw, source_path=None):
        self.source_path = source_path
        i = raw.find("<CDXML")
        if i < 0:
            raise CdxmlError("CDXML ファイルではないようです（<CDXML> が見つかりません）。\n"
                             "ChemDraw で「名前を付けて保存」→ 形式に CDXML を選んで保存してください。")
        self.prolog = raw[:i]
        try:
            self.root = ET.fromstring(raw[i:])
        except ET.ParseError as e:
            raise CdxmlError("ファイルを読み取れませんでした（XML の形式エラー: %s）" % e)
        pages = self.root.findall("page")
        if not pages:
            raise CdxmlError("<page> 要素が見つかりません。")
        if len(pages) > 1:
            raise CdxmlError("このファイルには <page> が複数あり、未対応です。")
        self.page = pages[0]
        bb = nums(self.page.get("BoundingBox"))
        if not bb or len(bb) != 4:
            raise CdxmlError("ページの大きさ（BoundingBox）が読み取れません。")
        try:
            hp = int(self.page.get("HeightPages", "1"))
            wp = int(self.page.get("WidthPages", "1"))
        except ValueError:
            raise CdxmlError("ページ数（HeightPages / WidthPages）が読み取れません。")
        if hp > 1 and wp > 1:
            raise CdxmlError("縦横どちらにもページが並ぶレイアウト（横%d×縦%d）は未対応です。"
                             % (wp, hp))
        self.vertical = wp == 1
        self.PW = (bb[2] - bb[0]) / wp
        self.PH = (bb[3] - bb[1]) / hp
        self.x0, self.y0 = bb[0], bb[1]
        self.declared = hp if self.vertical else wp

    @classmethod
    def load(cls, path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except UnicodeDecodeError:
            raise CdxmlError("テキスト（CDXML）として読めません。.cdx（バイナリ形式）の場合は、"
                             "ChemDraw で CDXML 形式で保存し直してください。")
        except OSError as e:
            raise CdxmlError("ファイルを開けませんでした: %s" % e)
        return cls(raw, source_path=path)

    def clone(self):
        return copy.deepcopy(self)

    # ---- ページの判定 ----
    @property
    def step(self):
        return self.PH if self.vertical else self.PW

    @property
    def total_pages(self):
        """設定上のページ数と、オブジェクトがある最後のページの大きい方。"""
        return max(self.declared, self.content_pages())

    def page_of(self, el):
        box = bbox_of(el)
        if box is None:
            return None
        if self.vertical:
            c = (box[1] + box[3]) / 2 - self.y0
        else:
            c = (box[0] + box[2]) / 2 - self.x0
        return max(1, int(math.floor(c / self.step)) + 1)

    def page_origin(self, n):
        if self.vertical:
            return self.x0, self.y0 + (n - 1) * self.PH
        return self.x0 + (n - 1) * self.PW, self.y0

    def objects_by_page(self):
        d = {}
        for el in list(self.page):
            p = self.page_of(el)
            if p is not None:
                d.setdefault(p, []).append(el)
        return d

    def content_pages(self):
        d = self.objects_by_page()
        return max(d) if d else 0

    def shift_pages(self, el, dpages):
        if dpages == 0:
            return
        dx, dy = (0, dpages * self.PH) if self.vertical else (dpages * self.PW, 0)
        translate(el, dx, dy)

    def set_page_count(self, n):
        n = max(1, n)
        bb = nums(self.page.get("BoundingBox"))
        if self.vertical:
            self.page.set("HeightPages", str(n))
            bb[3] = bb[1] + n * self.PH
        else:
            self.page.set("WidthPages", str(n))
            bb[2] = bb[0] + n * self.PW
        self.declared = n
        self.page.set("BoundingBox", " ".join(fmt(v) for v in bb))

    def update_root_bbox(self):
        boxes = [b for b in (bbox_of(e) for e in self.page) if b]
        if boxes:
            u = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                 max(b[2] for b in boxes), max(b[3] for b in boxes)]
            self.root.set("BoundingBox", " ".join(fmt(v) for v in u))

    def save_as(self, path):
        """別ファイルとして保存する。読み込んだ元のファイルへの上書きは拒否する。"""
        if self.source_path and same_file(self.source_path, path):
            raise CdxmlError("元のファイルには上書きできません。別の名前で保存してください。")
        self.update_root_bbox()
        body = ET.tostring(self.root, encoding="unicode")
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.prolog + body + "\n")
        except OSError as e:
            raise CdxmlError("保存できませんでした: %s" % e)

    # ---- id 管理 ----
    def max_id(self):
        m = 0
        for e in self.root.iter():
            v = e.get("id")
            if v and v.isdigit():
                m = max(m, int(v))
        return m

    def max_z(self):
        m = 0
        for e in self.root.iter():
            v = e.get("Z")
            if v and v.isdigit():
                m = max(m, int(v))
        return m


# ---------------------------------------------------------------------------
# ページの並べ替え（追加・削除・移動の共通処理）
# ---------------------------------------------------------------------------
def _remap_pages(doc, mapping, new_count):
    """mapping: 元ページ番号 -> 新ページ番号 (None なら削除)。"""
    deleted_ids = set()
    removed = 0
    for p, els in doc.objects_by_page().items():
        newp = mapping(p)
        for el in els:
            if newp is None:
                for e in el.iter():
                    if e.get("id"):
                        deleted_ids.add(e.get("id"))
                doc.page.remove(el)
                removed += 1
            else:
                doc.shift_pages(el, newp - p)
    if deleted_ids:
        _cleanup_refs(doc, deleted_ids)
    doc.set_page_count(new_count)
    return removed


def _cleanup_refs(doc, deleted):
    changed = True
    while changed:   # 削除した矢印などを置き換え先にしていた古い図形も削除
        changed = False
        for el in list(doc.page):
            sb = el.get("SupersededBy")
            if sb and sb in deleted:
                for e in el.iter():
                    if e.get("id"):
                        deleted.add(e.get("id"))
                doc.page.remove(el)
                changed = True
    for scheme in list(doc.page.findall("scheme")):   # 反応スキーム情報
        for st in list(scheme.findall("step")):
            for a in STEP_ATTRS:
                if st.get(a):
                    keep = [x for x in st.get(a).split() if x not in deleted]
                    if keep:
                        st.set(a, " ".join(keep))
                    else:
                        del st.attrib[a]
            if not any(st.get(a) for a in STEP_ATTRS):
                scheme.remove(st)
        if not scheme.findall("step"):
            doc.page.remove(scheme)
    for bg in list(doc.page.findall("bracketedgroup")):   # 括弧グループ
        ids = bg.get("BracketedObjectIDs", "").split()
        if ids and all(x in deleted for x in ids):
            doc.page.remove(bg)


def _clone_page_objects(doc, src_page, dst_page):
    els = doc.objects_by_page().get(src_page, [])
    return _add_clones(doc, els, dst_page - src_page)


def _add_clones(doc, els, dpages, prepare=None):
    """els を複製して doc に追加する。id・Z を付け替え、dpages ページ分ずらす。
    prepare(clone) があれば、ずらす前に各複製へ適用する（色・フォントの付け替えなど）。"""
    if not els:
        return 0
    next_id = doc.max_id() + 1
    zs = [int(e.get("Z")) for el in els for e in el.iter() if e.get("Z", "").isdigit()]
    zoff = (doc.max_z() + 1 - min(zs)) if zs else 0
    clones = [copy.deepcopy(el) for el in els]
    idmap = {}
    for c in clones:
        for e in c.iter():
            if e.get("id"):
                idmap[e.get("id")] = str(next_id)
                next_id += 1
    for c in clones:
        for e in c.iter():
            if e.get("id"):
                e.set("id", idmap[e.get("id")])
            if e.get("Z", "").isdigit():
                e.set("Z", str(int(e.get("Z")) + zoff))
            for k in REF_ATTRS:
                if e.get(k):
                    e.set(k, " ".join(idmap.get(x, x) for x in e.get(k).split()))
        if prepare:
            prepare(c)
        doc.shift_pages(c, dpages)
        doc.page.append(c)
    return len(clones)


# ---------------------------------------------------------------------------
# テンプレートファイル
# ---------------------------------------------------------------------------
class Template:
    """テンプレート用の CDXML（1ページ目に置いたオブジェクトを新しいページに配置する）。"""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)   # 表示名（GUI では登録時の元の名前）
        self.doc = Doc.load(path)
        els = self.doc.objects_by_page().get(1, [])
        if not els:
            raise CdxmlError("テンプレートの1ページ目にオブジェクトがありません: %s" % path)
        # 座標を持たない要素（反応スキーム情報など）は持ち込まない
        self.elements = els

    def size_warning(self, doc):
        if abs(self.doc.PW - doc.PW) > 1 or abs(self.doc.PH - doc.PH) > 1:
            return ("テンプレートのページサイズ (%.0f×%.0f pt) が編集中のファイル (%.0f×%.0f pt) と"
                    "異なります。位置はページ左上を基準に合わせています。"
                    % (self.doc.PW, self.doc.PH, doc.PW, doc.PH))
        return None


def _color_list(root):
    ct = root.find("colortable")
    return [] if ct is None else [(c.get("r"), c.get("g"), c.get("b")) for c in ct.findall("color")]


def _make_style_mapper(doc, tdoc):
    """テンプレート側の色番号・フォント番号を、編集中ファイルの番号に付け替える関数を返す。
    足りない色・フォントは編集中ファイルの表に追加する。"""
    dcolors = _color_list(doc.root)
    tcolors = _color_list(tdoc.root)
    cmap = {}

    def color_index(i):
        if i in cmap:
            return cmap[i]
        if i < 2 or i - 2 >= len(tcolors):      # 0=黒, 1=白 は固定
            cmap[i] = i
            return i
        rgb = tcolors[i - 2]
        try:
            target = tuple(round(float(x), 3) for x in rgb)
            j = next(k for k, c in enumerate(dcolors)
                     if tuple(round(float(x), 3) for x in c) == target)
        except (StopIteration, TypeError, ValueError):
            ct = doc.root.find("colortable")
            if ct is None:
                ct = ET.Element("colortable")
                doc.root.insert(0, ct)
            ET.SubElement(ct, "color", {"r": rgb[0], "g": rgb[1], "b": rgb[2]})
            dcolors.append(rgb)
            j = len(dcolors) - 1
        cmap[i] = j + 2
        return j + 2

    dft = doc.root.find("fonttable")
    tft = tdoc.root.find("fonttable")
    tfonts = {} if tft is None else {f.get("id"): f for f in tft.findall("font")}
    fmap = {}

    def font_id(fid):
        if fid in fmap:
            return fmap[fid]
        tf = tfonts.get(fid)
        if tf is None:
            fmap[fid] = fid
            return fid
        nonlocal dft
        if dft is None:
            dft = ET.Element("fonttable")
            ct = doc.root.find("colortable")
            doc.root.insert(1 if ct is not None else 0, dft)
        for f in dft.findall("font"):
            if f.get("name") == tf.get("name") and f.get("charset") == tf.get("charset"):
                fmap[fid] = f.get("id")
                return fmap[fid]
        used = {f.get("id") for f in dft.findall("font")}
        new = fid if fid not in used else str(max([int(x) for x in used if x.isdigit()] + [0]) + 1)
        nf = copy.deepcopy(tf)
        nf.set("id", new)
        dft.append(nf)
        fmap[fid] = new
        return new

    def apply(el):
        for e in el.iter():
            for k in ("color", "bgcolor"):
                v = e.get(k)
                if v and v.isdigit():
                    e.set(k, str(color_index(int(v))))
            if e.tag == "s" and e.get("font"):
                e.set("font", font_id(e.get("font")))
    return apply


def _place_template(doc, tpl, dst_page):
    tdoc = tpl.doc
    ox, oy = doc.page_origin(dst_page)
    tx, ty = tdoc.page_origin(1)
    mapper = _make_style_mapper(doc, tdoc)

    def prepare(c):
        mapper(c)
        translate(c, ox - tx, oy - ty)
    return _add_clones(doc, tpl.elements, 0, prepare=prepare)


def parse_pages(s):
    """'4,7-9' → [4,7,8,9]"""
    out = set()
    try:
        for part in s.replace("、", ",").replace("，", ",").split(","):
            part = part.strip().replace("〜", "-").replace("~", "-")
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                a, b = int(a), int(b)
                if a > b:
                    a, b = b, a
                out.update(range(a, b + 1))
            else:
                out.add(int(part))
    except ValueError:
        raise CdxmlError("ページの指定が読み取れません: 「%s」（例: 4 / 4,7-9）" % s)
    if not out:
        raise CdxmlError("ページを指定してください。")
    return sorted(out)


def insert_pages(doc, position, count=1, template=None, template_file=None):
    """position ページ目から count ページ挿入する。
    template: 複製するページ番号（挿入前の番号）／ template_file: Template オブジェクト"""
    total = doc.total_pages
    if count < 1:
        raise CdxmlError("挿入するページ数は 1 以上にしてください。")
    if not 1 <= position <= total + 1:
        raise CdxmlError("挿入位置は 1〜%d で指定してください。" % (total + 1))
    if template is not None and not 1 <= template <= total:
        raise CdxmlError("複製元のページは 1〜%d で指定してください。" % total)
    # 設定ページ数より後ろに挿入したときも、新しいページが設定範囲に入るようにする
    new_declared = max(doc.declared, position - 1) + count
    _remap_pages(doc, lambda p: p if p < position else p + count, new_declared)
    if template is not None:
        src = template if template < position else template + count
        for i in range(count):
            _clone_page_objects(doc, src, position + i)
        return "%dページ目に %dページ挿入しました（%dページ目の複製）。" % (position, count, template)
    if template_file is not None:
        for i in range(count):
            _place_template(doc, template_file, position + i)
        return "%dページ目に %dページ挿入しました（テンプレート: %s）。" % (
            position, count, template_file.name)
    return "%dページ目に空白ページを %dページ挿入しました。" % (position, count)


def delete_pages(doc, pages):
    if isinstance(pages, str):
        pages = parse_pages(pages)
    total = doc.total_pages
    bad = [p for p in pages if not 1 <= p <= total]
    if bad:
        raise CdxmlError("存在しないページです: %s（1〜%d）" % (", ".join(map(str, bad)), total))
    if len(pages) >= total:
        raise CdxmlError("すべてのページを削除することはできません。")
    dels = set(pages)

    def m(p):
        if p in dels:
            return None
        return p - sum(1 for d in dels if d < p)
    n = _remap_pages(doc, m, doc.declared - sum(1 for d in dels if d <= doc.declared))
    return "ページ %s を削除しました（オブジェクト %d 個）。" % (", ".join(map(str, pages)), n)


def move_page(doc, src, dst):
    total = doc.total_pages
    if not (1 <= src <= total and 1 <= dst <= total):
        raise CdxmlError("ページは 1〜%d で指定してください。" % total)
    if src == dst:
        return "移動元と移動先が同じなので、何もしませんでした。"

    def m(p):
        if p == src:
            return dst
        if src < dst and src < p <= dst:
            return p - 1
        if dst < src and dst <= p < src:
            return p + 1
        return p
    _remap_pages(doc, m, doc.declared)
    return "%dページ目を %dページ目へ移動しました。" % (src, dst)


# ---------------------------------------------------------------------------
# ページ番号
# ---------------------------------------------------------------------------
PRESETS = {   # 名前: (横位置, 縦位置)
    "右下": ("R", "B"), "中央下": ("C", "B"), "左下": ("L", "B"),
    "右上": ("R", "T"), "中央上": ("C", "T"), "左上": ("L", "T"),
}
PRESET_ALIASES = {
    "bottom-right": "右下", "bottom-center": "中央下", "bottom-left": "左下",
    "top-right": "右上", "top-center": "中央上", "top-left": "左上",
}


@dataclass
class NumberOptions:
    region: str = "右下"          # プリセット名、または "custom"
    width: float = 100.0          # プリセット領域の幅 (pt)
    height: float = 60.0          # プリセット領域の高さ (pt)
    custom: tuple = (0, 0, 0, 0)  # region == "custom" のときの x1,y1,x2,y2（ページ左上から, pt）
    first_page: int = 1           # 番号を振り始めるページ
    first_number: int = 1         # そのページに振る番号
    add_missing: bool = False     # 番号のないページにも追加する

    def rect(self, doc):
        r = PRESET_ALIASES.get(self.region, self.region)
        if r in PRESETS:
            w, h = self.width, self.height
            hx, vy = PRESETS[r]
            x1 = {"L": 0, "C": (doc.PW - w) / 2, "R": doc.PW - w}[hx]
            y1 = {"T": 0, "B": doc.PH - h}[vy]
            return [x1, y1, x1 + w, y1 + h]
        if r == "custom":
            x1, y1, x2, y2 = self.custom
            return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
        raise CdxmlError("ページ番号の領域「%s」は不明です。" % self.region)


def is_number_label(t):
    s = text_of(t).strip()
    return 0 < len(s) <= 12 and len(NUM_RE.findall(s)) == 1


def find_page_numbers(doc, rect):
    """{ページ: [<t> 要素, ...]}"""
    found = {}
    for el in doc.page:
        if el.tag != "t" or not is_number_label(el):
            continue
        p = doc.page_of(el)
        box = bbox_of(el)
        ox, oy = doc.page_origin(p)
        cx, cy = (box[0] + box[2]) / 2 - ox, (box[1] + box[3]) / 2 - oy
        if rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3]:
            found.setdefault(p, []).append(el)
    return found


def _set_number(t, n):
    for s in t.iter("s"):
        if s.text and NUM_RE.search(s.text):
            old = NUM_RE.search(s.text).group(0)
            new = str(n)
            if old == new:
                return False
            s.text = NUM_RE.sub(new, s.text, count=1)
            try:
                size = float(s.get("size", "12"))
            except ValueError:
                size = 12.0
            dw = (len(new) - len(old)) * DIGIT_EM * size
            box = nums(t.get("BoundingBox"))
            if box and dw:
                x1, y1, x2, y2 = box
                just = t.get("Justification") or t.get("CaptionJustification") or "Left"
                if just == "Right":
                    x1 -= dw
                elif just == "Center":
                    x1 -= dw / 2
                    x2 += dw / 2
                else:
                    x2 += dw
                t.set("BoundingBox", " ".join(fmt(v) for v in (x1, y1, x2, y2)))
            return True
    return False


def _new_number_label(doc, page, n, rect):
    """ファイル内に手本となるページ番号がないときに、新しく番号テキストを作る（右寄せ）。"""
    ft = doc.root.find("fonttable")
    font = ft.find("font") if ft is not None else None
    if font is None:
        if ft is None:
            ft = ET.Element("fonttable")
            doc.root.insert(0, ft)
        font = ET.SubElement(ft, "font", {"id": "3", "charset": "iso-8859-1", "name": "Arial"})
    size = doc.root.get("CaptionSize") or "12"
    ox, oy = doc.page_origin(page)
    x, y = ox + rect[2] - 24, oy + rect[3] - 15
    w = len(str(n)) * DIGIT_EM * float(size)
    t = ET.Element("t", {
        "id": str(doc.max_id() + 1), "p": "%s %s" % (fmt(x), fmt(y)),
        "BoundingBox": " ".join(fmt(v) for v in (x - w, y - float(size) * 0.83, x, y)),
        "Z": str(doc.max_z() + 1), "CaptionJustification": "Right", "Justification": "Right",
        "Warning": "Chemical Interpretation is not possible for this label"})
    s = ET.SubElement(t, "s", {"font": font.get("id"), "size": size, "color": "0"})
    s.text = str(n)
    return t


def renumber(doc, opts, add_pages=()):
    """ページ番号を振り直す。(メッセージ, 注意のリスト) を返す。
    add_pages: 番号がなければ追加するページ（挿入したページなど）。
    opts.add_missing が真なら、番号のないすべてのページに追加する。"""
    rect = opts.rect(doc)
    found = find_page_numbers(doc, rect)
    warnings = []
    if not found and not add_pages:
        warnings.append("指定した領域にページ番号が見つかりませんでした。領域の設定を確認してください。")
    changed, added = 0, 0
    sample = next((v[0] for v in found.values()), None)
    add_pages = set(add_pages)
    for p in range(1, doc.total_pages + 1):
        labels = found.get(p, [])
        if p < opts.first_page:
            if p in add_pages and not labels:
                warnings.append("%dページ目は番号を振り始めるページより前なので、番号を付けていません。" % p)
            continue
        n = opts.first_number + (p - opts.first_page)
        if len(labels) > 1:
            warnings.append("%dページ目の領域内に数字が %d 個あります（すべて書き換えました）。"
                            % (p, len(labels)))
        for t in labels:
            changed += _set_number(t, n)
        if labels or not (p in add_pages or (opts.add_missing and p <= doc.declared)):
            continue
        if sample is not None:
            c = copy.deepcopy(sample)
            c.set("id", str(doc.max_id() + 1))
            c.set("Z", str(doc.max_z() + 1))
            doc.shift_pages(c, p - doc.page_of(sample))
            _set_number(c, n)
        else:
            c = _new_number_label(doc, p, n, rect)
        doc.page.append(c)
        if sample is None:
            sample = c
        added += 1
    msg = "ページ番号を %d 件書き換えました" % changed
    if added:
        msg += "、%d 件追加しました" % added
    return msg + "。", warnings


# ---------------------------------------------------------------------------
# 一覧表示用
# ---------------------------------------------------------------------------
def page_summary(doc, rect):
    """[(ページ, オブジェクト数, 番号文字列, 見出し, 設定範囲内か)]"""
    found = find_page_numbers(doc, rect)
    by = doc.objects_by_page()
    rows = []
    for p in range(1, doc.total_pages + 1):
        els = by.get(p, [])
        nums_here = found.get(p, [])
        num = ",".join(text_of(t).strip() for t in nums_here)
        texts = sorted((e for e in els if e.tag == "t" and e not in nums_here),
                       key=lambda e: bbox_of(e)[1])
        title = text_of(texts[0]).strip().replace("\n", " ") if texts else ""
        rows.append((p, len(els), num, title, p <= doc.declared))
    return rows
