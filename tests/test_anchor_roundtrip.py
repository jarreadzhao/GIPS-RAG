"""锚点自洽：每个 chunk 的 [char_start, char_end) 必须精确切出 chunk.text。

这是 M2 回验器的前置条件。原实现拿 PyMuPDF 的原始字符流做基准，
双栏页上块序与字符流序不一致，chunk 区间会横跨到另一栏；
清洗删块后偏移也没重算，页眉会被算进 span。
"""
from __future__ import annotations

from app.parse.chunker import chunk_document
from app.parse.cleaner import clean
from app.parse.pdf_text import PageTextBlock, ParsedDocument, rebuild_page_stream


def _two_column_doc() -> ParsedDocument:
    """模拟双栏页：块序是「先左栏自上而下，再右栏」，与 y 序不同。"""
    blocks = [
        PageTextBlock(page_no=1, text="1.A.4 The firm must document policies in writing.",
                      bbox=(60, 100, 290, 130), char_start=0, char_end=0, ncol=2),
        PageTextBlock(page_no=1, text="1.A.4 Records must be retained for five years.",
                      bbox=(60, 140, 290, 180), char_start=0, char_end=0, ncol=2),
        PageTextBlock(page_no=1, text="1.A.5 The firm should present compliant performance.",
                      bbox=(320, 100, 540, 140), char_start=0, char_end=0, ncol=2),
        PageTextBlock(page_no=2, text="1.A.6 A compliant presentation includes all disclosures.",
                      bbox=(60, 100, 540, 140), char_start=0, char_end=0, ncol=1),
    ]
    return ParsedDocument(pages_text=rebuild_page_stream(blocks, 2), blocks=blocks)


def test_every_chunk_slice_equals_its_text():
    """四策略都必须满足「切片 == 文本」，而不是只看锚点非空。"""
    for strategy in ("provision", "section", "paragraph", "semantic"):
        doc = _two_column_doc()
        chunks = chunk_document(doc, strategy=strategy, lang="en",
                                target_chars=120, max_chars=400)
        assert chunks, f"{strategy} 未产出 chunk"
        for c in chunks:
            page_text = doc.pages_text[c.page_no - 1]
            assert page_text[c.char_start:c.char_end] == c.text, (
                f"{strategy} 策略下 span 与文本不一致：{c.text[:40]!r}"
            )


def test_offsets_stay_aligned_after_cleaning():
    """删掉页眉后偏移重算，剩余块的切片仍然对得上。"""
    doc = _two_column_doc()
    # 清洗器对页眉页脚是确定性删除（HEADER_FOOTER_RE），两页都插一条
    header = PageTextBlock(page_no=1, text="Confidential Internal Use Only",
                           bbox=(60, 20, 300, 40), char_start=0, char_end=0)
    doc.blocks.insert(0, header)
    doc.blocks.insert(3, PageTextBlock(page_no=2, text="Confidential Internal Use Only",
                                       bbox=(60, 20, 300, 40),
                                       char_start=0, char_end=0))
    doc.pages_text = rebuild_page_stream(doc.blocks, 2)

    cleaned = clean(doc)
    assert "Confidential Internal Use Only" not in "\n".join(cleaned.pages_text)
    assert cleaned.blocks
    for b in cleaned.blocks:
        assert cleaned.pages_text[b.page_no - 1][b.char_start:b.char_end] == b.text
