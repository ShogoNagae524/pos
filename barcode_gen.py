"""EAN-13 (JAN コード) バーコードを SVG で生成するプログラム。

外部ライブラリ不要。12 桁を渡すとチェックディジットを計算して 13 桁にする。
"""

from pathlib import Path

# 各数字のエンコードパターン (1 = 黒バー, 0 = 白スペース)
L_CODE = ["0001101", "0011001", "0010011", "0111101", "0100011",
          "0110001", "0101111", "0111011", "0110111", "0001011"]
G_CODE = ["0100111", "0110011", "0011011", "0100001", "0011101",
          "0111001", "0000101", "0010001", "0001001", "0010111"]
R_CODE = ["1110010", "1100110", "1101100", "1000010", "1011100",
          "1001110", "1010000", "1000100", "1001000", "1110100"]

# 先頭桁によって左側 6 桁の L/G の組み合わせが決まる
PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
          "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


def check_digit(digits12: str) -> str:
    """12 桁からモジュラス 10 / ウェイト 3 のチェックディジットを求める。"""
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(digits12))
    return str((10 - total % 10) % 10)


def encode(code: str) -> tuple[str, str]:
    """12 桁または 13 桁を受け取り、(13 桁コード, ビット列) を返す。"""
    if not code.isdigit() or len(code) not in (12, 13):
        raise ValueError("12 桁または 13 桁の数字を指定してください")
    if len(code) == 12:
        code += check_digit(code)
    elif code[-1] != check_digit(code[:12]):
        raise ValueError(f"チェックディジットが不正です: {code}")

    parity = PARITY[int(code[0])]
    bits = "101"  # スタートガード
    for p, d in zip(parity, code[1:7]):
        bits += (L_CODE if p == "L" else G_CODE)[int(d)]
    bits += "01010"  # センターガード
    for d in code[7:]:
        bits += R_CODE[int(d)]
    bits += "101"  # エンドガード
    return code, bits


def to_svg(code: str, bits: str, module: int = 3, height: int = 120) -> str:
    """ビット列を SVG 文字列に変換する。"""
    quiet = 11 * module  # 左右の余白
    width = quiet * 2 + len(bits) * module
    guard_positions = set(range(0, 3)) | set(range(45, 50)) | set(range(92, 95))

    rects = []
    for i, b in enumerate(bits):
        if b == "1":
            h = height + 10 if i in guard_positions else height  # ガードバーは長く
            rects.append(f'<rect x="{quiet + i * module}" y="10" '
                         f'width="{module}" height="{h}"/>')

    text_y = height + 28
    font = 'font-family="monospace" font-size="20" text-anchor="middle"'
    left_x = quiet + 24 * module
    right_x = quiet + 71 * module
    texts = (
        f'<text x="{quiet - 4 * module}" y="{text_y}" {font}>{code[0]}</text>'
        f'<text x="{left_x}" y="{text_y}" {font} letter-spacing="{module * 2}">{code[1:7]}</text>'
        f'<text x="{right_x}" y="{text_y}" {font} letter-spacing="{module * 2}">{code[7:]}</text>'
    )
    total_h = height + 40
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{total_h}" '
        f'viewBox="0 0 {width} {total_h}">'
        f'<rect width="100%" height="100%" fill="white"/>'
        f'<g fill="black">{"".join(rects)}</g>{texts}</svg>'
    )


def generate(code: str, out_dir: Path) -> Path:
    full, bits = encode(code)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{full}.svg"
    path.write_text(to_svg(full, bits), encoding="utf-8")
    return path


if __name__ == "__main__":
    # 異なる 3 つのバーコード (日本の JAN プレフィックス 45/49)
    samples = ["490123456789", "456995111617", "450000000001"]
    for s in samples:
        print("生成:", generate(s, Path(__file__).parent / "barcodes"))
