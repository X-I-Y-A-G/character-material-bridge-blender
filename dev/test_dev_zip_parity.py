"""dev zip parity / 双 zip / release.json 判别器（纯 Python，无 Blender）。

覆盖（fused-spec-v2 I1 + §5）：
  1) I1：dev zip 内 __init__.py / core.py 与源码树逐字节 sha256 一致（零变换）；
  2) 双 zip 文件清单准确、testzip 通过；
  3) release.json v2 结构：variants.{dev,user} 双 SHA 可复算、顶层镜像字段沿旧、
     独立构建不含示例包字段；本地有完整示例时验证默认模式字段；
  4) user zip：README.md ← dev/README_user.md；__init__ 与 dev 版确实不同且禁词为 0。

构建输出写自动清理的唯一临时目录，绝不写生产 dist/。
CMB_TEST_OUT 可指定临时目录的父目录，不删除其中已有文件。
运行：python dev/test_dev_zip_parity.py
"""
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release as br  # noqa: E402


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def check_artifacts(out, summary, *, plugin_only):
    version = summary['version']

    dev_zip = out / f'character_material_bridge-{version}.zip'
    user_zip = out / f'character_material_bridge_user-{version}.zip'
    assert dev_zip.is_file() and user_zip.is_file(), 'zip artifacts missing'

    dev_expect = {'character_material_bridge/__init__.py', 'character_material_bridge/core.py',
                  'character_material_bridge/outline.py', 'character_material_bridge/runtime.py', 'character_material_bridge/mesh_state.py', 'character_material_bridge/animation.py',
                  'character_material_bridge/ruri_params_layout.json',
                  'character_material_bridge/README.md'}
    user_expect = {'character_material_bridge_user/__init__.py', 'character_material_bridge_user/core.py',
                   'character_material_bridge_user/outline.py', 'character_material_bridge_user/runtime.py', 'character_material_bridge_user/mesh_state.py', 'character_material_bridge_user/animation.py',
                   'character_material_bridge_user/ruri_params_layout.json',
                   'character_material_bridge_user/README.md'}
    for name in ('defaults.py', 'postprocess.py', 'lighting.py', 'rig_runtime.py', 'face_outline.py', 'hair_outline.py', 'fur.py', 'fur_layers.py', 'uv_transfer.py', 'color_management.py', 'assets/endfield_post.blend'):
        dev_expect.add('character_material_bridge/' + name)
        user_expect.add('character_material_bridge_user/' + name)

    with zipfile.ZipFile(dev_zip) as dev, zipfile.ZipFile(user_zip) as user:
        assert dev.testzip() is None and user.testzip() is None
        assert set(dev.namelist()) == dev_expect, dev.namelist()
        assert set(user.namelist()) == user_expect, user.namelist()

        # 1) I1 dev parity：两 py sha256 == 源码树。
        for name in ('__init__.py', 'core.py', 'outline.py', 'runtime.py', 'mesh_state.py', 'animation.py'):
            zipped = _sha(dev.read(f'character_material_bridge/{name}'))
            source = _sha((br.PLUGIN / name).read_bytes())
            assert zipped == source, f'I1 parity failed for {name}'
        # ruri 布局表与 README 同为零变换直拷。
        assert dev.read('character_material_bridge/ruri_params_layout.json') == \
            (br.PLUGIN / 'ruri_params_layout.json').read_bytes()
        assert dev.read('character_material_bridge/README.md') == \
            (br.PLUGIN / 'README.md').read_bytes()

        # 4) user zip 内容事实：README ← README_user.md；两版 __init__ 确实不同。
        assert user.read('character_material_bridge_user/README.md') == \
            (br.ROOT / 'dev' / 'README_user.md').read_bytes()
        user_init_text = user.read('character_material_bridge_user/__init__.py').decode('utf-8')
        assert user_init_text != dev.read('character_material_bridge/__init__.py').decode('utf-8')
        user_core_text = user.read('character_material_bridge_user/core.py').decode('utf-8')
        br.check_banned_words(user_init_text, user_core_text)  # 独立复跑禁词层

    # 3) release.json v2 结构断言。
    release = json.loads((out / 'release.json').read_text(encoding='utf-8'))
    assert release['version'] == version
    variants = release['variants']
    assert set(variants) == {'dev', 'user'}
    assert variants['dev']['addon_sha256'] == _sha(dev_zip.read_bytes()), 'dev SHA not recomputable'
    assert variants['user']['addon_sha256'] == _sha(user_zip.read_bytes()), 'user SHA not recomputable'
    assert set(variants['dev']['files']) == dev_expect
    assert set(variants['user']['files']) == user_expect
    # 顶层旧字段镜像（0.4.1 消费者兼容）：addon 镜像 dev 变体。
    assert release['addon'] == variants['dev']['addon']
    assert release['addon_sha256'] == variants['dev']['addon_sha256']
    package_fields = {'package', 'package_sha256', 'package_audit'}
    if plugin_only:
        assert package_fields.isdisjoint(release), 'plugin-only manifest contains example assets'
    else:
        assert Path(release['package']).is_file()
        assert release['package_sha256'] == _sha(Path(release['package']).read_bytes())
        assert isinstance(release['package_audit'], dict) and release['package_audit']
    # user 变体的 zip 与声明路径一致。
    assert Path(variants['user']['addon']) == user_zip.resolve()

    return {edition: record['addon_sha256'] for edition, record in variants.items()}


def main():
    base = os.environ.get('CMB_TEST_OUT') or None
    if base:
        Path(base).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cmb-zip-parity-', dir=base) as directory:
        out = Path(directory) / 'plugin-only'
        summary = br.build(out, plugin_only=True)
        fingerprints = check_artifacts(out, summary, plugin_only=True)
        if br.PACKAGE.is_file() and br.PACKAGE_AUDIT.is_file():
            legacy_out = Path(directory) / 'with-example'
            legacy = br.build(legacy_out)
            assert check_artifacts(legacy_out, legacy, plugin_only=False) == fingerprints
            print('default example-package integration: PASS')
        else:
            print('default example-package integration: SKIP (local example assets absent)')
        print(f'test_dev_zip_parity: ALL GREEN (version={summary["version"]}, plugin-only)')


if __name__ == '__main__':
    main()
