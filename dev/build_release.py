"""Build both edition ZIPs (dev + user) for character_material_bridge (fused-spec-v2 §5).

编排：dev 零变换打包（I1）→ user 版内存变换 → 五层自检 → user zip → 双 zip 完整性
→ dev parity（I1）→ v0.5 包存在门禁 → release.json v2（variants + 顶层旧字段镜像）。
输出目录可参数化：--out（默认 dist/）。任何一层失配即整体失败（fail loud，C2）。

用法：python dev/build_release.py [--out DIR] [--plugin-only]
--plugin-only 仅构建插件，不读取本地示例材质包及其审计文件。
"""
import argparse
import ast
import hashlib
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import user_transform  # noqa: E402  同目录（脚本运行时 dev/ 本就在 sys.path）

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'character_material_bridge'
PACKAGE = ROOT / 'packages' / 'jue_A_无模型材质包_v0.5.blend'
PACKAGE_AUDIT = ROOT / 'dev' / 'package_audit_v05pkg.json'

DEV_DIR_IN_ZIP = 'character_material_bridge'
USER_DIR_IN_ZIP = 'character_material_bridge_user'
DEV_NAMES = {f'{DEV_DIR_IN_ZIP}/__init__.py', f'{DEV_DIR_IN_ZIP}/core.py',
             f'{DEV_DIR_IN_ZIP}/outline.py', f'{DEV_DIR_IN_ZIP}/runtime.py', f'{DEV_DIR_IN_ZIP}/mesh_state.py', f'{DEV_DIR_IN_ZIP}/animation.py',
             f'{DEV_DIR_IN_ZIP}/ruri_params_layout.json', f'{DEV_DIR_IN_ZIP}/README.md'}
USER_NAMES = {f'{USER_DIR_IN_ZIP}/__init__.py', f'{USER_DIR_IN_ZIP}/core.py',
              f'{USER_DIR_IN_ZIP}/outline.py', f'{USER_DIR_IN_ZIP}/runtime.py', f'{USER_DIR_IN_ZIP}/mesh_state.py', f'{USER_DIR_IN_ZIP}/animation.py',
              f'{USER_DIR_IN_ZIP}/ruri_params_layout.json', f'{USER_DIR_IN_ZIP}/README.md'}
ZIP_DATE = (1980, 1, 1, 0, 0, 0)  # 固定时间戳：同一源码树产出可复现的 zip/sha256
MIN_BLENDER_VERSION = (5, 2, 0)
EXTRA_FILES = ('defaults.py', 'postprocess.py', 'lighting.py', 'rig_runtime.py', 'face_outline.py', 'hair_outline.py', 'fur.py', 'fur_layers.py', 'uv_transfer.py', 'color_management.py', 'assets/endfield_post.blend')
DEV_NAMES.update(f'{DEV_DIR_IN_ZIP}/{name}' for name in EXTRA_FILES)
USER_NAMES.update(f'{USER_DIR_IN_ZIP}/{name}' for name in EXTRA_FILES)

# 检查④禁词表（v2-A08 写死；「EXPORT」/ExportHelper/「最终包」等合法存活 token 不在表内）。
INIT_BANNED_TOKENS = ('cmb.export_package', 'cmb.export_final_package', 'CMB_OT_export',
                      'CMB_OT_export_final', 'export_package', 'export_final_package', '修复脸部/身体描边亮度')
CORE_BANNED_TOKENS = ('def export_package', 'def export_final_package', 'class LibraryBuilder',
                      '_final_mapper', 'SAFE_MODS', 'import uuid')

# 检查⑤交叉复核：编排层独立持有的字面期望（不引用 user_transform 的表，避免同源循环）。
EXPECTED_RULE_HITS = {
    '__init__.py': {'U1': 1, 'U2': 1, 'U3': 1, 'U4': 1, 'U5': 1, 'U6': 1, 'U8': 1, 'U10': 1,
                    'U11': 1, 'U12': 1, 'U13': 1, 'U14': 1, 'U15': 3, 'U16': 1},
    'core.py': {'K1': 1, 'K2': 1, 'K3': 1, 'K4': 1, 'K5': 1, 'K6': 1, 'K7': 1, 'K8': 1,
                'K9': 1, 'K10': 1},
}
USER_CLASSES_COUNT = 33  # 0.5.23 adds the shared outline width editor.


class BuildCheckError(Exception):
    """Five-layer self-check or packaging gate failure (fail loud)."""


# ---------------------------------------------------------------------------
# 五层自检（fused-spec-v2 §5）

