# -*- coding: utf-8 -*-
"""代码回写门禁：代码改了，规格必须追认。

用法：
    python spec_sync.py --root v1-spec

解决的问题：
    spec_gate 判的是「规格改了，升版了吗」。
    但实际最常见的顺序是反的 —— 代码先改，改完发现和 PRD 写的不一致，
    这时要做的不是「让代码回去」，而是「把代码的实际行为回写进规格」。
    这类漏网的隐蔽性在于：改代码的人不觉得自己在改规格，只是「实现调整」。
    spec_gate 遇到「代码改了、规格没动」只打一行提醒，仍是 OK，拦不住。

本脚本把这条链补上，做四件机器能判的事：
    [1] 相对 git HEAD，哪些代码文件被改过（只看 staged，即这次真提交的内容）
    [2] 代码有改动 → changes/ 下必须有新增或修改的 CHG，且含「代码变更追溯」节
    [3] 追溯节声明了规格影响（R / CT / API / AC 编号）→ 四份规格必须真的被改过
        声明「无」→ 允许代码单飞，但要留痕
    [4] 追溯节列的代码路径必须真实存在于本次改动（防事后编造），
        实际改动的代码文件若未被覆盖 → WARN（防漏写）

退出码：有 FAIL 返回 1，可直接挂 pre-commit。
无 git 仓库或无 HEAD（首次提交）时，[1]-[4] 全部降级为跳过。

与另两个脚本的分工（顺序：sync → gate → link）：
    spec_sync  代码改了，规格追认了吗
    spec_gate  规格改了，算不算一次新版本（升版 + 开单）
    spec_link  四份之间对得上吗（上游版本链 + ID 闭合）
"""
import argparse
import fnmatch
import io
import os
import re
import subprocess
import sys

FAIL, WARN, PASS = [], [], []

# 代码目录：这些路径下的改动视为「改了实现」，需要规格追认
CODE_DIRS = ('app', 'src', 'db', 'config', 'prompts', 'scripts', 'tests', 'deploy', 'frontend')
CODE_FILES = ('pyproject.toml', 'docker-compose.yml', 'requirements.txt')

SPEC_NAMES = ('PRD.md', '契约.md', '接口.md', '验收标准.md')
TRACE_HEAD = '代码变更追溯'

# 规格影响里出现这些词即视为「声明有影响」
SPEC_ID = re.compile(r'\b(?:R|CT|API|AC)-\d+[A-Z]?\b')


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


def git(*args, **kw):
    try:
        p = subprocess.run(('git',) + args, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, **kw)
    except OSError:
        return 127, ''
    out = p.stdout.decode('utf-8', 'replace') if p.stdout else ''
    return p.returncode, out


def classify(path, rel, spec_dir='spec'):
    """把文件归入 code / spec / chg / other。path 相对仓库根。"""
    if path.startswith('%s/%s/' % (rel, spec_dir)) and os.path.basename(path) in SPEC_NAMES:
        return 'spec'
    if path.startswith('%s/changes/' % rel) and 'CHG-' in os.path.basename(path):
        return 'chg'
    top = path.split('/')[0]
    if top in CODE_DIRS:
        return 'code'
    if path in CODE_FILES:
        return 'code'
    # tests 可能在任意层，按目录名兜底
    if any(seg in ('tests', 'test') for seg in path.split('/')[:-1]):
        return 'code'
    return 'other'


