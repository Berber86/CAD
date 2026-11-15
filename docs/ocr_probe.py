"""
Проба: скан-страница deed без текстового слоя -> текст локальным OCR.

Зачем: в ACRIS документы — сканы без текстового слоя (PyMuPDF отдаёт 0 символов).
Проверяем, вытягивает ли бесплатный локальный OCR румбы и длины, которых нам хватает
(«N 22°26'12" E, 100.05 feet»), и сколько это занимает времени на страницу.

Запуск (нужен необязательный пакет):

    .venv/bin/pip install rapidocr-onnxruntime
    .venv/bin/python docs/ocr_probe.py

Результат на песочнице (2026-11-15, CPU без GPU, 300 dpi): 4–6 с на страницу,
все румбы и длины распознаны, у «BEGINNING AT A POINT» и «SUBJECT» поехал регистр,
а после запятых пропадают пробелы («THENCES 67°33'48"E») — для нашего парсера
это неважно, он нормализует текст.
"""
import time, random
from pathlib import Path
import pymupdf
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT = Path("/tmp/ocr"); OUT.mkdir(exist_ok=True)

text_lines = [
    "EXHIBIT A",
    "",
    "ALL THAT CERTAIN plot, piece or parcel of land, with the buildings",
    "and improvements thereon erected, situate, lying and being in the",
    "Borough of Richmond, County of Richmond, City and State of New York,",
    "bounded and described as follows:",
    "",
    'BEGINNING AT A POINT on the easterly side of Alvine Avenue, distant',
    '176.92 feet northerly from the corner formed by the intersection of',
    'the easterly side of Alvine Avenue with the northerly side of',
    'Stafford Avenue;',
    "",
    'RUNNING THENCE N 22°26\'12" E, 100.05 feet;',
    'THENCE S 67°33\'48" E, 40.00 feet;',
    'THENCE S 22°26\'12" W, 100.05 feet;',
    'THENCE N 67°33\'48" W, 40.00 feet to the point or place of BEGINNING.',
    "",
    "SUBJECT to covenants, restrictions and easements of record.",
]

def make_page(path, *, noise=0.0, blur=0.0, angle=0.0, dpi_scale=1.0):
    w, h = int(1700*dpi_scale), int(2200*dpi_scale)
    img = Image.new("L", (w, h), 246)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", int(30*dpi_scale))
    except Exception:
        font = ImageFont.load_default()
    y = int(150*dpi_scale)
    for line in text_lines:
        d.text((int(140*dpi_scale), y), line, fill=35, font=font)
        y += int(48*dpi_scale)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if angle:
        img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=246)
    if noise:
        px = img.load()
        rnd = random.Random(7)
        for _ in range(int(w*h*noise)):
            x, y2 = rnd.randrange(w), rnd.randrange(h)
            px[x, y2] = rnd.randrange(120, 200)
    img.save(path)

make_page(OUT/"clean.png")
make_page(OUT/"scan.png", noise=0.004, blur=1.1, angle=1.2)

# Картинка -> PDF без текстового слоя (как ACRIS)
for name in ("clean", "scan"):
    pdf = pymupdf.open()
    page = pdf.new_page(width=612, height=792)
    page.insert_image(pymupdf.Rect(0, 0, 612, 792), filename=str(OUT/f"{name}.png"))
    pdf.save(OUT/f"{name}.pdf"); pdf.close()

# 1) Проверяем, что текстового слоя нет
for name in ("clean", "scan"):
    doc = pymupdf.open(OUT/f"{name}.pdf")
    got = doc[0].get_text().strip()
    print(f"[{name}.pdf] текстовый слой: {len(got)} символов")

# 2) Рендерим 300 dpi и запускаем OCR
from rapidocr_onnxruntime import RapidOCR
ocr = RapidOCR()

for name in ("clean", "scan"):
    doc = pymupdf.open(OUT/f"{name}.pdf")
    pix = doc[0].get_pixmap(dpi=300)
    png = OUT/f"{name}_300.png"; pix.save(png)
    t0 = time.perf_counter()
    res, _ = ocr(str(png))
    dt = time.perf_counter() - t0
    words = [line[1] for line in (res or [])]
    full = " ".join(words)
    print(f"\n[{name}] OCR: {len(words)} строк за {dt:.2f} с")
    for probe in ("BEGINNING AT A POINT", "176.92", "22°26", "100.05", "67°33", "40.00"):
        print(f"   {probe!r:26} -> {'ЕСТЬ' if probe.replace(' ','') in full.replace(' ','' ) else 'нет'}")