def check_compile(user_init_text, user_core_text):
    """① 双文件可编译。"""
    compile(user_init_text, f'{USER_DIR_IN_ZIP}/__init__.py', 'exec')
    compile(user_core_text, f'{USER_DIR_IN_ZIP}/core.py', 'exec')


def _bl_info(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'bl_info'
                                                 for t in node.targets):
            return ast.literal_eval(node.value) if isinstance(node.value, ast.Dict) else None
    return None


def check_blender_minimum(info, label):
    if not isinstance(info, dict) or info.get('blender') != MIN_BLENDER_VERSION:
        raise BuildCheckError(f'{label}: minimum Blender version must be {MIN_BLENDER_VERSION}')


def check_ast(user_init_text):
    """② AST：无导出类 / CLASSES 恰 33 类 / bl_info 与 Blender 最低版本校验。"""
    tree = ast.parse(user_init_text)
    export_classes = {'CMB_OT_export', 'CMB_OT_export_final'}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in export_classes:
            raise BuildCheckError(f'② user __init__.py still defines export class {node.name} '
                                  f'at line {node.lineno}')
    classes = next((node for node in tree.body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == 'CLASSES' for t in node.targets)), None)
    if classes is None or not isinstance(classes.value, ast.Tuple):
        raise BuildCheckError('② user __init__.py: module-level CLASSES tuple not found')
    elements = classes.value.elts
    if len(elements) != USER_CLASSES_COUNT:
        raise BuildCheckError(f'② user __init__.py: CLASSES has {len(elements)} entries, '
                              f'expected exactly {USER_CLASSES_COUNT}')
    top_classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    for element in elements:
        if not (isinstance(element, ast.Name) and element.id in top_classes):
            raise BuildCheckError(f'② user __init__.py: CLASSES entry {ast.dump(element)[:60]} '
                                  'is not a defined top-level class')
    info = _bl_info(tree)
    if not isinstance(info, dict):
        raise BuildCheckError('② user __init__.py: bl_info dict not readable')
    check_blender_minimum(info, 'user __init__.py')
    for key, expected in (('name', user_transform.USER_NAME),
                          ('location', user_transform.USER_LOCATION),
                          ('description', user_transform.USER_DESCRIPTION)):
        if info.get(key) != expected:
            raise BuildCheckError(f'② user __init__.py: bl_info[{key!r}] != expected constant '
                                  f'({info.get(key)!r} != {expected!r})')


def check_references(user_init_text, user_core_text):
    """③ 双断言：a) 被删符号代码级引用为 0（AST Name/Attribute，注释豁免）；
    b) __init__ 全部 core.X 引用在 user core 中存在。"""
    deleted = user_transform.DELETED_INIT_SYMBOLS | user_transform.DELETED_CORE_SYMBOLS
    for label, text in (('user __init__.py', user_init_text), ('user core.py', user_core_text)):
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Name) and node.id in deleted:
                raise BuildCheckError(f'③a {label}: code-level reference to deleted symbol '
                                      f'{node.id!r} at line {node.lineno}')
            if isinstance(node, ast.Attribute) and node.attr in deleted:
                raise BuildCheckError(f'③a {label}: code-level attribute reference to deleted '
                                      f'symbol {node.attr!r} at line {node.lineno}')
    refs = {node.attr for node in ast.walk(ast.parse(user_init_text))
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == 'core'}
    defined = set()
    for node in ast.parse(user_core_text).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name_node in ast.walk(target):
                    if isinstance(name_node, ast.Name):
                        defined.add(name_node.id)
    missing = sorted(refs - defined)
    if missing:
        raise BuildCheckError(f'③b user __init__.py references core.X absent from user core.py: '
                              f'{missing}')