def trace_block(text):
    """抽取 CHG 里「代码变更追溯」节全文；无则返回 None。"""
    m = re.search(r'^#+\s*' + TRACE_HEAD + r'.*$', text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r'^#{1,2}\s+\S', rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def trace_field(block, field):
    """从追溯节里取某个字段的值：支持 | 字段 | 值 | 与 字段：值 两种写法。"""
    for line in block.split('\n'):
        m = re.match(r'^\|\s*' + re.escape(field) + r'\s*\|\s*(.+?)\s*\|\s*$', line)
        if m:
            return m.group(1).strip()
        m = re.match(r'^\s*[-*]?\s*' + re.escape(field) + r'\s*[:：]\s*(.+)$', line)
        if m:
            return m.group(1).strip()
    return None


def load_codemap(root):
    """读 codemap.md 的映射表：[{glob, ids, note}]。缺失返回空表。"""
    p = os.path.join(root, 'codemap.md')
    if not os.path.exists(p):
        return None
    rows = []
    for line in read(p).split('\n'):
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) < 2 or cells[0] in ('路径', '路径 glob') or set(cells[0]) <= set('-: '):
            continue
        rows.append({'glob': cells[0].strip('`'), 'ids': cells[1], 'note': cells[2] if len(cells) > 2 else ''})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='.')
    ap.add_argument('--spec-dir', default='spec')
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    if not os.path.isdir(os.path.join(root, a.spec_dir)):
        bad('找不到规格目录：%s' % os.path.join(root, a.spec_dir))
        return report()

    rc, top = git('-C', root, 'rev-parse', '--show-toplevel')
    if rc != 0 or not top.strip():
        warn('不在 git 仓库内，跳过代码回写检查')
        return report()
    repo = top.strip()
    rel = os.path.relpath(root, repo).replace('\\', '/')

    rc2, _ = git('-C', repo, 'rev-parse', '--verify', 'HEAD')
    if rc2 != 0:
        warn('尚无 HEAD（首次提交），跳过代码回写检查')
        return report()

    # [1] 本次提交（staged）改了哪些文件
    print('[1] 本次提交的改动')
    # core.quotepath=false：中文文件名否则被转成八进制并加引号，路径匹配静默失效
    rc, out = git('-C', repo, '-c', 'core.quotepath=false',
                  'diff', '--cached', '--name-only', 'HEAD')
    if rc != 0:
        warn('git diff --cached 调用失败，跳过代码回写检查')
        return report()

    files = [l.strip() for l in out.split('\n') if l.strip()]
    code, spec, chg = [], [], []
    for f in files:
        kind = classify(f, rel, a.spec_dir)
        if kind == 'code':
            code.append(f)
        elif kind == 'spec':
            spec.append(f)
        elif kind == 'chg':
            chg.append(f)

    if not code:
        ok('本次提交无代码改动（纯规格/文档），不要求代码追溯')
        return report()

    ok('代码改动 %d 个文件，规格改动 %d 份，变更单改动 %d 份'
       % (len(code), len(spec), len(chg)))
    for f in code[:8]:
        print('       - ' + f)
    if len(code) > 8:
        print('       - ...（共 %d 个）' % len(code))

    # [2] 有代码改动 → 必须有变更单 + 追溯节
    print('[2] 变更单与追溯节')
    if not chg:
        bad('代码已改但 changes/ 下没有新增或修改的 CHG → 代码改了必须开单，'
            '单里写清影响到哪条规格')
        return report()

    ok('变更单已改动：%s' % ', '.join(os.path.basename(c) for c in chg[:3]))
    block, source = None, None
    for c in chg:
        p = os.path.join(repo, c)
        if os.path.exists(p):
            b = trace_block(read(p))
            if b is not None:
                block, source = b, c
                break
    if block is None:
        bad('变更单缺「## %s」节 → 无法判断这次代码改动影响了哪条规格' % TRACE_HEAD)
        print('       需要三行：规格影响 / 代码文件 / 说明（格式见 codemap.md 末尾）')
        return report()
    ok('追溯节存在：%s' % os.path.basename(source))

    # [3] 声明了规格影响 → 规格必须真改
    print('[3] 规格影响是否已回写')
    impact = trace_field(block, '规格影响')
    if impact is None:
        bad('追溯节缺「规格影响」字段（填 R/CT/API/AC 编号，或写「无」）')
    else:
        ids = SPEC_ID.findall(impact)
        if ids:
            if not spec:
                bad('声明影响 %s，但四份规格一份未改 → 声明了却没回写'
                    % ', '.join(ids[:6]))
            else:
                ok('声明影响 %s，且已改 %d 份规格' % (', '.join(ids[:6]), len(spec)))
        elif impact.strip() in ('无', 'none', 'None', '—', '-'):
            ok('声明规格影响为「无」，代码单飞已留痕')
            # 防止永远写「无」：命中了 codemap 就提示一下
            cm = load_codemap(root)
            if cm:
                hit = {}
                for f in code:  # f 已是相对仓库根的路径，与 codemap 同口径
                    for row in cm:
                        if fnmatch.fnmatch(f, row['glob']):
                            hit.setdefault(row['ids'], set()).add(f)
                if hit:
                    warn('但改到的模块在 codemap 里挂着规格：%s → 请确认真的无影响'
                         % '; '.join('%s(%s)' % (k, ', '.join(sorted(v))[:60]) for k, v in list(hit.items())[:3]))
        else:
            warn('「规格影响」既无编号也不等于「无」：%r，按未声明处理' % impact[:40])

    # [4] 追溯节列的代码路径必须真实，实际改动应被覆盖
    print('[4] 代码清单对账')
    listed = trace_field(block, '代码文件')
    if listed is None:
        warn('追溯节缺「代码文件」字段，无法对账')
    elif listed.strip() in ('无', '—', '-'):
        warn('声明「代码文件：无」，但本次实际改了 %d 个代码文件' % len(code))
    else:
        declared = [x.strip().strip('`') for x in re.split(r'[,，、\s]+', listed) if x.strip()]
        declared = [d for d in declared if '/' in d or '.' in d]
        actual = set(code)
        ghost = [d for d in declared if not any(a == d or a.endswith('/' + d) or d.endswith('/' + a)
                                                for a in actual)]
        if ghost:
            bad('追溯节列的代码文件本次并未改动（疑似事后编造）：%s' % ', '.join(ghost[:4]))
        else:
            ok('追溯节列的 %d 个代码文件均在本次改动中' % len(declared))
        uncovered = [a for a in actual
                     if not any(a == d or a.endswith('/' + d) or d.endswith('/' + a) for d in declared)]
        if uncovered:
            warn('实际改动未被追溯节覆盖 %d 个（测试与配置文件可忽略）：%s'
                 % (len(uncovered), ', '.join(sorted(uncovered)[:4])))

    return report()


def report():
    print('\n==== 代码回写门禁：%d 通过 / %d 失败 / %d 警告 ====' % (len(PASS), len(FAIL), len(WARN)))
    for f in FAIL:
        print('FAIL:', f)
    for w in WARN:
        print('WARN:', w)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
