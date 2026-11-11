# -*- coding: utf-8 -*-
"""四件套联检 v0.2：PRD / 契约 / 接口 / 验收标准

用法：
    python spec_link.py --root .
    python spec_link.py --root . --contract spec/契约.md --accept spec/验收标准.md

检查项：
    [1] 文件与版本头
    [2] 上游版本链
    [3] ID 定义/引用闭合
    [4] 派生条目挂上游来源
    [5] 计数型断言
    [6] 变更单
    [7] 残留词表与占位符
    [8] 契约六块齐备            v0.2 新增
    [9] 验收阶段门齐备          v0.2 新增

设计原则：
    1. 缺文件只 WARN，不中断（新建项目时三份派生文件可能还不存在）
    2. 所有检查机器可判，不做语义判断
    3. 退出码：有 FAIL 返回 1，可直接挂 pre-commit
"""
import argparse
import io
import json
import os
import re
import sys

FAIL, WARN, PASS = [], [], []


def ok(msg):
    PASS.append(msg)
    print('  OK   ' + msg)


def bad(msg):
    FAIL.append(msg)
    print('  FAIL ' + msg)


def warn(msg):
    WARN.append(msg)
    print('  WARN ' + msg)


def read(path):
    with io.open(path, encoding='utf-8') as f:
        return f.read()


# ---------- 版本头 ----------

def header(text, field):
    """从 markdown 头部的 | 字段 | 内容 | 表里取字段值。"""
    for line in text.split('\n'):
        m = re.match(r'^\|\s*' + re.escape(field) + r'\s*\|(.+?)\|\s*$', line)
        if m:
            return m.group(1).strip()
    return None


def version_of(text):
    raw = header(text, '版本')
    if not raw:
        return None
    m = re.search(r'v?(\d+(?:\.\d+)+)', raw)
    return m.group(1) if m else None


# ---------- 标题与分节 ----------

def headings(text):
    """返回 [(行号, 级别, 标题)] 与全部行。"""
    hs, lines = [], text.split('\n')
    for i, l in enumerate(lines):
        m = re.match(r'^(#{2,4})\s+(.*?)\s*$', l)
        if m:
            hs.append((i, len(m.group(1)), m.group(2)))
    return hs, lines


def section_body(lines, hs, idx, level):
    """取 idx 标题之下、下一个同级或更高级标题之前的正文。"""
    start, end = idx + 1, len(lines)
    for (j, lv, _t) in hs:
        if j > idx and lv <= level:
            end = j
            break
    return '\n'.join(lines[start:end])


def norm_title(t):
    """去掉标题里的编号与装饰，便于关键词匹配。"""
    t = re.sub(r'^[（(]?\s*\d+\s*[)）.、]?\s*', '', t)
    t = re.sub(r'^第\s*\d+\s*[块节部分]\s*[：:、.．]?\s*', '', t)
    t = re.sub(r'^[一二三四五六七八九十]+\s*[、.．]\s*', '', t)
    return t.strip(' 　:：*_')


# ---------- ID 扫描 ----------

def scan_ids(text, prefixes):
    """返回 (定义集合, 引用集合, {id: (行号, 行)})。行首出现的算定义，其余算引用。"""
    defs, refs, where = set(), set(), {}
    for lineno, line in enumerate(text.split('\n'), 1):
        for p in prefixes:
            for m in re.finditer(r'(?<![\w-])(' + re.escape(p) + r'-\d+)(?![\d-])', line):
                tok = m.group(1)
                stripped = re.sub(r'^[\s>#*\-|]+', '', line)
                # 允许「- [x] AC-1 ...」这种勾选行开头也算定义（验收自查清单常见）
                stripped = re.sub(r'^\[[ xX]\]\s*', '', stripped)
                is_def = stripped.startswith(tok) or stripped.startswith('**' + tok)
                if is_def:
                    defs.add(tok)
                    where.setdefault(tok, (lineno, line))
                else:
                    refs.add(tok)
    return defs, refs, where


# ---------- 契约六块 / 验收五节 ----------

