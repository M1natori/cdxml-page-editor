#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cdxml_pages.py — コマンド版

  cdxml_pages info     input.cdxml
  cdxml_pages insert   input.cdxml 5 [--count 3] [--template 11 | --template-file tpl.cdxml]
  cdxml_pages delete   input.cdxml 4,7-9
  cdxml_pages move     input.cdxml 8 3
  cdxml_pages renumber input.cdxml

結果は常に別ファイル（既定: 元の名前_edited.cdxml、-o で指定）に保存し、元のファイルは変更しない。
"""

import argparse
import sys

import cdxml_core as core


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cdxml_pages",
        description="ChemDraw (CDXML) のページ追加・削除・移動とページ番号の振り直し")
    ap.add_argument("--version", action="version", version=core.__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, writes=True):
        p.add_argument("input", help="入力 .cdxml ファイル")
        g = p.add_argument_group("ページ番号")
        g.add_argument("--region", default="右下",
                       help="番号を探す領域: 右下/中央下/左下/右上/中央上/左上"
                            "（bottom-right などの英語も可）、または x1,y1,x2,y2（ページ左上から pt）")
        g.add_argument("--region-size", default="100,60", help="プリセット領域の 幅,高さ (pt)")
        if writes:
            g.add_argument("--first-page", type=int, default=1, help="番号を振り始めるページ")
            g.add_argument("--first-number", type=int, help="そのページの番号（既定: ページ順そのまま）")
            g.add_argument("--add-missing", action="store_true", help="番号のないページにも追加")
            g.add_argument("--no-renumber", action="store_true", help="ページ番号を変更しない")
            p.add_argument("-o", "--output", help="出力ファイル（既定: 元の名前_edited.cdxml）")

    common(sub.add_parser("info", help="ページ構成とページ番号を表示"), writes=False)
    p = sub.add_parser("insert", help="ページを挿入")
    common(p)
    p.add_argument("position", type=int, help="新しいページが何ページ目になるか")
    p.add_argument("--count", type=int, default=1, help="挿入するページ数")
    p.add_argument("--template", type=int, help="このページ（挿入前の番号）を複製して挿入")
    p.add_argument("--template-file", help="テンプレート .cdxml（1ページ目のオブジェクトを新しいページに配置）")
    p = sub.add_parser("delete", help="ページを削除（例: 4 / 4,7-9）")
    common(p)
    p.add_argument("pages")
    p = sub.add_parser("move", help="ページを移動")
    common(p)
    p.add_argument("src", type=int)
    p.add_argument("dst", type=int)
    common(sub.add_parser("renumber", help="ページ番号だけを振り直す"))
    args = ap.parse_args(argv)

    try:
        w, h = (float(x) for x in args.region_size.split(","))
        opts = core.NumberOptions(width=w, height=h)
        if "," in args.region:
            opts.region = "custom"
            opts.custom = tuple(float(x) for x in args.region.split(","))
            if len(opts.custom) != 4:
                raise ValueError
        else:
            opts.region = args.region
        if args.cmd != "info":
            opts.first_page = args.first_page
            opts.first_number = args.first_number if args.first_number is not None else args.first_page
            opts.add_missing = args.add_missing
    except ValueError:
        sys.exit("--region / --region-size の指定が正しくありません。")

    try:
        doc = core.Doc.load(args.input)
        rect = opts.rect(doc)
        if args.cmd == "info":
            print("ページサイズ: %.2f × %.2f pt（%s方向に並ぶ）" %
                  (doc.PW, doc.PH, "縦" if doc.vertical else "横"))
            print("設定上のページ数: %d" % doc.declared)
            if doc.content_pages() > doc.declared:
                print("注意: %dページ目の位置までオブジェクトがあります（設定ページ数より後ろ）"
                      % doc.content_pages())
            print("ページ番号の領域: x %.1f–%.1f, y %.1f–%.1f pt（各ページ左上から）\n"
                  % (rect[0], rect[2], rect[1], rect[3]))
            print("ページ  オブジェクト数  ページ番号  先頭のテキスト")
            for p, n, num, title, inside in core.page_summary(doc, rect):
                print("%4d%s %10d  %10s  %s" % (p, " " if inside else "*", n, num or "-", title[:40]))
            return
        if args.cmd == "insert":
            tpl = core.Template(args.template_file) if args.template_file else None
            if tpl and tpl.size_warning(doc):
                print("注意: " + tpl.size_warning(doc))
            print(core.insert_pages(doc, args.position, args.count, args.template, tpl))
            added = range(args.position, args.position + args.count)
        elif args.cmd == "delete":
            print(core.delete_pages(doc, args.pages))
        elif args.cmd == "move":
            print(core.move_page(doc, args.src, args.dst))
        if args.cmd == "renumber" or not args.no_renumber:
            msg, warns = core.renumber(doc, opts, add_pages=added if args.cmd == "insert" else ())
            print(msg)
            for wmsg in warns:
                print("  注意: " + wmsg)
        out = args.output or core.default_output_path(args.input)
        doc.save_as(out)
        print("保存しました: %s" % out)
    except core.CdxmlError as e:
        sys.exit("エラー: %s" % e)


if __name__ == "__main__":
    main()
