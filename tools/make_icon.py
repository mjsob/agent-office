"""실행: python3 tools/make_icon.py payload/office.ico
Office 아이콘을 크기마다 픽셀 격자에 맞춰 다시 그려요. 256 단위 원본 도안을 크기별로 맞춰요."""
import sys
from PIL import Image, ImageDraw
BG, FG = (29, 29, 27, 255), (255, 255, 255, 255)
SS = 8   # 가장자리만 부드럽게 하려고 8배로 그린 뒤 줄여요

def layout(n):
    """크기 n의 도형 좌표(픽셀). 작은 크기는 따로 손으로 맞춰요."""
    if n in MANUAL:
        return MANUAL[n]
    s = n / 256
    t = max(1, round(16 * s))
    L = round(72 * s); R = n - L; T = round(48 * s); B = round(208 * s)
    ww, wh = max(1, round(24 * s)), max(1, round(16 * s))
    gap = max(1, round(8 * s))   # 창 두 칸 사이는 가운데 기준으로 띄워서 붙지 않게 해요
    if (n - gap) % 2: gap += 1
    wx = (n - gap) // 2 - ww; wy1 = round(80 * s); wy2 = max(round(112 * s), wy1 + wh + 1)
    dw = round(40 * s)
    if (n - dw) % 2: dw += 1   # 문도 가운데에 딱 맞게
    dl = (n - dw) // 2
    sw = max(1, round(8 * s));
    if (dw - sw) % 2: sw += 1
    return dict(r=56 * s, t=t, box=(L, T, R, B), br=8 * s,
                wins=[(wx, wy1, ww, wh), (n - wx - ww, wy1, ww, wh), (wx, wy2, ww, wh), (n - wx - ww, wy2, ww, wh)],
                wr=wh / 2 if wh >= 3 else 0,
                door=(dl, round(160 * s), dl + dw, B), slot=((n - sw) // 2, round(176 * s), (n + sw) // 2, max(round(192 * s), round(176 * s) + 1)))

MANUAL = {
    # 16px: 건물 테두리 1px, 창 1x1 넷, 문은 2px 덩어리
    16: dict(r=3.5, t=1, box=(4, 2, 12, 14), br=0, wins=[(6, 4, 1, 1), (9, 4, 1, 1), (6, 6, 1, 1), (9, 6, 1, 1)], wr=0,
             door=(7, 10, 9, 14), slot=None),
    # 20px
    20: dict(r=4.5, t=1, box=(5, 3, 15, 17), br=0, wins=[(7, 5, 2, 1), (11, 5, 2, 1), (7, 8, 2, 1), (11, 8, 2, 1)], wr=0,
             door=(8, 12, 12, 17), slot=(9, 14, 11, 16)),
    # 24px: 100% 배율 작업 표시줄 크기
    24: dict(r=5.25, t=2, box=(6, 4, 18, 20), br=1, wins=[(9, 7, 2, 2), (13, 7, 2, 2), (9, 10, 2, 2), (13, 10, 2, 2)], wr=0,
             door=(10, 14, 14, 20), slot=(11, 16, 13, 18)),
}

def render(n):
    g = layout(n); k = SS
    im = Image.new("RGBA", (n * k, n * k), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, n * k - 1, n * k - 1), radius=g["r"] * k, fill=BG)
    L, T, R, B = g["box"]; t = g["t"]; br = g["br"] * k
    d.rounded_rectangle((L * k, T * k, R * k - 1, B * k - 1), radius=br, fill=FG)
    d.rounded_rectangle(((L + t) * k, (T + t) * k, (R - t) * k - 1, (B - t) * k - 1), radius=max(0, br - t * k) if br else 0, fill=BG)
    for x, y, w, h in g["wins"]:
        d.rounded_rectangle((x * k, y * k, (x + w) * k - 1, (y + h) * k - 1), radius=g["wr"] * k, fill=FG)
    x0, y0, x1, y1 = g["door"]; d.rectangle((x0 * k, y0 * k, x1 * k - 1, y1 * k - 1), fill=FG)
    if g["slot"]:
        x0, y0, x1, y1 = g["slot"]; d.rectangle((x0 * k, y0 * k, x1 * k - 1, y1 * k - 1), fill=BG)
    return im.reduce(k)

def bmp_frame(im):
    """256보다 작은 크기는 오래된 Windows 읽기 경로도 읽도록 32비트 BMP(DIB)로 넣어요."""
    import struct
    w, h = im.size
    px = im.convert("RGBA").tobytes("raw", "BGRA")
    rows = [px[y * w * 4:(y + 1) * w * 4] for y in range(h)][::-1]   # 아래에서 위로
    mask_row = ((w + 31) // 32) * 4
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, w * h * 4 + mask_row * h, 0, 0, 0, 0)
    return header + b"".join(rows) + b"\0" * (mask_row * h)

def write_ico(out, frames):
    import io, struct
    blobs = []
    for n, im in frames.items():
        if n >= 256:
            buf = io.BytesIO(); im.save(buf, "PNG"); blobs.append((n, buf.getvalue()))
        else:
            blobs.append((n, bmp_frame(im)))
    head = struct.pack("<HHH", 0, 1, len(blobs)); offset = 6 + 16 * len(blobs); entries = b""
    for n, data in blobs:
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset); offset += len(data)
    with open(out, "wb") as f:
        f.write(head + entries + b"".join(data for _, data in blobs))

SIZES = (16, 20, 24, 30, 32, 36, 40, 48, 64, 256)
if __name__ == "__main__":
    out = sys.argv[1]
    frames = {n: render(n) for n in SIZES}
    write_ico(out, frames)
    print("ok", out)