CONTRACT_BLOCKS = [
    ('字段与类型', ('字段与类型', '字段类型', '字段与约束')),
    ('成功与失败语义', ('成功与失败', '错误码', '错误结构', '失败语义')),
    ('身份与数据规则', ('身份与数据', '身份与权限', '权限与敏感', '敏感数据')),
    ('状态与并发', ('状态与并发', '状态机与并发', '幂等与并发')),
    ('兼容与版本', ('兼容与版本', '版本与兼容', '兼容性')),
    ('责任划分', ('责任划分', '责任边界')),
]

ACCEPT_SECTIONS = ['进入条件', '执行', '产物', '自查清单', '放行条件']


def check_contract_blocks(files):
    print('[8] 契约六块')
    if 'contract' not in files:
        warn('无契约文件，跳过六块检查')
        return
    text = files['contract'][1]
    hs, lines = headings(text)
    titles = [norm_title(t) for (_i, _lv, t) in hs]
    missing, tabled = [], []
    for name, kws in CONTRACT_BLOCKS:
        hit = None
        for k, (i, lv, _t) in enumerate(hs):
            if any(kw in titles[k] for kw in kws):
                hit = (i, lv)
                break
        if not hit:
            in_table = any(l.strip().startswith('|') and any(kw in l for kw in kws)
                           for l in lines)
            (tabled if in_table else missing).append(name)
            continue
        body = section_body(lines, hs, hit[0], hit[1]).strip()
        # 允许"不适用"，但必须给理由
        m = re.search(r'不适用\s*[：:]\s*(.{4,})', body)
        if m:
            continue
        if '不适用' in body:
            bad('契约「%s」声明不适用但未给理由（写成「不适用：<理由>」）' % name)
        elif len(re.sub(r'\s', '', body)) < 10:
            bad('契约「%s」只有标题，没有内容' % name)
    if missing:
        bad('契约缺 %d 块：%s —— 标题须保留，不适用也要写「不适用：<理由>」'
            % (len(missing), '、'.join(missing)))
    if tabled:
        warn('契约 %s 只出现在表格里，未成节（表格容易被当成目录，不算写完）'
             % '、'.join(tabled))
    if not missing and not tabled:
        ok('契约六块齐备')


def check_accept_gate(files):
    print('[9] 验收阶段门')
    if 'accept' not in files:
        warn('无验收文件，跳过阶段门检查')
        return
    text = files['accept'][1]
    hs, lines = headings(text)
    if not hs:
        bad('验收文件没有任何二级以上标题，未按阶段门组织')
        return

    # 阶段名：首个二级标题应形如「## 阶段：xxx」
    top = [(i, t) for (i, lv, t) in hs if lv == 2]
    if top and '阶段' not in top[0][1]:
        warn('验收首个二级标题不含「阶段」：%s（建议写成「## 阶段：验收」）' % top[0][1])

    titles = [norm_title(t) for (_i, _lv, t) in hs]
    idx_map = {}
    for name in ACCEPT_SECTIONS:
        for k, (i, lv, _t) in enumerate(hs):
            if name in titles[k]:
                idx_map[name] = (i, lv)
                break
    missing = [n for n in ACCEPT_SECTIONS if n not in idx_map]
    if missing:
        bad('验收缺 %d 节：%s' % (len(missing), '、'.join(missing)))
    for name in ACCEPT_SECTIONS:
        if name not in idx_map:
            continue
        body = section_body(lines, hs, idx_map[name][0], idx_map[name][1])
        if len(re.sub(r'\s', '', body)) < 6:
            bad('验收「%s」节为空' % name)

    # 自查清单：必须有勾选项；已签发时不允许有未勾项
    if '自查清单' in idx_map:
        body = section_body(lines, hs, idx_map['自查清单'][0], idx_map['自查清单'][1])
        boxes = re.findall(r'^\s*[-*]\s*\[([ xX])\]', body, flags=re.M)
        if not boxes:
            bad('验收「自查清单」没有任何 - [ ] 勾选项')
        else:
            unchecked = len([b for b in boxes if b == ' '])
            state = (header(text, '状态') or '').strip()
            if unchecked and state == '已签发':
                bad('验收状态为「已签发」但自查清单仍有 %d 项未勾' % unchecked)
            elif unchecked:
                warn('验收自查清单未勾 %d / %d 项（草稿阶段可接受）' % (unchecked, len(boxes)))
            else:
                ok('验收自查清单 %d 项全部已勾' % len(boxes))
    if not missing:
        ok('验收五节齐备')


