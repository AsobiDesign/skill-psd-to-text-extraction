#!/usr/bin/env python3
"""PSDからテキストだけを取り出し、羅列版とHTML整形版の2つを書き出す。

依存は psd-tools>=1.17 のみ。ImageMagickもPhotoshopも要らないので、
WindowsでもmacOSでも同じ手順で動く。

使い方:
    python psd_text.py design.psd                  # 画面と件数の要約
    python psd_text.py design.psd -o out/          # 羅列版とHTML版を書き出す
    python psd_text.py design.psd --txt            # 羅列版だけ標準出力へ
    python psd_text.py design.psd --screen SP_top  # 画面を絞る
"""

import argparse
import html
import os
import re
import sys

from psd_tools import PSDImage
from psd_tools.constants import Tag

# ---------------------------------------------------------------- 出力の文字コード


def use_utf8_stdout():
    """標準出力をUTF-8に固定する。

    Windowsのコンソールは既定がcp932で、日本語混じりの出力をファイルへ
    リダイレクトすると UnicodeEncodeError で落ちる。書き出すファイルは
    encoding を明示しているが、標準出力だけは環境まかせになるため。
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------- 画面の切り出し

SP_HINT = re.compile(r"(^|[^a-z])(sp|smart\s*phone|smartphone|mobile|mb)([^a-z]|$)|スマホ|スマートフォン|モバイル", re.I)
PC_HINT = re.compile(r"(^|[^a-z])(pc|desktop|dt|web)([^a-z]|$)|デスクトップ|パソコン", re.I)
TABLET_HINT = re.compile(r"(^|[^a-z])(tab|tablet|ipad)([^a-z]|$)|タブレット", re.I)


def guess_device(name, width):
    if SP_HINT.search(name or ""):
        return "sp"
    if TABLET_HINT.search(name or ""):
        return "tablet"
    if PC_HINT.search(name or ""):
        return "pc"
    if width <= 500 or width in (750, 828, 1080):
        return "sp"
    if width <= 900:
        return "tablet"
    return "pc"


def artboard_rect(layer):
    blocks = getattr(layer, "tagged_blocks", None)
    if not blocks:
        return None
    for key in (Tag.ARTBOARD_DATA1, Tag.ARTBOARD_DATA2, Tag.ARTBOARD_DATA3):
        if key in blocks:
            try:
                rect = blocks.get_data(key)[b"artboardRect"]
                return (int(rect[b"Left"]), int(rect[b"Top "]),
                        int(rect[b"Rght"]), int(rect[b"Btom"]))
            except Exception:
                return tuple(layer.bbox)
    return None


def find_screens(psd):
    """アートボードで作られたPSDと、1枚のキャンバスに直接組まれたPSDの
    どちらでも「1画面ぶん」の単位を返す。"""
    artboards = []
    for layer in psd:
        rect = artboard_rect(layer)
        if rect:
            artboards.append((layer, rect))
    if artboards:
        return [{"layer": l, "name": str(l.name), "origin": (r[0], r[1]),
                 "width": r[2] - r[0], "height": r[3] - r[1], "source": "artboard"}
                for l, r in artboards], "artboard"

    groups = [l for l in psd if l.is_group() and l.visible and l.bbox != (0, 0, 0, 0)]
    hinted = [g for g in groups if SP_HINT.search(str(g.name)) or PC_HINT.search(str(g.name))]
    if len(hinted) >= 2:
        return [{"layer": g, "name": str(g.name), "origin": (g.bbox[0], g.bbox[1]),
                 "width": g.bbox[2] - g.bbox[0], "height": g.bbox[3] - g.bbox[1],
                 "source": "group"} for g in hinted], "group"

    return [{"layer": psd, "name": "canvas", "origin": (0, 0),
             "width": psd.width, "height": psd.height, "source": "canvas"}], "canvas"


# ---------------------------------------------------------------- テキストの書式

WEIGHT_TABLE = [
    ("extrablack", 950), ("ultrablack", 950),
    ("extrabold", 800), ("ultrabold", 800),
    ("semibold", 600), ("demibold", 600),
    ("extralight", 200), ("ultralight", 200),
    ("black", 900), ("heavy", 900),
    ("bold", 700), ("medium", 500), ("light", 300), ("thin", 100),
    ("regular", 400), ("normal", 400), ("roman", 400), ("book", 400),
]

IGNORED_FONTS = {"adobeinvisfont", "myriadpro"}

JUSTIFY = {0: "left", 1: "right", 2: "center", 3: "justify"}


def font_weight(ps_name):
    raw = str(ps_name or "").lower()
    for token, value in WEIGHT_TABLE:
        if token in raw:
            return value
    return 400


def to_hex(values):
    """FillColor.Values は [alpha, R, G, B]（0.0-1.0）。先頭がalphaなのが要注意。"""
    try:
        vals = [float(v) for v in values]
    except (TypeError, ValueError):
        return None
    if len(vals) >= 4:
        r, g, b = vals[1], vals[2], vals[3]
    elif len(vals) == 2:
        r = g = b = vals[1]
    else:
        return None
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, round(v * 255))) for v in (r, g, b)])


def read_text(layer):
    """テキストレイヤーから、文字列と代表的な書式を取り出す。

    書式は「見出しか本文か」を後で判断するために使う。1つのレイヤーで
    書式が混ざることもあるが、判定には支配的な（最初の）書式で足りる。
    """
    try:
        engine = layer.engine_dict
        fontset = layer.resource_dict.get("FontSet", [])
    except Exception:
        return None

    fonts = [str(f.get("Name", "")).strip("'\"") for f in fontset]
    text = layer.text.replace("\r", "\n").strip("\n")
    if not text.strip():
        return None

    scale = 1.0
    try:
        tr = layer.transform
        if tr and len(tr) >= 4:
            scale = round(float(tr[0]), 4) or 1.0
    except Exception:
        pass

    size, weight, color = 0.0, 400, None
    try:
        for style in engine["StyleRun"]["RunArray"]:
            data = style["StyleSheet"]["StyleSheetData"]
            idx = int(data.get("Font", 0))
            raw_font = fonts[idx] if 0 <= idx < len(fonts) else ""
            if re.sub(r"[^a-z0-9]", "", raw_font.lower()) in IGNORED_FONTS:
                continue
            size = round(float(data.get("FontSize", 0)) * scale, 1)
            weight = font_weight(raw_font)
            if data.get("FauxBold"):
                weight = max(weight, 700)
            fill = data.get("FillColor")
            color = to_hex(fill["Values"]) if fill else None
            break
    except Exception:
        pass

    align = "left"
    try:
        props = engine["ParagraphRun"]["RunArray"][0]["ParagraphSheet"]["Properties"]
        align = JUSTIFY.get(int(props.get("Justification", 0)), "left")
    except Exception:
        pass

    return {"text": text, "size": size, "weight": weight, "color": color, "align": align}


# ---------------------------------------------------------------- 収集

def collect_texts(screen):
    """画面配下のテキストを、所属セクションつきで集める。

    非表示グループ（開いたメニュー、ホバー時の表示など）は bbox が
    潰れて座標が読めないので、読む間だけ表示に切り替える。原稿としては
    それらも「そのページに載る文言」なので拾っておき、印だけ付ける。
    """
    ox, oy = screen["origin"]
    items = []

    def walk(layer, depth, section, hidden):
        # 表示状態は「開いて見せる」より前に確定させる。あとで評価すると
        # 自分で表示に切り替えた結果を拾ってしまい、非表示の印が消える。
        was_hidden = bool(hidden or not layer.visible)

        revealed = False
        if layer.is_group() and not layer.visible and layer.bbox == (0, 0, 0, 0):
            layer.visible = True
            revealed = True
        try:
            if layer.is_group():
                here = section
                if depth == 1 and layer.bbox != (0, 0, 0, 0):
                    here = str(layer.name)
                for child in layer:
                    walk(child, depth + 1, here, was_hidden)
                return
            if layer.kind != "type" or layer.bbox == (0, 0, 0, 0):
                return
            style = read_text(layer)
            if not style:
                return
            left, top, right, bottom = layer.bbox
            items.append({
                **style,
                "section": section or "(ルート)",
                "x": left - ox, "y": top - oy,
                "w": right - left, "h": bottom - top,
                "hidden": was_hidden,
            })
        finally:
            if revealed:
                layer.visible = False

    for child in screen["layer"]:
        walk(child, 1, None, False)
    return items


def dedupe(items, tolerance=8):
    """同じ文言がほぼ同じ位置に重なっているものを1つにまとめる。

    ホバー時の色違いを同じ場所に重ねておくのはよくある作りで、原稿として
    は1つで足りる。ただし座標は1〜2pxずれていることがあるため、位置を
    丸めたキーでは取りこぼす。近さで判定し、表示されている方を残す。
    """
    kept = []
    for item in sorted(items, key=lambda t: (t["hidden"], t["y"], t["x"])):
        if any(k["text"] == item["text"]
               and abs(k["x"] - item["x"]) <= tolerance
               and abs(k["y"] - item["y"]) <= tolerance
               for k in kept):
            continue
        kept.append(item)
    return kept


# ---------------------------------------------------------------- 並べ替えと構造推定

def to_lines(items):
    """上から下・左から右の読み順に並べ、同じ高さのものを1行にまとめる。"""
    lines = []
    for item in sorted(items, key=lambda t: (t["y"], t["x"])):
        placed = False
        for line in lines:
            top = max(item["y"], line[0]["y"])
            bottom = min(item["y"] + item["h"], line[0]["y"] + line[0]["h"])
            shorter = min(item["h"], line[0]["h"]) or 1
            # 縦の重なりが半分を超えるなら同じ行とみなす
            if bottom - top > shorter * 0.5:
                line.append(item)
                placed = True
                break
        if not placed:
            lines.append([item])
    for line in lines:
        line.sort(key=lambda t: t["x"])
    lines.sort(key=lambda line: min(t["y"] for t in line))
    return lines


def find_table_runs(lines):
    """行の並びの中から、表として扱える連続範囲を見つける。

    表の手がかりは「2列以上の行が2行以上続き、列の左端が揃っている」こと。
    カンプ上に罫線が引かれていなくても、座標が揃っていれば人は表として
    読むので、座標だけで判断する。
    """
    runs = []
    start = None
    for index, line in enumerate(lines + [[]]):
        multi = len(line) >= 2
        if multi and start is None:
            start = index
        elif not multi and start is not None:
            if index - start >= 2 and columns_align(lines[start:index]):
                runs.append((start, index))
            start = None
    return runs


def columns_align(rows):
    """行をまたいで列の左端が揃っているか。"""
    counts = {len(r) for r in rows}
    if len(counts) > 2:  # 列数がばらつきすぎる並びは表とみなさない
        return False
    widest = max(rows, key=len)
    tolerance = max(24, sum(t["h"] for t in widest) / len(widest) * 1.5)
    for row in rows:
        for cell in row:
            if not any(abs(cell["x"] - ref["x"]) <= tolerance for ref in widest):
                return False

    # 列に並べたとき空欄だらけになるなら、それは表ではなく
    # 「たまたま高さが揃って並んでいるバナー」などの飾り。
    columns = table_columns(rows)
    filled = 0
    for row in rows:
        filled += len({min(range(len(columns)), key=lambda i: abs(columns[i] - cell["x"]))
                       for cell in row})
    return filled >= len(rows) * len(columns) * 0.7


def heading_levels(items):
    """文字サイズから見出しレベルを決める。

    絶対値では決められない（PCとSPで同じ見出しでもサイズが違う）ので、
    その画面で一番多く使われているサイズを本文とみなし、そこから
    どれだけ大きいかで h2/h3 を割り当てる。
    """
    sizes = {}
    for item in items:
        if item["size"]:
            sizes[item["size"]] = sizes.get(item["size"], 0) + 1
    if not sizes:
        return 0, {}
    body = max(sizes, key=lambda s: (sizes[s], -s))
    larger = sorted({s for s in sizes if s > body * 1.15}, reverse=True)
    return body, {size: min(2 + rank, 4) for rank, size in enumerate(larger)}


def is_listy(line_group):
    """短い文字列が同じ左端で縦に等間隔に並んでいればリストとみなす。"""
    if len(line_group) < 3:
        return False
    xs = [line[0]["x"] for line in line_group]
    if max(xs) - min(xs) > 8:
        return False
    if any(len(line) != 1 for line in line_group):
        return False
    if any("\n" in line[0]["text"] or len(line[0]["text"]) > 30 for line in line_group):
        return False
    gaps = [line_group[i + 1][0]["y"] - line_group[i][0]["y"] for i in range(len(line_group) - 1)]
    if not gaps:
        return False
    return max(gaps) - min(gaps) <= max(6, sum(gaps) / len(gaps) * 0.35)


# ---------------------------------------------------------------- 出力

def build_screens(psd_path, screen_filter=None):
    psd = PSDImage.open(psd_path)
    screens, mode = find_screens(psd)
    result = []
    for screen in screens:
        if screen_filter and screen_filter.lower() not in screen["name"].lower():
            continue
        items = dedupe(collect_texts(screen))
        sections = []
        for item in items:
            if not sections or sections[-1]["name"] != item["section"]:
                if not any(s["name"] == item["section"] for s in sections):
                    sections.append({"name": item["section"], "items": []})
            for section in sections:
                if section["name"] == item["section"]:
                    section["items"].append(item)
                    break
        # セクションは画面上の位置順に並べる
        sections.sort(key=lambda s: min(i["y"] for i in s["items"]))
        result.append({
            "name": screen["name"],
            "device": guess_device(screen["name"], screen["width"]),
            "width": screen["width"], "height": screen["height"],
            "sections": sections,
            "count": len(items),
        })
    return result, mode


def render_txt(screens):
    """羅列版。読み順に文字列を並べるだけ。原稿としてそのまま渡せる形。"""
    out = []
    for screen in screens:
        out.append(f"{'=' * 60}")
        out.append(f"{screen['name']}  ({screen['device']} / {screen['width']}x{screen['height']})")
        out.append(f"{'=' * 60}")
        for section in screen["sections"]:
            out.append("")
            out.append(f"--- {section['name']} ---")
            shown = [i for i in section["items"] if not i["hidden"]]
            hidden = [i for i in section["items"] if i["hidden"]]

            for line in to_lines(shown):
                for item in line:
                    out.extend(item["text"].split("\n"))

            # タブの裏側や開いたメニューは、通常表示とは別の塊にして出す
            if hidden:
                out.append("")
                out.append("  ［別状態（PSDで非表示）］")
                for line in to_lines(hidden):
                    for item in line:
                        out.extend("  " + row for row in item["text"].split("\n"))
        out.append("")
    return "\n".join(out)


def esc(text):
    return html.escape(text).replace("\n", "<br>\n")


def render_flow(items, levels, body_size):
    """テキスト群を、表・リスト・見出し・段落に振り分けてHTMLにする。"""
    out = []
    lines = to_lines(items)
    tables = find_table_runs(lines)
    covered = {i for start, end in tables for i in range(start, end)}

    index = 0
    while index < len(lines):
        table = next((t for t in tables if t[0] == index), None)
        if table:
            out.append(render_table(lines[table[0]:table[1]]))
            index = table[1]
            continue

        # リストになりそうな連続を先に拾う
        run_end = index
        while (run_end < len(lines) and run_end not in covered
               and len(lines[run_end]) == 1):
            run_end += 1
        group = lines[index:run_end]
        if len(group) >= 3 and is_listy(group):
            out.append("<ul>")
            for line in group:
                out.append(f"<li>{esc(line[0]['text'])}</li>")
            out.append("</ul>")
            index = run_end
            continue

        for item in lines[index]:
            out.append(render_item(item, levels, body_size))
        index += 1
    return out


def render_html_body(screens):
    """HTML整形版。座標から役割を推し量ってタグを当てる。"""
    out = []
    for screen in screens:
        out.append(f'<section class="screen" data-device="{screen["device"]}">')
        out.append(f'<h1>{esc(screen["name"])} <small>{screen["device"]} / '
                   f'{screen["width"]}×{screen["height"]}</small></h1>')

        body_size, levels = heading_levels(
            [i for s in screen["sections"] for i in s["items"]])

        for section in screen["sections"]:
            out.append('<div class="block">')
            out.append(f'<p class="block__name">{esc(section["name"])}</p>')

            # 表示中のものと非表示のものは分けて組む。タブの裏側や開いた
            # メニューは表示中の要素と同じ座標に重なっているため、混ぜると
            # 行や列の判定が崩れる。原稿としても別々に読めたほうがよい。
            shown = [i for i in section["items"] if not i["hidden"]]
            hidden = [i for i in section["items"] if i["hidden"]]

            out.extend(render_flow(shown, levels, body_size))

            if hidden:
                out.append('<div class="alt">')
                out.append('<p class="alt__name">別状態（PSDで非表示のレイヤー）</p>')
                out.extend(render_flow(hidden, levels, body_size))
                out.append('</div>')
            out.append("</div>")
        out.append("</section>")
    return "\n".join(out)


def render_item(item, levels, body_size):
    text = esc(item["text"])
    attrs = ' class="hidden-src" title="PSDでは非表示のレイヤー"' if item["hidden"] else ""
    level = levels.get(item["size"])
    if level and "\n" not in item["text"] and len(item["text"]) <= 60:
        return f"<h{level}{attrs}>{text}</h{level}>"
    if item["weight"] >= 700 and len(item["text"]) <= 40 and "\n" not in item["text"]:
        return f"<p{attrs}><strong>{text}</strong></p>"
    return f"<p{attrs}>{text}</p>"


COLUMN_TOLERANCE = 24


def table_columns(rows):
    """列の位置を決める。

    いちばん列の多い行を物差しにするが、それだけだと物差しに無い位置の
    セルが隣のセルへ押し込まれて文字が繋がる。どの列にも当てはまらない
    セルが出たら、その位置に列を足して居場所を作る。
    """
    columns = sorted(cell["x"] for cell in max(rows, key=len))
    for row in rows:
        for cell in row:
            if not any(abs(cell["x"] - c) <= COLUMN_TOLERANCE for c in columns):
                columns.append(cell["x"])
    return sorted(columns)


def render_table(rows):
    """列位置を基準にセルを揃えて表にする。"""
    columns = table_columns(rows)

    out = ["<table>"]
    for position, row in enumerate(rows):
        cells = [""] * len(columns)
        for cell in row:
            nearest = min(range(len(columns)), key=lambda i: abs(columns[i] - cell["x"]))
            cells[nearest] = (cells[nearest] + "<br>" if cells[nearest] else "") + esc(cell["text"])
        # 見出し行と言えるのは「1行目だけが太く、続く行が細い」場合。
        # 全行が同じ太さ（お知らせ一覧など）は、1行目もデータとして扱う。
        head = (position == 0 and len(rows) > 1
                and all(c["weight"] >= 700 for c in row)
                and any(c["weight"] < 700 for c in rows[1]))
        tag = "th" if head else "td"
        out.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


HTML_SHELL = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: system-ui, "Hiragino Sans", "Noto Sans JP", sans-serif;
        line-height: 1.8; margin: 0 auto; padding: 24px; max-width: 900px; color: #222; }}
h1 {{ font-size: 20px; border-bottom: 2px solid #333; padding-bottom: 6px; margin-top: 48px; }}
h1 small {{ font-weight: normal; font-size: 13px; color: #777; }}
h2 {{ font-size: 22px; margin: 28px 0 10px; }}
h3 {{ font-size: 18px; margin: 22px 0 8px; }}
h4 {{ font-size: 16px; margin: 18px 0 6px; }}
p {{ margin: 6px 0; }}
ul {{ margin: 8px 0 8px 1.4em; }}
table {{ border-collapse: collapse; margin: 12px 0; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; vertical-align: top; }}
th {{ background: #f3f3f3; }}
.block {{ margin: 20px 0 28px; padding-left: 14px; border-left: 3px solid #e0e0e0; }}
.block__name {{ font-size: 12px; color: #999; letter-spacing: .04em; margin: 0 0 6px; }}
.hidden-src {{ opacity: .75; }}
.alt {{ margin: 14px 0 4px; padding: 10px 12px; background: #fffbe9; border: 1px dashed #e0c072; }}
.alt__name {{ font-size: 12px; color: #a4791f; margin: 0 0 6px; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def main():
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description="PSDからテキストを抽出する")
    parser.add_argument("psd")
    parser.add_argument("-o", "--outdir", help="羅列版とHTML版を書き出すディレクトリ")
    parser.add_argument("--txt", action="store_true", help="羅列版を標準出力へ")
    parser.add_argument("--html", action="store_true", help="HTML版を標準出力へ")
    parser.add_argument("--screen", help="画面名で絞り込む（部分一致）")
    args = parser.parse_args()

    screens, mode = build_screens(args.psd, args.screen)
    if not screens:
        print("該当する画面がありません")
        return 1

    if args.txt:
        print(render_txt(screens))
        return 0
    if args.html:
        print(HTML_SHELL.format(title=html.escape(os.path.basename(args.psd)), body=render_html_body(screens)))
        return 0

    base = os.path.splitext(os.path.basename(args.psd))[0]
    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)
        txt_path = os.path.join(args.outdir, f"{base}-text.txt")
        html_path = os.path.join(args.outdir, f"{base}-text.html")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(render_txt(screens))
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(HTML_SHELL.format(title=html.escape(base), body=render_html_body(screens)))
        print(f"羅列版   : {txt_path}")
        print(f"HTML版   : {html_path}")

    print(f"detect   : {mode}")
    for screen in screens:
        hidden = sum(1 for s in screen["sections"] for i in s["items"] if i["hidden"])
        note = f"（うち非表示 {hidden}）" if hidden else ""
        print(f"  [{screen['device']:6s}] {screen['name']:<12} "
              f"{screen['width']}x{screen['height']}  テキスト {screen['count']}件{note}  "
              f"セクション {len(screen['sections'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
