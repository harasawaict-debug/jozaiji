#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""常在寺サイト用 画像一括加工スクリプト

ローカルの HTML ツール（寺宝画像加工ツール.html）の処理を Pillow に移植したもの。
リサイズ・五三桐の透かし・WebP 変換・Exif 除去をまとめて行う。

使い方（ディレクトリ単位）:
    python scripts/watermark.py --input photos-original --output assets/img \
        --size 1000 --opacity 0.18 --position center

使い方（分類マニフェストで絞り込む）:
    python scripts/watermark.py --map scripts/photo-map.tsv --category D2 \
        --input photos-original --output assets/img --size 1000 --opacity 0.10

透かしを入れない分類:
    python scripts/watermark.py --map scripts/photo-map.tsv --category B \
        --input photos-original --output assets/img --size 1600 --no-watermark
"""

import argparse
import csv
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageFilter, ImageOps, ImageStat

# HTML ツールから引き継いだ既定値。見た目を揃えるため数値は変えないこと。
DEFAULT_WATERMARK = Path(__file__).parent / "assets" / "watermark-gosankiri.png"
DEFAULT_SIZE = 1000
DEFAULT_OPACITY = 0.18
DEFAULT_QUALITY = 82

# 透かしの色は素材の色を使わず、重なる領域の明るさで決める。
# 素材 PNG はアルファチャンネルだけをマスクとして使う。
COLOR_INK = (0x1A, 0x16, 0x10)     # 墨色。明るい被写体の上に置く
COLOR_PAPER = (0xF5, 0xF0, 0xE8)   # 生成り。暗い被写体の上に置く
DEFAULT_THRESHOLD = 0.5            # この輝度より明るければ墨色
DEFAULT_OUTLINE = "auto"           # 紋の縁取りの太さ。auto は長辺÷500（1〜4px）
OUTLINE_DIVISOR = 500
OUTLINE_MIN = 1
OUTLINE_MAX = 4

# 自己検証：紋の内側の平均差分が「理論値 × この比率」を下回ったらエラーで止める。
# 合成が効いていない事故（アルファの二重適用など）をここで捕まえる。
DEFAULT_MIN_DIFF_RATIO = 0.5

# 透かしの寸法・配置（長辺に対する比率）
SCALE_CENTER = 0.28
SCALE_BR = 0.28
SCALE_TILE = 0.20
MARGIN_BR = 0.04
TILE_GAP = 1.7

SOURCE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


def load_map(map_path, category):
    """photo-map.tsv から {元ファイル名: 出力スラッグ} を作る。"""
    selected = {}
    with open(map_path, encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh, delimiter="\t"):
            if not row or row[0].startswith("#"):
                continue
            name, cat, slug = row[0], row[1], row[2]
            if cat != category:
                continue
            if slug == "-":
                continue
            selected[name] = slug
    return selected


def resize_long_edge(img, size):
    """長辺を size に合わせる。元が小さければ拡大せずそのまま。"""
    long_edge = max(img.size)
    if long_edge <= size:
        return img
    ratio = size / long_edge
    new_size = (max(1, round(img.width * ratio)), max(1, round(img.height * ratio)))
    return img.resize(new_size, Image.LANCZOS)


def scaled_mask(mark, target_width):
    """透かしのアルファチャンネルだけを指定幅に縮小して返す。縦横比は保つ。"""
    width = max(1, int(round(target_width)))
    height = max(1, int(round(mark.height * width / mark.width)))
    return mark.resize((width, height), Image.LANCZOS).getchannel("A")


def region_luma(base, box, mask):
    """透かしが重なる部分の平均輝度を 0〜1 で返す。紋の形の内側だけを測る。

    画像の外にはみ出す分（tile の端）は測定から除く。測る画素が無ければ None。
    """
    x0, y0, x1, y1 = box
    ix0, iy0 = max(0, x0), max(0, y0)
    ix1, iy1 = min(base.width, x1), min(base.height, y1)
    if ix1 <= ix0 or iy1 <= iy0:
        return None

    region = base.crop((ix0, iy0, ix1, iy1)).convert("L")
    sub = mask.crop((ix0 - x0, iy0 - y0, ix1 - x0, iy1 - y0))
    # 紋の輪郭のぼけを拾わないよう、しっかり乗る画素だけを測る
    binary = sub.point(lambda v: 255 if v > 128 else 0)
    if not binary.getbbox():
        return None
    return ImageStat.Stat(region, mask=binary).mean[0] / 255.0


def pick_color(base, box, mask, threshold, forced):
    """(色, 測った輝度) を返す。色を固定しているときの輝度は None。"""
    luma = region_luma(base, box, mask)
    if forced is not None:
        return forced, luma
    if luma is None:
        return COLOR_INK, None
    return (COLOR_INK if luma > threshold else COLOR_PAPER), luma


class WatermarkError(RuntimeError):
    """透かしが乗っていないと判定したときに投げる。"""


def placements(img, mark, position):
    """(左上座標, 紋のマスク) を順に返す。"""
    long_edge = max(img.size)

    if position == "center":
        mask = scaled_mask(mark, long_edge * SCALE_CENTER)
        yield ((img.width - mask.width) // 2, (img.height - mask.height) // 2), mask

    elif position == "br":
        mask = scaled_mask(mark, long_edge * SCALE_BR)
        margin = int(round(long_edge * MARGIN_BR))
        yield (img.width - mask.width - margin, img.height - mask.height - margin), mask

    elif position == "tile":
        mask = scaled_mask(mark, long_edge * SCALE_TILE)
        gap_x = max(1, int(round(mask.width * TILE_GAP)))
        gap_y = max(1, int(round(mask.height * TILE_GAP)))
        row = 0
        y = -mask.height
        while y < img.height:
            # 奇数行は間隔の半分だけ横にずらす
            offset = gap_x // 2 if row % 2 else 0
            x = -mask.width + offset
            while x < img.width:
                yield (x, y), mask
                x += gap_x
            y += gap_y
            row += 1

    else:
        raise ValueError("unknown position: %s" % position)


def outline_width(long_edge, spec):
    """縁取りの太さを px で返す。auto なら長辺÷500 を 1〜4px に収める。"""
    if spec == "auto":
        return max(OUTLINE_MIN, min(OUTLINE_MAX, int(round(long_edge / OUTLINE_DIVISOR))))
    return max(0, int(spec))


def outline_mask(mask, width):
    """紋の外側に width px の縁だけを残したマスクを作る。"""
    grown = mask.filter(ImageFilter.MaxFilter(width * 2 + 1))
    return ImageChops.subtract(grown, mask)


def luma_of(color):
    r, g, b = color
    return 0.299 * r + 0.587 * g + 0.114 * b


def apply_watermark(img, mark, position, opacity, threshold, forced, outline_spec):
    """透かしを乗せた RGB 画像・使った色・検証用の情報を返す。

    紋は「元画像に直接」貼る。中間レイヤーにマスク付きで貼ってから合成すると
    アルファが二重に掛かるので、その形は使わない。
    """
    work = img.copy()
    body_cover = Image.new("L", img.size, 0)   # 紋の内側（検証に使う）
    target_l = Image.new("L", img.size, 0)     # 紋が完全不透明なら何色になるか
    used = []
    outline_px = outline_width(max(img.size), outline_spec)

    for (x, y), mask in placements(img, mark, position):
        box = (x, y, x + mask.width, y + mask.height)
        # 色の判定は、まだ何も乗っていない元画像で行う
        color, luma = pick_color(img, box, mask, threshold, forced)
        used.append((color, luma))

        body_alpha = mask.point(lambda v: int(round(v * opacity)))

        if outline_px > 0:
            edge = outline_mask(mask, outline_px)
            edge_color = COLOR_INK if color == COLOR_PAPER else COLOR_PAPER
            work.paste(Image.new("RGB", mask.size, edge_color), box,
                       edge.point(lambda v: int(round(v * opacity))))

        work.paste(Image.new("RGB", mask.size, color), box, body_alpha)

        binary = mask.point(lambda v: 255 if v > 128 else 0)
        body_cover.paste(255, box, binary)
        target_l.paste(int(round(luma_of(color))), box, binary)

    return work, used, body_cover, target_l


def verify(before, after, body_cover, target_l, opacity, min_ratio):
    """紋の内側の平均差分を測り、理論値に届いていなければエラーにする。

    戻り値は (実測の平均差分, 理論値)。
    """
    if not body_cover.getbbox():
        raise WatermarkError("紋のマスクが空です")

    before_l = before.convert("L")
    actual = ImageStat.Stat(
        ImageChops.difference(before_l, after.convert("L")), mask=body_cover).mean[0]
    # 理論値：紋の色と元画像の明るさの差に不透明度を掛けたもの
    expected = opacity * ImageStat.Stat(
        ImageChops.difference(before_l, target_l), mask=body_cover).mean[0]

    if actual < min_ratio * expected:
        raise WatermarkError(
            "透かしが乗っていません。マスク内平均差分 %.2f（理論値 %.2f の %.0f%% 未満）"
            % (actual, expected, min_ratio * 100))
    return actual, expected


def process(src, dst, size, opacity, position, quality, use_watermark, mark,
            threshold=DEFAULT_THRESHOLD, forced=None, outline_spec=DEFAULT_OUTLINE,
            min_ratio=DEFAULT_MIN_DIFF_RATIO):
    used = []
    diff = None
    with Image.open(src) as raw:
        # Exif の回転を反映してから Exif ごと捨てる
        img = ImageOps.exif_transpose(raw)
        img = img.convert("RGB")
        img = resize_long_edge(img, size)

        if use_watermark:
            # 透かしなしの状態を持っておき、あとで差分を測る
            plain = img
            img, used, body_cover, target_l = apply_watermark(
                img, mark, position, opacity, threshold, forced, outline_spec)
            # 検証に通らなければ保存しない
            diff, _expected = verify(plain, img, body_cover, target_l, opacity, min_ratio)

        # Exif を持ち込まないよう、画素だけを新しい Image に移す
        clean = Image.new("RGB", img.size)
        clean.paste(img)

        dst.parent.mkdir(parents=True, exist_ok=True)
        clean.save(dst, "WEBP", quality=quality, method=6)
        return img.size, used, diff


def describe_tint(used):
    """透かしの色と、判定に使った輝度を1行にまとめる。"""
    if not used:
        return "-"
    colors = [c for c, _ in used]
    lumas = [l for _, l in used if l is not None]
    avg = ("%.2f" % (sum(lumas) / len(lumas))) if lumas else "-"

    names = set()
    for c in colors:
        names.add("墨色" if c == COLOR_INK else "生成り" if c == COLOR_PAPER else "固定色")
    if len(names) == 1:
        return "%s (輝度 %s)" % (names.pop(), avg)
    ink = sum(1 for c in colors if c == COLOR_INK)
    return "混在 墨%d/生%d (輝度 %s)" % (ink, len(colors) - ink, avg)


def main(argv=None):
    p = argparse.ArgumentParser(description="画像のリサイズ・五三桐透かし・WebP 変換")
    p.add_argument("--input", required=True, help="入力ディレクトリ")
    p.add_argument("--output", required=True, help="出力ディレクトリ")
    p.add_argument("--size", type=int, default=DEFAULT_SIZE,
                   help="長辺の画素数。HTML ツールの選択肢は 800 / 1000 / 1200"
                        "（ヒーロー用の 2000 など任意の値も指定できる）。既定 %d" % DEFAULT_SIZE)
    p.add_argument("--opacity", type=float, default=DEFAULT_OPACITY,
                   help="透かしの不透明度。既定 %.2f" % DEFAULT_OPACITY)
    p.add_argument("--position", choices=["center", "br", "tile"], default="center",
                   help="透かしの配置。既定 center")
    p.add_argument("--no-watermark", action="store_true", help="透かしを入れない")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                   help="透かしの色を切り替える輝度のしきい値（0〜1）。"
                        "重なる領域がこれより明るければ墨色 #1A1610、暗ければ生成り #F5F0E8。"
                        "既定 %.2f" % DEFAULT_THRESHOLD)
    p.add_argument("--mon-color", default=None,
                   help="紋の色を固定する（検証用。例 '#1A1610'）。既定は明るさによる自動切替")
    p.add_argument("--outline", default=DEFAULT_OUTLINE,
                   help="紋の縁取りの太さ。'auto'（既定）は長辺÷%d を %d〜%dpx に収める。"
                        "数値を渡せば固定、0 で縁取りなし。"
                        "本体が生成りなら縁は墨色、逆も同様。"
                        % (OUTLINE_DIVISOR, OUTLINE_MIN, OUTLINE_MAX))
    p.add_argument("--min-diff-ratio", type=float, default=DEFAULT_MIN_DIFF_RATIO,
                   help="自己検証のしきい値。紋の内側の平均差分が理論値のこの割合を"
                        "下回ったらエラーで停止する。既定 %.2f" % DEFAULT_MIN_DIFF_RATIO)
    p.add_argument("--quality", type=int, default=DEFAULT_QUALITY,
                   help="WebP 品質。既定 %d" % DEFAULT_QUALITY)
    p.add_argument("--watermark", default=str(DEFAULT_WATERMARK), help="透かし PNG のパス")
    p.add_argument("--map", help="分類マニフェスト（TSV: 元ファイル名/分類/スラッグ/説明）")
    p.add_argument("--category", help="--map と併用。この分類だけを処理する")
    p.add_argument("--suffix", default="", help="出力名の末尾に付ける文字列（例: -1200）")
    p.add_argument("--only", nargs="*", help="元ファイル名を挙げて、その分だけ処理する")
    p.add_argument("--dry-run", action="store_true", help="変換せず対象だけ表示する")
    p.add_argument("--report", help="変換結果を TSV で書き出すパス（追記）")
    args = p.parse_args(argv)

    in_dir = Path(args.input)
    out_dir = Path(args.output)
    if not in_dir.is_dir():
        p.error("入力ディレクトリがありません: %s" % in_dir)

    if args.map and not args.category:
        p.error("--map を使うときは --category も指定してください")

    # 処理対象と出力名を決める
    if args.map:
        slugs = load_map(args.map, args.category)
        targets = []
        for name, slug in sorted(slugs.items()):
            src = in_dir / name
            if not src.exists():
                print("  見つかりません: %s" % name, file=sys.stderr)
                continue
            targets.append((src, slug))
    else:
        targets = [(f, f.stem) for f in sorted(in_dir.iterdir())
                   if f.suffix.lower() in SOURCE_SUFFIXES]

    if args.only:
        wanted = set(args.only)
        targets = [t for t in targets if t[0].name in wanted]

    if not targets:
        print("対象がありません。")
        return 1

    use_watermark = not args.no_watermark
    mark = None
    forced = None
    if use_watermark:
        mark_path = Path(args.watermark)
        if not mark_path.exists():
            p.error("透かし PNG がありません: %s" % mark_path)
        mark = Image.open(mark_path).convert("RGBA")
        if args.mon_color:
            try:
                forced = ImageColor.getrgb(args.mon_color)[:3]
            except ValueError:
                p.error("色として読めません: %s" % args.mon_color)

    if not use_watermark:
        label = "透かしなし"
    elif forced:
        label = "透かし %s %.2f 色固定 #%02X%02X%02X" % ((args.position, args.opacity) + forced)
    else:
        label = "透かし %s %.2f 自動配色（しきい値 %.2f）" % (args.position, args.opacity, args.threshold)
    if use_watermark:
        if args.outline == "auto":
            label += "／縁取り auto（長辺÷%d, %d〜%dpx）" % (OUTLINE_DIVISOR, OUTLINE_MIN, OUTLINE_MAX)
        elif int(args.outline) > 0:
            label += "／縁取り %dpx" % int(args.outline)
        else:
            label += "／縁取りなし"
    print("対象 %d 件／長辺 %dpx／%s／WebP q%d" % (len(targets), args.size, label, args.quality))
    print("%-24s %-28s %9s %9s %6s %-10s %-22s %s"
          % ("元ファイル", "出力", "変換前", "変換後", "比", "寸法", "紋の色", "マスク内平均差分"))
    print("-" * 126)

    report = None
    report_writer = None
    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        new = not rp.exists()
        report = open(rp, "a", encoding="utf-8", newline="")
        report_writer = csv.writer(report, delimiter="\t", lineterminator="\n")
        if new:
            report_writer.writerow(["元ファイル", "出力", "分類", "長辺", "寸法",
                                    "変換前bytes", "変換後bytes", "紋の色", "輝度",
                                    "マスク内平均差分"])

    total_before = total_after = 0
    for src, slug in targets:
        dst = out_dir / (slug + args.suffix + ".webp")
        if args.dry_run:
            print("%-24s %-30s" % (src.name, dst.name))
            continue

        before = src.stat().st_size
        try:
            size_px, used, diff = process(src, dst, args.size, args.opacity, args.position,
                                          args.quality, use_watermark, mark,
                                          args.threshold, forced, args.outline,
                                          args.min_diff_ratio)
        except WatermarkError as err:
            print()
            print("エラー: %s" % src.name, file=sys.stderr)
            print("  %s" % err, file=sys.stderr)
            print("  このファイルは出力していません。処理を中止します。", file=sys.stderr)
            return 2
        after = dst.stat().st_size
        total_before += before
        total_after += after

        tint = describe_tint(used)

        print("%-24s %-28s %7.1fKB %7.1fKB %5.1f%% %-10s %-22s %s"
              % (src.name, dst.name, before / 1024, after / 1024,
                 after / before * 100, "%dx%d" % size_px, tint,
                 "%.2f" % diff if diff is not None else "-"))

        if report:
            colors = [c for c, _ in used]
            lumas = [l for _, l in used if l is not None]
            if not colors:
                cname, lval = "なし", ""
            else:
                names = {"墨色" if c == COLOR_INK else "生成り" if c == COLOR_PAPER
                         else "固定色" for c in colors}
                cname = names.pop() if len(names) == 1 else "混在"
                lval = "%.3f" % (sum(lumas) / len(lumas)) if lumas else ""
            report_writer.writerow([
                src.name, dst.name, args.category or "", str(args.size),
                "%dx%d" % size_px, str(before), str(after),
                cname, lval, "%.2f" % diff if diff is not None else ""])

    if report:
        report.close()

    if not args.dry_run:
        print("-" * 100)
        print("合計 %d 件  %.1fMB → %.1fMB"
              % (len(targets), total_before / 1048576, total_after / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
