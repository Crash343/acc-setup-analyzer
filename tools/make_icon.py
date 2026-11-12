"""Erzeugt das Programm-Icon: Rennstrecken-Kurve von oben mit Gummiabrieb auf der Ideallinie und rot-weißem Curb.

    python tools/make_icon.py      -> accanalyzer/assets/icon.ico (16–256 px) und icon.png (256 px)
Benötigt Pillow (kommt mit matplotlib).
"""
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024                          # Zeichenauflösung (wird runterskaliert -> glatte Kanten)
OUT = Path(__file__).resolve().parent.parent / "accanalyzer" / "assets"


def ring_points(cx, cy, r, a0, a1, n=200):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def band(draw, cx, cy, r_in, r_out, a0, a1, fill):
    pts = ring_points(cx, cy, r_out, a0, a1) + ring_points(cx, cy, r_in, a1, a0)
    draw.polygon(pts, fill=fill)


def make() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # Hintergrund: Wiese mit leichtem Verlauf, abgerundetes Quadrat
    bg = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(bg)
    for y in range(S):
        t = y / S
        gd.line([(0, y), (S, y)], fill=(int(70 + 25 * t), int(150 - 30 * t), int(60 + 10 * t), 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=190, fill=255)
    img.paste(bg, (0, 0), mask)

    d = ImageDraw.Draw(img)
    # Haarnadel von oben: Strecke kommt links unten herein, läuft oben um den Scheitel und rechts unten hinaus
    cx, cy = S * 0.5, S * 0.60
    r_in, r_out = S * 0.17, S * 0.44
    a0, a1 = 180, 360                       # Winkel (Grad, im Uhrzeigersinn ab rechts, y nach unten): obere Hälfte
    asphalt, line = (96, 98, 104, 255), (240, 240, 240, 255)
    lw = S * 0.028
    band(d, cx, cy, r_out, r_out + lw, a0, a1, line)                       # weiße Außenlinie
    band(d, cx, cy, r_in - lw, r_in, a0, a1, line)                         # weiße Innenlinie
    band(d, cx, cy, r_in, r_out, a0, a1, asphalt)                          # Asphalt Kurve
    for sgn in (-1, 1):                                                    # zwei Geraden nach unten
        xo, xi = cx + sgn * r_out, cx + sgn * r_in
        d.rectangle([min(xo, xi), cy, max(xo, xi), S], fill=asphalt)
        d.rectangle([min(xo, xo + sgn * lw), cy, max(xo, xo + sgn * lw), S], fill=line)
        d.rectangle([min(xi, xi - sgn * lw), cy, max(xi, xi - sgn * lw), S], fill=line)

    # Gummiabrieb: viele dunkle, halbtransparente Spuren entlang der Ideallinie
    # (links außen herein -> Scheitel innen -> rechts außen hinaus)
    rub = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rub)
    rnd = random.Random(7)
    wide = r_out - S * 0.07
    for k in range(110):
        off = rnd.gauss(0, S * 0.016)
        alpha = rnd.randint(60, 120)
        width = rnd.randint(int(S * 0.010), int(S * 0.024))
        pts = [(cx - wide - off, S), (cx - wide - off, cy)]
        for i in range(201):
            u = i / 200
            a = a0 + (a1 - a0) * u
            apex = math.sin(math.pi * u) ** 1.3                     # 1 am Scheitelpunkt
            r = wide - (r_out - r_in - S * 0.12) * apex + off
            pts.append((cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))))
        pts += [(cx + wide + off, cy), (cx + wide + off, S)]
        rd.line(pts, fill=(12, 12, 14, alpha), width=width, joint="curve")
    rub = rub.filter(ImageFilter.GaussianBlur(S * 0.005))
    img.alpha_composite(rub)

    d = ImageDraw.Draw(img)
    red, white = (214, 32, 39, 255), (250, 250, 250, 255)

    def kerb(r1, r2, b0, b1, seg=9.0):
        a, i = b0, 0
        while a < b1:
            band(d, cx, cy, r1, r2, a, min(a + seg, b1), red if i % 2 == 0 else white)
            a += seg
            i += 1
    kerb(r_in - S * 0.08, r_in + S * 0.005, 215, 325, seg=10)          # innen am Scheitelpunkt
    kerb(r_out - S * 0.005, r_out + S * 0.065, a0, a0 + 50, seg=8)    # außen am Eingang
    kerb(r_out - S * 0.005, r_out + S * 0.065, a1 - 50, a1, seg=8)    # außen am Ausgang

    # alles außerhalb des abgerundeten Quadrats wieder transparent
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    big = make()
    png = big.resize((256, 256), Image.LANCZOS)
    png.save(OUT / "icon.png")
    png.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("geschrieben:", OUT / "icon.ico", OUT / "icon.png")


if __name__ == "__main__":
    main()