def check_banned_words(user_init_text, user_core_text):
    """④ 禁词（词表 §5-④ 写死；requirements 用 AST 定义级断言避开存活字符串键）。"""
    for token in INIT_BANNED_TOKENS:
        hits = user_init_text.count(token)
        if hits:
            raise BuildCheckError(f'④ user __init__.py: banned token {token!r} occurs {hits}x')
    for token in CORE_BANNED_TOKENS:
        hits = user_core_text.count(token)
        if hits:
            raise BuildCheckError(f'④ user core.py: banned token {token!r} occurs {hits}x')
    info = _bl_info(ast.parse(user_init_text))
    if info is None or info.get('description') != user_transform.USER_DESCRIPTION:
        raise BuildCheckError('④ user __init__.py: bl_info description != expected constant')
    if '导出' in user_transform.USER_DESCRIPTION:
        raise BuildCheckError('④ USER_DESCRIPTION constant itself contains 「导出」')
    for node in ast.walk(ast.parse(user_core_text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'requirements':
            raise BuildCheckError(f'④ user core.py: function definition `requirements` survives '
                                  f'at line {node.lineno}')


def check_rule_hits(reports):
    """⑤ 变换器自报命中数 × 编排层独立字面期望 交叉复核。"""
    for filename, report in reports.items():
        expected = EXPECTED_RULE_HITS.get(filename)
        if expected is None:
            raise BuildCheckError(f'⑤ no orchestrator-side expectation table for {filename!r}')
        reported = {entry['id']: entry['hits'] for entry in report['rules']}
        if set(reported) != set(expected):
            raise BuildCheckError(f'⑤ {filename}: rule id set mismatch '
                                  f'(reported {sorted(reported)} vs expected {sorted(expected)})')
        for rule_id, hits in sorted(reported.items()):
            if hits != expected[rule_id]:
                raise BuildCheckError(f'⑤ {filename}/{rule_id}: self-reported hits {hits} != '
                                      f'orchestrator expectation {expected[rule_id]}')


def check_user_edition(user_init_text, user_core_text, reports):
    """五层自检按序执行；任一层失败即 BuildCheckError。"""
    check_compile(user_init_text, user_core_text)
    check_ast(user_init_text)
    check_references(user_init_text, user_core_text)
    check_banned_words(user_init_text, user_core_text)
    check_rule_hits(reports)


# ---------------------------------------------------------------------------
# 打包

def read_version():
    tree = ast.parse((PLUGIN / '__init__.py').read_text(encoding='utf-8'))
    info_node = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'bl_info' for t in n.targets))
    return '.'.join(map(str, ast.literal_eval(info_node)['version']))


