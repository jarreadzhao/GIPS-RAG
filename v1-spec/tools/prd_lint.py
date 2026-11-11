# -*- coding: utf-8 -*-
"""PRD 结构机检。用法：python prd_lint.py <file.md>
检查：章/节编号连续性、图表编号双向、表格列数、悬空引用、计数型断言、残留旧表述、emoji。
"""
import io, re, sys

path = sys.argv[1] if len(sys.argv) > 1 else 'PRD.md'
s = io.open(path, encoding='utf-8').read()
L = s.split('\n')
FAIL = []
WARN = []

def ok(msg): print('  OK   ' + msg)
def bad(msg): FAIL.append(msg); print('  FAIL ' + msg)
def warn(msg): WARN.append(msg); print('  WARN ' + msg)

# 1 章节
print('[1] 章节编号')
chs = []
for l in L:
    m = re.match(r'^## 第 (\d+) 章', l)
    if m: chs.append(int(m.group(1)))
    elif re.match(r'^## 附录 ([A-Z])', l): chs.append(0)
seq = [c for c in chs if c > 0]
ok('章数 %d：%s' % (len(seq), seq == list(range(1, len(seq) + 1)) and '连续' or seq))
if seq != list(range(1, len(seq) + 1)): bad('章号不连续：%s' % seq)
apps = [l for l in L if re.match(r'^## 附录 ([A-Z])', l)]
ap = [re.match(r'^## 附录 ([A-Z])', l).group(1) for l in apps]
if ap != [chr(65 + i) for i in range(len(ap))]: bad('附录顺序异常：%s' % ap)
else: ok('附录 %d 个：%s' % (len(ap), ','.join(ap)))

# 2 小节
print('[2] 小节编号')
sec = {}
for l in L:
    m = re.match(r'^### (\d+)\.(\d+)\s', l)
    if m: sec.setdefault(int(m.group(1)), []).append(int(m.group(2)))
badc = []
for c in sorted(sec):
    if sec[c] != list(range(1, len(sec[c]) + 1)): badc.append((c, sec[c]))
if badc: bad('小节不连续：%s' % badc)
else: ok('全部小节连续（共 %d 个）' % sum(len(v) for v in sec.values()))

# 3 图
print('[3] 图编号与引用')
defs = []
for i, l in enumerate(L, 1):
    for m in re.finditer(r'\*\*图 (\d+)：', l): defs.append((int(m.group(1)), i))
refs = set()
incode = False
for l in L:
    if l.strip().startswith('```'): incode = not incode; continue
    if incode: continue
    for m in re.finditer(r'(?<!\*\*)图 (\d+)(?![:：\d])', l): refs.add(int(m.group(1)))
dn = [d[0] for d in defs]
if dn != list(range(1, len(dn) + 1)): bad('图定义编号不连续：%s' % dn)
else: ok('图 1–%d 定义连续' % len(dn))
miss_def = sorted(refs - set(dn))
miss_ref = sorted(set(dn) - refs)
if miss_def: bad('引用了未定义的图：%s' % miss_def)
else: ok('无悬空图引用')
if miss_ref: bad('定义了但正文无引用：%s' % miss_ref)
else: ok('每张图均有正文引用')

# 4 表格列数
print('[4] 表格列数')
incode = False; tbl = []; issues = []
for i, l in enumerate(L, 1):
    if l.strip().startswith('```'): incode = not incode; continue
    if incode: continue
    c = re.sub(r'`[^`]*`', 'C', l).replace('\\|', 'P')
    st = c.strip()
    if st.startswith('|') and (st.endswith('|') or '<span' in st):
        tbl.append((i, c.count('|')))
    else:
        if tbl and len(set(x[1] for x in tbl)) > 1:
            issues.append((tbl[0][0], sorted(set(x[1] for x in tbl))))
        tbl = []
if tbl and len(set(x[1] for x in tbl)) > 1:
    issues.append((tbl[0][0], sorted(set(x[1] for x in tbl))))
if issues: bad('表格列数不一致（起始行, 列数集合）：%s' % issues)
else: ok('表格列数全部一致')

# 5 悬空 X.Y 引用
print('[5] 跨章节引用')
heads = set()
for l in L:
    for m in re.finditer(r'^#{2,4} (\d+(?:\.\d+)+)', l): heads.add(m.group(1))
body_ref = set()
incode = False
for l in L:
    if l.strip().startswith('```'): incode = not incode; continue
    if incode or l.startswith('#'): continue
    for m in re.finditer(r'(?<![\w.\-])(\d{1,2}\.\d{1,2})(?![\d.])', l):
        tok = m.group(1)
        if tok.startswith('0.'): continue   # 0.75 这类阈值/小数，不是小节号
        tail = l[m.end():m.end() + 10]
        if re.match(r'\s*(%|GB|MB|KB|TB|token|ms|个百分点|元|万元|亿|万|倍|秒|分|小时|s\b)', tail): continue  # 57.3% / 7.5 GB 这类带单位数值
        body_ref.add(tok)