# ---------- 原有检查 ----------

def check_files(args):
    print('[1] 文件与版本头')
    files = {}
    for role, path in (('prd', args.prd), ('contract', args.contract),
                       ('api', args.api), ('accept', args.accept)):
        if not path or not os.path.exists(path):
            warn('缺少 %s：%s（跳过该文件的检查）' % (role, path))
            continue
        files[role] = (path, read(path))
        v = version_of(files[role][1])
        chg = header(files[role][1], '变更单')
        if not v:
            bad('%s 未找到「版本」字段或格式不可解析：%s' % (role, path))
        if not chg:
            bad('%s 未找到「变更单」字段：%s' % (role, path))
        if v:
            ok('%s 版本 v%s，变更单 %s' % (role, v, chg or '(缺)'))
    return files


def check_chain(files):
    print('[2] 上游版本链')
    if 'prd' not in files:
        warn('无 PRD，跳过版本链检查')
        return
    prd_v = version_of(files['prd'][1])
    for role in ('contract', 'api', 'accept'):
        if role not in files:
            continue
        up = header(files[role][1], '上游版本')
        if not up:
            bad('%s 未填「上游版本」，无法判断是否跟随 PRD' % role)
            continue
        m = re.search(r'v?(\d+(?:\.\d+)+)', up)
        up_v = m.group(1) if m else None
        if up_v != prd_v:
            bad('%s 上游版本 v%s ≠ PRD v%s（改了 PRD 没同步下游）' % (role, up_v, prd_v))
        else:
            ok('%s 上游版本与 PRD 一致（v%s）' % (role, up_v))


def check_ids(files, prefixes, terminal=()):
    print('[3] ID 定义/引用闭合')
    all_defs, all_refs = set(), set()
    for role, (path, text) in files.items():
        d, r, _w = scan_ids(text, prefixes)
        all_defs |= d
        all_refs |= r
    dangling = sorted(all_refs - all_defs)
    if dangling:
        bad('引用了未定义的 ID：%s' % dangling[:20])
    else:
        ok('所有 ID 引用均有定义（%d 个定义 / %d 个引用）' % (len(all_defs), len(all_refs)))
    # 链末端 ID（AC / API）不会被下游引用，报"孤儿"只会制造永久噪音
    never = sorted(t for t in (all_defs - all_refs)
                   if t.split('-')[0] not in terminal)
    if never:
        warn('定义了但从未被引用（可能是孤儿条目，也可能正常）：%s' % never[:15])


def check_origin(files, prefixes):
    print('[4] 派生条目必须挂上游来源')
    for role, child, parent in (('accept', 'AC', 'R'), ('api', 'API', 'CT')):
        if role not in files:
            continue
        d, _, w = scan_ids(files[role][1], prefixes)
        if not d:
            warn('%s 中未发现 %s-* 条目' % (role, child))
            continue
        missing = []
        for tok in sorted(d):
            lineno, line = w[tok]
            if not re.search(r'(?<![\w-])' + parent + r'-\d+', line):
                missing.append('%s(L%d)' % (tok, lineno))
        if missing:
            warn('%s 有 %d 条未标 %s 来源：%s' % (role, len(missing), parent, missing[:10]))
        else:
            ok('%s 的 %s 条目全部标注了 %s 来源（%d 条）' % (role, child, parent, len(d)))


def check_counts(files):
    print('[5] 计数型断言')
    if 'prd' not in files:
        warn('无 PRD，跳过计数断言')
        return
    prd = files['prd'][1]
    lines = prd.split('\n')
    n_ch = len([l for l in lines if re.match(r'^##\s*第\s*\d+\s*章', l)])
    n_ap = len([l for l in lines if re.match(r'^##\s*附录\s*[A-Z]', l)])
    for m in re.finditer(r'共\s*(\d+)\s*章', prd):
        if int(m.group(1)) != n_ch:
            bad('文中「共 %s 章」与实际 %d 章不符' % (m.group(1), n_ch))
    for m in re.finditer(r'共\s*(\d+)\s*(?:个)?附录', prd):
        if int(m.group(1)) != n_ap:
            bad('文中「共 %s 个附录」与实际 %d 个不符' % (m.group(1), n_ap))
    ok('章节实数：%d 章 / %d 附录' % (n_ch, n_ap))
    if 'accept' in files:
        d, _, _ = scan_ids(files['accept'][1], ['AC'])
        n_ac = len(d)
        for m in re.finditer(r'(\d+)\s*条\s*(?:AC|验收|用例)', prd):
            if int(m.group(1)) != n_ac:
                bad('PRD 写「%s 条」，验收文件实际 %d 条 AC' % (m.group(1), n_ac))
        ok('验收条目实数：%d 条 AC' % n_ac)


