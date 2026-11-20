"""Local, ephemeral preview of the exact PDF raster sent to decision models."""
from __future__ import annotations

import io
import queue
import threading
from pathlib import Path
from typing import Any

import developer_probability as dp


class PdfImagePreview:
    def __init__(self, owner: Any, filename: str, pages_entry: Any) -> None:
        import tkinter as tk

        self.owner, self.filename, self.pages_entry = owner, filename, pages_entry
        self.closed = False
        self.generation = 0
        self.pending = None
        self.events: queue.Queue = queue.Queue()
        self.page_number: int | None = None
        self.page_list: list[int] = []
        self.image = None
        self.photo = None
        self.zoom = 100
        ctk = owner.ctk
        self.win = ctk.CTkToplevel(owner.win)
        self.win.title("Предпросмотр изображения PDF")
        self.win.geometry("900x800")
        self.win.minsize(480, 400)
        self.win.attributes("-topmost", bool(owner.app.topmost_var.get()))
        self.win.transient(owner.win)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.grid_rowconfigure(2, weight=1)
        self.win.grid_columnconfigure(0, weight=1)
        toolbar = ctk.CTkFrame(self.win, fg_color="transparent")
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=6)
        ctk.CTkButton(toolbar, text="◀", width=35, command=lambda: self.step(-1)).pack(side="left", padx=3)
        ctk.CTkButton(toolbar, text="▶", width=35, command=lambda: self.step(1)).pack(side="left", padx=3)
        ctk.CTkButton(toolbar, text="−", width=35, command=lambda: self.change_zoom(-1)).pack(side="left", padx=3)
        ctk.CTkButton(toolbar, text="+", width=35, command=lambda: self.change_zoom(1)).pack(side="left", padx=3)
        self.label = ctk.StringVar(value="Подготовка страницы…")
        ctk.CTkLabel(toolbar, textvariable=self.label, wraplength=650, justify="left").pack(side="left", padx=8)
        self.info = ctk.StringVar(value="Предпросмотр только локальный; запрос к API не отправляется.")
        ctk.CTkLabel(self.win, textvariable=self.info, wraplength=820, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", padx=10)
        self.canvas = tk.Canvas(self.win, background="#777777", highlightthickness=0)
        self.canvas.grid(row=2, column=0, sticky="nsew", padx=(8, 0), pady=(5, 0))
        vertical = tk.Scrollbar(self.win, orient="vertical", command=self.canvas.yview)
        vertical.grid(row=2, column=1, sticky="ns", pady=(5, 0))
        horizontal = tk.Scrollbar(self.win, orient="horizontal", command=self.canvas.xview)
        horizontal.grid(row=3, column=0, sticky="ew", padx=(8, 0), pady=(0, 8))
        self.canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.refresh()

    def _selected(self) -> list[int]:
        import pymupdf
        with pymupdf.open(self.filename) as pdf:
            if pdf.needs_pass:
                raise ValueError("PDF защищён паролем")
            pages = dp.parse_pages(str(self.pages_entry.get()).strip(), len(pdf))
        if len(pages) > dp.MAX_IMAGES:
            raise ValueError(f"За один запуск допускается не более {dp.MAX_IMAGES} страниц")
        return pages

    def invalidate(self) -> None:
        """Discard old previews immediately, then debounce the new rasterization."""
        if self.closed:
            return
        self.generation += 1
        self.image = self.photo = None
        self.canvas.delete("all")
        self.info.set("Настройки изменены — обновление предпросмотра…")
        if self.pending is not None:
            self.win.after_cancel(self.pending)
        self.pending = self.win.after(300, self.refresh)

    def refresh(self) -> None:
        if self.closed:
            return
        self.pending = None
        self.generation += 1
        generation = self.generation
        self.image = self.photo = None
        self.canvas.delete("all")
        try:
            self.page_list = self._selected()
            if not self.page_list:
                raise ValueError("Не выбраны страницы")
            if self.page_number not in self.page_list:
                self.page_number = self.page_list[0]
            page = self.page_number
            side = self.owner.pdf_image_side
            provider = self.owner.selected_provider()
        except Exception as exc:
            self.info.set(str(exc) if isinstance(exc, ValueError) else "Не удалось открыть PDF для предпросмотра.")
            return
        self.label.set(f"{Path(self.filename).name} · страница {page} · масштаб {self.zoom}%")
        self.info.set(f"Подготовка PNG: длинная сторона до {side} пикселей…")
        def work() -> None:
            try:
                content = dp.prepare_documents([(self.filename, str(page))], "visual", provider=provider, pdf_image_side=side)[0]["content"]
                self.events.put((generation, side, provider, content, None))
            except Exception as exc:
                self.events.put((generation, side, provider, None, str(exc) if isinstance(exc, ValueError) else "Не удалось подготовить изображение."))
        threading.Thread(target=work, daemon=True).start()

    def poll(self) -> None:
        if self.closed:
            return
        while True:
            try:
                generation, side, provider, content, error = self.events.get_nowait()
            except queue.Empty:
                return
            if generation != self.generation:
                continue
            if error:
                self.info.set(error)
                continue
            from PIL import Image
            with Image.open(io.BytesIO(content)) as im:
                self.image = im.convert("RGB").copy()
            w, h = self.image.size
            self.info.set(f"PNG для отправки: {w} × {h} пикселей, {len(content):,} байт · настройка {side} px. "
                          + ("Малые изображения дополнены белым до 280 px. " if provider == "routerai" else "")
                          + ("Сейчас выбран OCR: отправится текст, а не PNG. " if self.owner.mode.get() != "Изображение (эксперимент)" else "")
                          + "Масштаб просмотра не меняет отправляемую картинку. API не вызывается.")
            self._draw()

    def _draw(self) -> None:
        if self.closed or self.image is None:
            return
        from PIL import Image, ImageTk
        size = tuple(max(1, round(n * self.zoom / 100)) for n in self.image.size)
        shown = self.image if self.zoom == 100 else self.image.resize(size, Image.Resampling.NEAREST)
        self.photo = ImageTk.PhotoImage(image=shown, master=self.win)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.configure(scrollregion=(0, 0, *size))
        self.label.set(f"{Path(self.filename).name} · страница {self.page_number} · масштаб {self.zoom}%")

    def step(self, amount: int) -> None:
        if self.page_number not in self.page_list:
            return
        index = self.page_list.index(self.page_number)
        index = max(0, min(len(self.page_list) - 1, index + amount))
        self.page_number = self.page_list[index]
        self.invalidate()

    def change_zoom(self, amount: int) -> None:
        zooms = (50, 100, 200, 400)
        index = max(0, min(len(zooms) - 1, zooms.index(self.zoom) + amount))
        self.zoom = zooms[index]
        self._draw()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        if self.pending is not None:
            self.win.after_cancel(self.pending)
        self.image = self.photo = None
        self.win.destroy()
        if self.owner.preview is self:
            self.owner.preview = None