dangling = sorted(r for r in body_ref if r not in heads)
if dangling: bad('悬空 X.Y 引用：%s' % dangling)
else: ok('无悬空小节引用')

# 6 计数型断言
print('[6] 计数型断言')
rows = [l for l in L if re.match(r'^\| [A-E]\d+ \|', l)]
n_row = len(rows)
print('     场景行数 = %d' % n_row)
for m in re.finditer(r'(\d+)\s*场景', s):
    if int(m.group(1)) != n_row: bad('文中「%s 场景」与实际 %d 行不符' % (m.group(1), n_row))
for m in re.finditer(r'≥\s*(\d+)\s*(?:条)?用例', s):
    if int(m.group(1)) != n_row * 3: bad('文中「≥%s 用例」应为 %d（%d×3）' % (m.group(1), n_row * 3, n_row))
print('     文中应为：%d 场景 / ≥%d 用例' % (n_row, n_row * 3))

# 指标双处一致
def metric_rows(start_pat, stop_pat):
    on = False; n = 0
    for l in L:
        if re.match(start_pat, l): on = True; continue
        if on and re.match(stop_pat, l): break
        if on and l.startswith('| ') and not re.match(r'^\|\s*(指标|项|#|---)', l): n += 1
    return n
# 1.3 与附录 A
def block_rows(start, stop):
    on = False; n = 0
    for l in L:
        if start in l: on = True; continue
        if on and stop in l: break
        if on and l.startswith('|') and not re.match(r'^\|[\s\-:|]+\|$', l) and not re.match(r'^\| 指标', l): n += 1
    return n
try:
    a = block_rows('### 1.3 核心验收指标', '### 1.4')
    b = block_rows('## 附录 A 指标口径汇总', '## 附录 B')
    if a != b: bad('1.3 指标 %d 条 vs 附录 A %d 条，数量不等' % (a, b))
    else: ok('1.3 与附录 A 指标数一致（%d 条）' % a)
except Exception as e:
    warn('指标数比对跳过：%s' % e)

# 7 残留旧表述（双向断言）
print('[7] 残留旧表述')
for kw in ['全私有化']:
    hits = [(i, l[:90]) for i, l in enumerate(L, 1) if kw in l]
    if hits:
        warn('「%s」仍出现 %d 处（须确认均为历史留痕/取代说明）：' % (kw, len(hits)))
        for h in hits: print('        L%d %s' % h)
    else: ok('无「%s」残留' % kw)

# 8 emoji 与行首标题
print('[8] 格式')
emo = [c for c in set(s) if ord(c) > 0x1F000]
if emo: bad('含 emoji：%s' % emo)
else: ok('无 emoji')
hd = [i for i, l in enumerate(L, 1) if re.match(r'^<span[^>]*>\s*#{2,4} ', l)]
if hd: bad('标题被 span 包裹（不会被渲染）：行 %s' % hd)
else: ok('无标题被样式包裹')

# 9 span 配对
print('[9] 内联样式')
n_open = s.count('<span'); n_close = s.count('</span>')
if n_open != n_close: bad('span 开合不等：%d / %d' % (n_open, n_close))
else: ok('span 开合配对（%d 对）' % n_open)

# 10 mermaid
blocks = re.findall(r'```mermaid\n(.*?)```', s, re.S)
print('[10] mermaid 块 %d 个' % len(blocks))
for i, b in enumerate(blocks, 1):
    ft = b.strip().split('\n')[0].strip()
    if not re.match(r'^(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie|journey|mindmap|timeline)', ft):
        bad('第 %d 个 mermaid 首行非法：%s' % (i, ft))
    # 节点标签内不宜出现未转义引号
    if ft.startswith(('graph', 'flowchart')):
        if '-->>' in b or '->>' in b: warn('第 %d 个 flowchart 含时序语法' % i)
ok('mermaid 首行类型合法')

# 11 中英文括号与半角标点粗检
print('[11] 其他')
dup = re.findall(r'[，。；：]\s*[，。；：]', s)
if dup: warn('疑似重复标点 %d 处' % len(dup))
ph = [l for l in L if 'APPEND-HERE' in l or 'TODO' in l or 'XXX' in l]
if ph: bad('含占位符：%d 行' % len(ph))
else: ok('无遗留占位符')

print('\n==== 结果：%d 失败 / %d 警告 / 总行数 %d / 字符 %d ====' % (len(FAIL), len(WARN), len(L), len(s)))
for f in FAIL: print('FAIL:', f)
for w in WARN: print('WARN:', w)
sys.exit(1 if FAIL else 0)
