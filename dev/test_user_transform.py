"""user_transform 判别器（纯 Python，无 Blender）。

覆盖（fused-spec-v2 §6 test_user_transform）：
  1) 当前源码全规则 pass + 变换产物可编译；
  2) AST：无导出类 / CLASSES 恰 33 / bl_info 三值及 Blender 最低版本准确；
  3) 禁词 0（__init__ 六 token + core 六 token + requirements AST 级）；
  4) 双断言（被删符号代码级引用 0 + core.X 闭包）；
  5) 负例：U13 子串消失 → BuildError；export_package 改名 → BuildError；
     按钮语句改多行调用 → 仍整句删除且可编译。
运行：python dev/test_user_transform.py
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release as br  # noqa: E402
import user_transform as ut  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / 'character_material_bridge'


def expect_build_error(mutated_text, filename, needle_in_message, case):
    try:
        ut.transform_user(mutated_text, filename)
    except ut.BuildError as exc:
        message = str(exc)
        if needle_in_message not in message:
            raise SystemExit(f'FAIL {case}: BuildError raised but message lacks '
                             f'{needle_in_message!r}: {message}')
        return
    raise SystemExit(f'FAIL {case}: expected BuildError, transform succeeded')


def main():
    src_init = (SRC / '__init__.py').read_text(encoding='utf-8')
    src_core = (SRC / 'core.py').read_text(encoding='utf-8')

    # 1) 全规则 pass + 编译。
    user_init, report_init = ut.transform_user_verbose(src_init, '__init__.py')
    user_core, report_core = ut.transform_user_verbose(src_core, 'core.py')
    compile(user_init, '<user __init__>', 'exec')
    compile(user_core, '<user core>', 'exec')
    hits = {f: {r['id']: r['hits'] for r in rep['rules']} for f, rep in
            (('__init__.py', report_init), ('core.py', report_core))}
    assert hits['__init__.py'] == {'U1': 1, 'U2': 1, 'U3': 1, 'U4': 1, 'U5': 1, 'U6': 1, 'U8': 1,
                                   'U10': 1, 'U11': 1, 'U12': 1, 'U13': 1, 'U14': 1, 'U15': 3, 'U16': 1}, \
        f'unexpected self-reported hits: {hits}'
    assert hits['core.py'] == {'K1': 1, 'K2': 1, 'K3': 1, 'K4': 1, 'K5': 1, 'K6': 1, 'K7': 1,
                               'K8': 1, 'K9': 1, 'K10': 1}, f'unexpected self-reported hits: {hits}'
    # 结构抽查：详情框整块消失而列表保留；CLASSES 行仍存在。
    assert '确认源组合' not in user_init
    assert '修复脸部/身体描边亮度' in src_init and '修复脸部/身体描边亮度' not in user_init
    assert 'template_list' in user_init
    assert 'CLASSES' in user_init
    assert 'separator()' in user_init  # U7/U9 撤销：separator 保留（A01）

    # 2) AST 判别（build_release 检查②，独立复跑）。
    br.check_ast(user_init)
    old_minimum = user_init.replace("'blender': (5, 2, 0)", "'blender': (4, 2, 0)", 1)
    assert old_minimum != user_init
    try:
        br.check_ast(old_minimum)
    except br.BuildCheckError as exc:
        assert 'minimum Blender version' in str(exc)
    else:
        raise AssertionError('user edition with an outdated Blender minimum was accepted')
    tree = ast.parse(user_init)
    classes = next(n for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'CLASSES' for t in n.targets))
    assert len(classes.value.elts) == 33
    assert not {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)} & \
        {'CMB_OT_export', 'CMB_OT_export_final'}

    # 3)+4) 禁词 0 与双断言（build_release 检查③④，独立复跑）。
    br.check_banned_words(user_init, user_core)
    br.check_references(user_init, user_core)

    # 5a) 负例：源码中 U13 子串消失（源码演化）→ BuildError。
    guard_substring = " and hasattr(core, 'export_final_package')"
    mutated = src_init.replace(guard_substring, '', 1)
    assert mutated != src_init, 'negative-case fixture failed to mutate source'
    expect_build_error(mutated, '__init__.py', 'U13', 'U13-substring-missing')

    # 5b) 负例：core.export_package 改名 → K9 命中 0 → BuildError。
    mutated_core = src_core.replace('def export_package(', 'def my_export(', 1)
    assert mutated_core != src_core
    expect_build_error(mutated_core, 'core.py', 'K9', 'export-package-renamed')

    # 5c) 按钮语句改成多行调用 → 仍整句（跨行）删除，产物可编译。
    one_line = "layout.operator('cmb.export_package', icon='EXPORT')"
    two_line = "layout.operator('cmb.export_package',\n                    icon='EXPORT')"
    mutated2 = src_init.replace(one_line, two_line, 1)
    assert mutated2 != src_init, 'multi-line fixture failed to mutate source'
    out2 = ut.transform_user(mutated2, '__init__.py')
    assert 'cmb.export_package' not in out2, 'multi-line operator call survived the transform'
    assert "icon='EXPORT')" not in out2, 'continuation line of the statement survived'
    compile(out2, '<user __init__ multiline>', 'exec')

    print('test_user_transform: ALL GREEN '
          f'({len(hits["__init__.py"]) + len(hits["core.py"])} rules, 4 negative cases)')


if __name__ == '__main__':
    main()
