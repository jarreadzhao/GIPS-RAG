"""AC-3：条款边界切分 —— provision_id 非空、char 偏移页内唯一、块间不重叠。"""
from __future__ import annotations

from app.parse.chunker import chunk_document, make_chunk_id
from app.parse.pdf_text import PageTextBlock, ParsedDocument


def _doc() -> ParsedDocument:
    """构造两页、共四个条款块的合成文档。"""
    texts = [
        (1, "1.A.4 The firm must document its policies and procedures in writing.", 0),
        (1, "1.A.4 Continued explanation of the disclosure requirement for the firm.", 75),
        (2, "1.A.5 The firm shall retain records for a minimum of five years.", 0),
        (2, "1.A.6 The firm shall present a compliant presentation to all prospects.", 72),
    ]
    blocks = [
        PageTextBlock(page_no=p, text=t, bbox=(0, 0, 100, 10),
                      char_start=s, char_end=s + len(t))
        for p, t, s in texts
    ]
    return ParsedDocument(pages_text=["", ""], blocks=blocks)


def test_provision_ids_present():
    """每个 chunk 都能回链到条款编号。"""
    chunks = chunk_document(_doc(), strategy="provision", lang="en",
                            target_chars=120, max_chars=400)
    assert chunks
    assert all(c.provision_id in {"1.A.4", "1.A.5", "1.A.6"} for c in chunks)


def test_chunks_never_overlap():
    """同一页内后一块的起点不得小于前一块终点。"""
    chunks = chunk_document(_doc(), strategy="provision", lang="en",
                            target_chars=80, max_chars=400)
    by_page: dict[int, list] = {}
    for c in chunks:
        by_page.setdefault(c.page_no, []).append(c)
    for page_chunks in by_page.values():
        ordered = sorted(page_chunks, key=lambda c: c.char_start)
        for prev, nxt in zip(ordered, ordered[1:]):
            assert nxt.char_start >= prev.char_end


def test_chunk_id_deterministic():
    """chunk_id 只由定位信息决定，重复构建不变。"""
    a = make_chunk_id("DOC-1", 1, 1, 0, 10)
    b = make_chunk_id("DOC-1", 1, 1, 0, 10)
    c = make_chunk_id("DOC-1", 1, 1, 0, 11)
    assert a == b != c
    assert len(a) == 32


def test_back_matter_does_not_inherit_clause_id():
    """D-M2-07：术语表/附录没有条款编号，不得继承上一条款的编号。"""
    blocks = [
        PageTextBlock(page_no=1,
                      text="8.H.4 The firm should disclose the definition of the firm.",
                      bbox=(0, 0, 100, 10), char_start=0,
                      char_end=len("8.H.4 The firm should disclose the definition of the firm.")),
        PageTextBlock(page_no=2, text="GLOSSARY", bbox=(0, 0, 100, 10),
                      char_start=0, char_end=8),
        PageTextBlock(page_no=2,
                      text="accrual accounting The recording of transactions as income is earned.",
                      bbox=(0, 0, 100, 10), char_start=9,
                      char_end=9 + len("accrual accounting The recording of transactions as income is earned.")),
        PageTextBlock(page_no=3,
                      text="additional risk measures Risk measures included beyond those required.",
                      bbox=(0, 0, 100, 10), char_start=0,
                      char_end=len("additional risk measures Risk measures included beyond those required.")),
    ]
    doc = ParsedDocument(pages_text=["", "", ""], blocks=blocks)
    chunks = chunk_document(doc, strategy="provision", lang="en",
                            target_chars=200, max_chars=400)

    clause = [c for c in chunks if "8.H.4" in c.text]
    assert clause and all(c.provision_id == "8.H.4" for c in clause)
    back_matter = [c for c in chunks if c.page_no >= 2]
    assert back_matter
    assert all(c.provision_id is None for c in back_matter)


def test_chinese_back_matter_does_not_inherit_clause_id():
    """D-M2-07 中文侧：中文「术语表」必须断开条款号延续。"""
    heading = "术语表"
    blocks = [
        PageTextBlock(page_no=1,
                      text="8.H.4 公司应当披露公司的定义。",
                      bbox=(0, 0, 100, 10), char_start=0,
                      char_end=len("8.H.4 公司应当披露公司的定义。")),
        PageTextBlock(page_no=2, text=heading, bbox=(0, 0, 100, 10),
                      char_start=0, char_end=len(heading)),
        PageTextBlock(page_no=2, text="权责发生制会计 将交易记录为收入的获得或费用的发生。",
                      bbox=(0, 0, 100, 10), char_start=len(heading) + 1,
                      char_end=len(heading) + 1 + len(
                          "权责发生制会计 将交易记录为收入的获得或费用的发生。")),
    ]
    doc = ParsedDocument(pages_text=["", ""], blocks=blocks)
    chunks = chunk_document(doc, strategy="provision", lang="zh",
                            target_chars=200, max_chars=400)

    clause = [c for c in chunks if "8.H.4" in c.text]
    assert clause and all(c.provision_id == "8.H.4" for c in clause)
    glossary = [c for c in chunks if "权责发生制会计" in c.text]
    assert glossary and all(c.provision_id is None for c in glossary)


def test_chinese_provision_mentioning_appendix_is_not_back_matter():
    """正文提到「附录」但标题不是后置正文行时，不得断开条款号。"""
    blocks = [
        PageTextBlock(page_no=1,
                      text="1.A.4 公司应当披露附录中列示的补充信息。",
                      bbox=(0, 0, 100, 10), char_start=0,
                      char_end=len("1.A.4 公司应当披露附录中列示的补充信息。")),
        PageTextBlock(page_no=1,
                      text="1.A.5 公司应当保存相关记录至少五年。",
                      bbox=(0, 0, 100, 10), char_start=30,
                      char_end=30 + len("1.A.5 公司应当保存相关记录至少五年。")),
    ]
    doc = ParsedDocument(pages_text=[""], blocks=blocks)
    chunks = chunk_document(doc, strategy="provision", lang="zh",
                            target_chars=200, max_chars=400)
    assert all(c.provision_id in {"1.A.4", "1.A.5"} for c in chunks)