def check_change(files, root):
    print('[6] 变更单')
    chgs = {}
    for role, (path, text) in files.items():
        c = header(text, '变更单')
        if c:
            chgs[role] = c.strip()
    if not chgs:
        warn('四份文件均未填变更单号')
        return
    vals = set(chgs.values())
    if len(vals) > 1:
        bad('变更单号不一致：%s' % chgs)
        return
    chg = vals.pop()
    ok('变更单号一致：%s' % chg)
    cand = [os.path.join(root, 'changes', chg + '.md'), os.path.join(root, chg + '.md')]
    if not any(os.path.exists(p) for p in cand):
        warn('未找到变更单文件 %s（应放在 changes/ 下）' % chg)


def check_lexicon(files, lexicon_path):
    print('[7] 残留词表与占位符')
    lex = []
    if lexicon_path and os.path.exists(lexicon_path):
        with io.open(lexicon_path, encoding='utf-8') as f:
            lex = json.load(f).get('stale_terms', [])
    for role, (path, text) in files.items():
        for kw in lex:
            hits = [i for i, l in enumerate(text.split('\n'), 1) if kw in l]
            if hits:
                warn('%s 仍含「%s」%d 处：行 %s' % (role, kw, len(hits), hits[:8]))
        # 占位符检：跳过代码块，且不吃「占位符」这类业务术语
        ph, incode = [], False
        for i, l in enumerate(text.split('\n'), 1):
            if l.strip().startswith('```'):
                incode = not incode
                continue
            if incode:
                continue
            if re.search(r'TODO|FIXME|XXX|APPEND-HERE|待填|待补充|此处省略', l):
                ph.append(i)
        if ph:
            bad('%s 含占位符 %d 处：行 %s' % (role, len(ph), ph[:8]))
        if not text.strip():
            bad('%s 为空文件：%s' % (role, path))
    if not lex:
        warn('未提供词表（--lexicon spec/stale-terms.json），跳过残留检查')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='.')
    ap.add_argument('--prd', default=None)
    ap.add_argument('--contract', default=None)
    ap.add_argument('--api', default=None)
    ap.add_argument('--accept', default=None)
    ap.add_argument('--prefixes', default='R,CT,API,AC')
    ap.add_argument('--lexicon', default=None)
    ap.add_argument('--terminal', default='AC,API',
                    help='链末端 ID 前缀：不会被下游引用，不报孤儿')
    a = ap.parse_args()

    root = a.root
    spec = os.path.join(root, 'spec')
    a.prd = a.prd or os.path.join(spec, 'PRD.md')
    a.contract = a.contract or os.path.join(spec, '契约.md')
    a.api = a.api or os.path.join(spec, '接口.md')
    a.accept = a.accept or os.path.join(spec, '验收标准.md')
    lexicon = a.lexicon or os.path.join(spec, 'stale-terms.json')
    prefixes = [p.strip() for p in a.prefixes.split(',') if p.strip()]

    print('==== 四件套联检 root=%s ====' % os.path.abspath(root))
    files = check_files(a)
    if not files:
        print('\n未找到任何规格文件。预期路径：')
        for p in (a.prd, a.contract, a.api, a.accept):
            print('  ' + p)
        return 1
    check_chain(files)
    check_ids(files, prefixes, set(t.strip() for t in a.terminal.split(',') if t.strip()))
    check_origin(files, prefixes)
    check_counts(files)
    check_change(files, root)
    check_lexicon(files, lexicon)
    check_contract_blocks(files)
    check_accept_gate(files)

    print('\n==== 结果：%d 通过 / %d 失败 / %d 警告 ====' % (len(PASS), len(FAIL), len(WARN)))
    for f in FAIL:
        print('FAIL:', f)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