def _write_zip(path, entries, expected_names):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as out:
        for arcname, data in entries:
            info = zipfile.ZipInfo(arcname, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            out.writestr(info, data)
    with zipfile.ZipFile(path) as check:
        if check.testzip() is not None:
            raise BuildCheckError(f'zip integrity failure: {path}')
        if set(check.namelist()) != expected_names:
            raise BuildCheckError(f'zip content mismatch for {path.name}: '
                                  f'{sorted(check.namelist())} != {sorted(expected_names)}')


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def build(out_dir, *, plugin_only=False):
    """Produce both ZIPs + release.json under `out_dir`; returns the summary dict."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    py_sources = sorted(PLUGIN.glob('*.py'), key=lambda p: p.name)
    if {p.name for p in py_sources} != {'__init__.py', 'core.py', 'outline.py', 'runtime.py', 'mesh_state.py', 'animation.py', 'defaults.py', 'postprocess.py', 'lighting.py', 'rig_runtime.py', 'face_outline.py', 'hair_outline.py', 'fur.py', 'fur_layers.py', 'uv_transfer.py', 'color_management.py'}:
        raise BuildCheckError(f'unexpected top-level *.py set in plugin dir: '
                              f'{sorted(p.name for p in py_sources)}')
    src_init_bytes = (PLUGIN / '__init__.py').read_bytes()
    check_blender_minimum(_bl_info(ast.parse(src_init_bytes)), 'dev __init__.py')
    src_core_bytes = (PLUGIN / 'core.py').read_bytes()
    outline_bytes = (PLUGIN / 'outline.py').read_bytes()
    compile(outline_bytes.decode('utf-8'), 'outline.py', 'exec')
    runtime_bytes = (PLUGIN / 'runtime.py').read_bytes()
    compile(runtime_bytes.decode('utf-8'), 'runtime.py', 'exec')
    animation_bytes = (PLUGIN / 'animation.py').read_bytes()
    compile(animation_bytes.decode('utf-8'), 'animation.py', 'exec')
    mesh_state_bytes = (PLUGIN / 'mesh_state.py').read_bytes()
    compile(mesh_state_bytes.decode('utf-8'), 'mesh_state.py', 'exec')
    layout_bytes = (PLUGIN / 'ruri_params_layout.json').read_bytes()
    readme_bytes = (PLUGIN / 'README.md').read_bytes()
    readme_user_bytes = (ROOT / 'dev' / 'README_user.md').read_bytes()
    version = read_version()
    extra = [(name, (PLUGIN/name).read_bytes()) for name in EXTRA_FILES]
    for name, data in extra:
        if name.endswith('.py'):compile(data.decode('utf-8'), name, 'exec')

    # 1) dev zip：零变换（I1——字节级直拷源码树）。
    dev_zip = out_dir / f'character_material_bridge-{version}.zip'
    _write_zip(dev_zip, [
        (f'{DEV_DIR_IN_ZIP}/__init__.py', src_init_bytes),
        (f'{DEV_DIR_IN_ZIP}/core.py', src_core_bytes),
        (f'{DEV_DIR_IN_ZIP}/outline.py', outline_bytes),
        (f'{DEV_DIR_IN_ZIP}/runtime.py', runtime_bytes),
        (f'{DEV_DIR_IN_ZIP}/mesh_state.py', mesh_state_bytes),
        (f'{DEV_DIR_IN_ZIP}/animation.py', animation_bytes),
        (f'{DEV_DIR_IN_ZIP}/ruri_params_layout.json', layout_bytes),
        (f'{DEV_DIR_IN_ZIP}/README.md', readme_bytes),
    ] + [(f'{DEV_DIR_IN_ZIP}/{name}', data) for name, data in extra], DEV_NAMES)

    # 2) user 版变换（内存，不落盘中间产物）。
    user_init_text, init_report = user_transform.transform_user_verbose(
        src_init_bytes.decode('utf-8'), '__init__.py')
    user_core_text, core_report = user_transform.transform_user_verbose(
        src_core_bytes.decode('utf-8'), 'core.py')

    # 3) 五层自检。
    check_user_edition(user_init_text, user_core_text,
                       {'__init__.py': init_report, 'core.py': core_report})

    # 4) user zip：顶层 character_material_bridge_user/，README.md ← dev/README_user.md。
    user_zip = out_dir / f'character_material_bridge_user-{version}.zip'
    _write_zip(user_zip, [
        (f'{USER_DIR_IN_ZIP}/__init__.py', user_init_text.encode('utf-8')),
        (f'{USER_DIR_IN_ZIP}/core.py', user_core_text.encode('utf-8')),
        (f'{USER_DIR_IN_ZIP}/outline.py', outline_bytes),
        (f'{USER_DIR_IN_ZIP}/runtime.py', runtime_bytes),
        (f'{USER_DIR_IN_ZIP}/mesh_state.py', mesh_state_bytes),
        (f'{USER_DIR_IN_ZIP}/animation.py', animation_bytes),
        (f'{USER_DIR_IN_ZIP}/ruri_params_layout.json', layout_bytes),
        (f'{USER_DIR_IN_ZIP}/README.md', readme_user_bytes),
    ] + [(f'{USER_DIR_IN_ZIP}/{name}', data) for name, data in extra], USER_NAMES)

    # 5) dev parity（I1）：dev zip 内两 py 与源码树 sha256 一致。
    with zipfile.ZipFile(dev_zip) as check:
        for name in ('__init__.py', 'core.py', 'outline.py', 'runtime.py', 'mesh_state.py', 'animation.py') + EXTRA_FILES:
            if _sha256(check.read(f'{DEV_DIR_IN_ZIP}/{name}')) != \
                    _sha256((PLUGIN / name).read_bytes()):
                raise BuildCheckError(f'I1 dev-zip parity failed for {name}')

    # 6) 默认模式保留 v0.5 包门禁；源码仓的独立构建不读取示例资产。
    package_metadata = {}
    if not plugin_only:
        if not PACKAGE.exists():
            raise BuildCheckError('v0.5 package missing: ' + str(PACKAGE))
        package_metadata = {
            'package': str(PACKAGE),
            'package_sha256': _sha256(PACKAGE.read_bytes()),
            'package_audit': json.loads(PACKAGE_AUDIT.read_text(encoding='utf-8')),
        }

    # 7) release.json v2：variants.{dev,user} + 顶层旧字段镜像（addon/addon_sha256 指向
    #    dev 变体；package/package_audit 沿旧）。
    dev_sha, user_sha = _sha256(dev_zip.read_bytes()), _sha256(user_zip.read_bytes())
    summary = {
        'version': version,
        'addon': str(dev_zip.resolve()),
        'addon_sha256': dev_sha,
        **package_metadata,
        'variants': {
            'dev': {'addon': str(dev_zip.resolve()), 'addon_sha256': dev_sha,
                    'module': DEV_DIR_IN_ZIP, 'files': sorted(DEV_NAMES)},
            'user': {'addon': str(user_zip.resolve()), 'addon_sha256': user_sha,
                     'module': USER_DIR_IN_ZIP, 'files': sorted(USER_NAMES)},
        },
    }
    (out_dir / 'release.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', default=str(ROOT / 'dist'), type=Path,
                        help='output directory for the two ZIPs + release.json (default: dist/)')
    parser.add_argument('--plugin-only', action='store_true',
                        help='build plugin ZIPs without a local example material package or its audit')
    args = parser.parse_args(argv)
    summary = build(args.out, plugin_only=args.plugin_only)
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
