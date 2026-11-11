"""出域前裁边（PRD 2.5.3：只发行文区域）。

为什么必须裁：整页图像会带页边批注、骑缝章、手写签批、页脚文件编号，
这些内容在文本层里不存在，DLP 的数值与客户名正则也扫不到。
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import fitz  # PyMuPDF
from PIL import Image


@dataclass
class RenderedPage:
    """栅格化后的整页图像及其坐标换算信息。"""

    png_bytes: bytes
    width_px: int
    height_px: int
    width_pt: float
    height_pt: float
    dpi: int


@dataclass
class CroppedImage:
    """裁边后的图像，box 记录相对原始页面的坐标以便复现与审计。"""

    png_bytes: bytes
    box_px: dict[str, int]
    box_pt: dict[str, float]
    width_px: int
    height_px: int


def render_page(pdf_path: str, page_no: int, dpi: int = 200) -> RenderedPage:
    """把指定页渲染为 PNG；page_no 从 1 起。"""
    with fitz.open(pdf_path) as doc:
        if page_no < 1 or page_no > doc.page_count:
            raise ValueError(f"page_no {page_no} 超出范围 1..{doc.page_count}")
        page = doc[page_no - 1]
        pix = page.get_pixmap(dpi=dpi)
        return RenderedPage(
            png_bytes=pix.tobytes("png"),
            width_px=pix.width,
            height_px=pix.height,
            width_pt=page.rect.width,
            height_pt=page.rect.height,
            dpi=dpi,
        )


def crop_for_egress(page: RenderedPage, margin_x: int, margin_y: int) -> CroppedImage:
    """按页边距裁剪图像，并返回像素/点两套坐标。

    安全约束：裁完必须仍有正文区域，否则抛错——宁可让该页进人工复核，
    也不允许退化为「发送整页」。
    """
    img = Image.open(BytesIO(page.png_bytes))
    w, h = img.size
    x0 = min(max(margin_x, 0), w - 1)
    y0 = min(max(margin_y, 0), h - 1)
    x1 = max(min(w - margin_x, w), x0 + 1)
    y1 = max(min(h - margin_y, h), y0 + 1)
    if (x1 - x0) < w * 0.5 or (y1 - y0) < h * 0.5:
        raise ValueError("裁边后正文区域不足一半，拒绝出域，转人工复核")
    cropped = img.crop((x0, y0, x1, y1))
    buf = BytesIO()
    cropped.save(buf, format="PNG")
    scale = 72.0 / page.dpi  # 像素 -> PDF 点
    return CroppedImage(
        png_bytes=buf.getvalue(),
        box_px={"x0": x0, "y0": y0, "x1": x1, "y1": y1},
        box_pt={k: round(v * scale, 2) for k, v in
                {"x0": x0, "y0": y0, "x1": x1, "y1": y1}.items()},
        width_px=x1 - x0,
        height_px=y1 - y0,
    )
