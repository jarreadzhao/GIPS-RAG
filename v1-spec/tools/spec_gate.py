# -*- coding: utf-8 -*-
"""四件套版本门禁：改了规格正文，必须升版并留下变更单。

用法：
    python spec_gate.py --root v1-spec

与 spec_link.py 的分工（两者都要跑，顺序：gate → link）：
    spec_gate  比对 git HEAD，回答「这次改动算不算一次变更」
    spec_link  只看当前文件，回答「四份是否互相对得上」

本脚本只做四件机器能判的事：
    [1] 找出血量：相对 git HEAD，四份规格里哪些被改过
    [2] 版本必须前进：被改过的文件，版本号必须大于 HEAD 里的版本
    [3] PRD 改动必须开单：changes/ 下要有新增或被修改的 CHG 文件
    [4] 下游必须跟版：PRD 升版后，三份的「上游版本」必须等于新版本

退出码：有 FAIL 返回 1，可直接挂 pre-commit。
无 git 仓库或无 HEAD（首次提交）时，[1]-[3] 降级为 WARN，只保留 [4]。
"""
import argparse
import io
import os
import re
import subprocess
import sys

FAIL, WARN, PASS = [], [], []

SPECS = [('prd', 'PRD.md'), ('contract', '契约.md'), ('api', '接口.md'), ('accept', '验收标准.md')]


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


def header(text, field):
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


def vtuple(v):
    return tuple(int(x) for x in v.split('.'))


def git(*args, **kw):
    """返回 (退出码, stdout 文本)。不做异常抛出，调用方判断退出码。"""
    try:
        p = subprocess.run(('git',) + args, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, **kw)
    except OSError:
        return 127, ''
    out = p.stdout.decode('utf-8', 'replace') if p.stdout else ''
    return p.returncode, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='.')
    ap.add_argument('--spec-dir', default='spec')
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    spec_dir = os.path.join(root, a.spec_dir)
    if not os.path.isdir(spec_dir):
        bad('找不到规格目录：%s' % spec_dir)
        return report()

    # 定位 git 仓库根与规格目录的相对路径
    rc, top = git('-C', root, 'rev-parse', '--show-toplevel')
    have_git = rc == 0 and top.strip() != ''
    repo = top.strip() if have_git else None
    rel = os.path.relpath(spec_dir, repo).replace('\\', '/') if have_git else None

    rc2, _ = git('-C', root, 'rev-parse', '--verify', 'HEAD')
    have_head = have_git and rc2 == 0

    cur = {}
    for role, name in SPECS:
        p = os.path.join(spec_dir, name)
        if os.path.exists(p):
            cur[role] = (p, read(p))
        else:
            warn('%s 不存在：%s' % (role, p))

    if not have_head:
        warn('无 git 仓库或尚无 HEAD，跳过「相对上一版」的三项检查（首次提交属正常）')
        check_downstream(cur)
        return report()

    # [1] 血
    print('[1] 相对 HEAD 的改动')
    # core.quotepath=false：否则中文文件名会被转成 "\346\221..." 并加引号，路径匹配全部失效
    rc, out = git('-C', repo, '-c', 'core.quotepath=false', 'status', '--porcelain', '--', rel)
    changed = {}
    if rc != 0:
        warn('git status 调用失败，降级处理')
    for line in out.split('\n'):
        if not line.strip():
            continue
        path = line[3:].strip().strip('"').replace('\\', '/')
        base = os.path.basename(path)
        for role, name in SPECS:
            if base == name and path.startswith(rel):
                changed[role] = line[:2].strip()
    if changed:
        ok('改动 %d 份：%s' % (len(changed), ', '.join('%s(%s)' % (r, s) for r, s in sorted(changed.items()))))
    else:
        ok('四份规格均未改动（纯代码改动）')
        print('       提醒：纯代码改动仍须能说出「不触及任何 R / CT / API / AC」，写进 CHG 或 commit message')

    # [2] 版本必须前进
    print('[2] 版本必须前进')
    old = {}
    for role, name in SPECS:
        rc, out = git('-C', repo, 'show', 'HEAD:%s/%s' % (rel, name))
        if rc == 0 and out:
            old[role] = version_of(out)
    bumped = {}
    for role, state in sorted(changed.items()):
        if role not in cur:
            continue
        new_v = version_of(cur[role][1])
        old_v = old.get(role)
        if not new_v:
            bad('%s 版本字段不可解析' % role)
            continue
        if old_v is None:
            warn('%s HEAD 中无旧版本，跳过比较' % role)
            continue
        if vtuple(new_v) <= vtuple(old_v):
            bad('%s 正文已改，版本仍为 v%s（HEAD 是 v%s）→ 改了内容必须升版'
                % (role, new_v, old_v))
        else:
            bumped[role] = (old_v, new_v)
            ok('%s v%s → v%s' % (role, old_v, new_v))

    # [3] PRD 改动必须开单
    print('[3] 变更单')
    prd_touched = 'prd' in changed
    chg_rel = os.path.normpath(os.path.join(rel, '..', 'changes')).replace('\\', '/')
    rc, out = git('-C', repo, '-c', 'core.quotepath=false', 'status', '--porcelain', '--', chg_rel)
    chg_touched = [l for l in out.split('\n') if 'CHG-' in l]
    if prd_touched:
        if chg_touched:
            ok('PRD 改动已伴随变更单改动 %d 处' % len(chg_touched))
            for l in chg_touched[:5]:
                print('       %s' % l.strip())
        else:
            bad('PRD 已改但 changes/ 下没有新增或修改的 CHG 文件 → 无单不认改动')
    else:
        ok('PRD 未改动，不要求新开变更单')

    # [3b] 变更单号是否指向真实文件
    for role in ('prd',) + tuple(r for r in bumped if r != 'prd'):
        if role not in cur:
            continue
        chg = header(cur[role][1], '变更单')
        if not chg:
            bad('%s 缺「变更单」字段' % role)
            continue
        cands = [os.path.join(root, 'changes', chg + '.md'),
                 os.path.join(root, chg + '.md'),
                 os.path.join(spec_dir, 'changes', chg + '.md')]
        if not any(os.path.exists(c) for c in cands):
            bad('%s 的变更单 %s 在 changes/ 下找不到' % (role, chg))

    check_downstream(cur)
    return report()


def check_downstream(cur):
    """[4] 下游三份的上游版本必须等于 PRD 当前版本。"""
    print('[4] 下游跟版')
    if 'prd' not in cur:
        warn('无 PRD，跳过')
        return
    prd_v = version_of(cur['prd'][1])
    if not prd_v:
        bad('PRD 版本不可解析')
        return
    for role in ('contract', 'api', 'accept'):
        if role not in cur:
            continue
        up = header(cur[role][1], '上游版本')
        m = re.search(r'v?(\d+(?:\.\d+)+)', up or '')
        up_v = m.group(1) if m else None
        if up_v != prd_v:
            bad('%s 上游版本 v%s ≠ PRD v%s' % (role, up_v, prd_v))
        else:
            ok('%s 上游版本 v%s' % (role, up_v))


def report():
    print('\n==== 版本门禁：%d 通过 / %d 失败 / %d 警告 ====' % (len(PASS), len(FAIL), len(WARN)))
    for f in FAIL:
        print('FAIL:', f)
    for w in WARN:
        print('WARN:', w)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
