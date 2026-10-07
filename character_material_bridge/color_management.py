"""Portable image semantics and operation-time OCIO compatibility checks.

No scene access at import/register time. Never change the active OCIO config.
The standalone entry point reads images only in a factory-startup worker.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

import bpy

API_REVISION = 526
VERSION = 1
KEY = 'cmb_image_color'
_config_cache = {}
_legacy_cache = {}
_SRGB = ('srgb_rec709_scene', 'srgb_texture', 'sRGB - Texture',
         'Utility - sRGB - Texture', 'sRGB Encoded Rec.709 (sRGB)', 'sRGB')
_REC709 = ('lin_rec709_scene', 'Linear Rec.709', 'Linear Rec.709 (sRGB)')


def _native_config():
    # system_resource resolves directories, not individual files.
    directory = bpy.utils.system_resource('DATAFILES', path='colormanagement')
    return str(Path(directory) / 'config.ocio') if directory else ''


def _config():
    """Blender initializes OCIO to its active path before Python starts."""
    import PyOpenColorIO as ocio
    path = os.environ.get('OCIO') or _native_config()
    if not path:
        raise RuntimeError('无法读取当前 OCIO 配置')
    if path not in _config_cache:
        cfg = ocio.Config.CreateFromBuiltinConfig(path[7:]) if path.startswith('ocio://') else ocio.Config.CreateFromFile(path)
        _config_cache.clear()
        _config_cache[path] = cfg
    return _config_cache[path]


def _canonical(cfg, aliases):
    for name in aliases:
        space = cfg.getColorSpace(name)
        if space is not None and not space.isData():
            return space.getName()
    return None


def validate_descriptor(data):
    if (not isinstance(data, dict) or type(data.get('version')) is not int
            or data['version'] != VERSION or data.get('kind') not in {'DATA', 'SRGB', 'NAMED'}
            or not isinstance(data.get('name'), str) or not data['name']):
        raise RuntimeError('无效的图像色彩空间标记，请重新导出材质包')
    return dict(data)


def marker(image):
    value = image.get(KEY)
    if value is None:
        return None
    if hasattr(value, 'to_dict'):
        value = value.to_dict()
    return validate_descriptor(value)


def describe(image):
    settings = image.colorspace_settings
    name = settings.name
    if not name:
        raise RuntimeError('图像色彩空间已丢失，无法确定用途: ' + image.name)
    if settings.is_data:
        kind = 'DATA'
    else:
        try:
            cfg = _config()
            space = cfg.getColorSpace(name)
            srgb = _canonical(cfg, _SRGB)
            kind = 'SRGB' if space is not None and space.getName() == srgb else 'NAMED'
            if space is not None:name = space.getName()
        except ImportError:
            kind = 'SRGB' if name in _SRGB else 'NAMED'
    return {'version': VERSION, 'kind': kind, 'name': name}


def record(image, descriptor=None):
    data = validate_descriptor(descriptor) if descriptor is not None else describe(image)
    image[KEY] = data
    return data


def set_data(image, annotate=True):
    """Select the config's data role, preserving pixels and alpha semantics."""
    try:
        image.colorspace_settings.is_data = True
        if not image.colorspace_settings.is_data or not image.colorspace_settings.name:
            raise RuntimeError('当前 OCIO 没有可用的数据色彩空间')
    except Exception as exc:
        raise RuntimeError('无法将图像设为非颜色数据: ' + image.name + '；请检查 OCIO 的 data 角色') from exc
    if annotate:
        record(image, {'version': VERSION, 'kind': 'DATA', 'name': image.colorspace_settings.name})


def _set_srgb(image):
    try:
        name = _canonical(_config(), _SRGB)
        if name is None:
            raise RuntimeError('当前 OCIO 没有明确的 sRGB 纹理空间')
        image.colorspace_settings.name = name
    except ImportError:
        # Blender builds without Python OCIO bindings can still use explicitly
        # named texture spaces. Never substitute an ACES display transform.
        for name in reversed(_SRGB):
            try:
                image.colorspace_settings.name = name
                break
            except TypeError:
                continue
        else:
            raise RuntimeError('当前 OCIO 没有明确的 sRGB 纹理空间')
    if image.colorspace_settings.is_data:
        raise RuntimeError('sRGB 纹理空间被标记为数据空间')


def assign(image, descriptor):
    data = validate_descriptor(descriptor)
    try:
        if data['kind'] == 'DATA':
            set_data(image, annotate=False)
        elif data['kind'] == 'SRGB':
            _set_srgb(image)
        else:
            name = data['name']
            try:
                space = _config().getColorSpace(name)
                if space is not None:name = space.getName()
            except ImportError:
                pass
            image.colorspace_settings.name = name
            if image.colorspace_settings.is_data:
                raise RuntimeError('颜色图的具名空间被解析成了数据空间')
    except Exception as exc:
        raise RuntimeError('无法恢复图像色彩空间: ' + image.name + ' / ' + data['name'] + '；' + str(exc)) from exc
    record(image, data)


def _fingerprint(path):
    path = Path(path).resolve()
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError('材质包在读取期间发生变化，请重试')
    return (str(path), after.st_size, after.st_mtime_ns, digest.hexdigest())


def legacy_descriptors(filepath):
    """Read missing metadata once; no objects, scenes, or auto-executed code."""
    key = _fingerprint(filepath)
    if key in _legacy_cache:
        return _legacy_cache[key], True
    config = _native_config()
    binary = bpy.app.binary_path
    if not config or not Path(config).is_file() or not binary or not Path(binary).is_file():
        raise RuntimeError('无法启动旧包色彩空间辅助读取，请检查 Blender 的默认 OCIO 和可执行文件')
    with tempfile.TemporaryDirectory(prefix='cmb-image-color-') as directory:
        folder = Path(directory)
        output = folder / 'images.json'
        env = os.environ.copy()
        env['OCIO'] = config
        for name in ('OCIO_ACTIVE_DISPLAYS', 'OCIO_ACTIVE_VIEWS', 'PYTHONPATH'):
            env.pop(name, None)
        for name, suffix in (('BLENDER_USER_CONFIG', 'config'), ('BLENDER_USER_SCRIPTS', 'scripts'),
                             ('BLENDER_USER_DATAFILES', 'datafiles')):
            child = folder / suffix
            child.mkdir()
            env[name] = str(child)
        command = [binary, '--background', '--factory-startup', '--disable-autoexec',
                   '--threads', '1', '--python-exit-code', '1', '--python', str(Path(__file__).resolve()),
                   '--', key[0], str(output)]
        try:
            proc = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  encoding='utf8', errors='replace', timeout=60,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('旧包色彩空间辅助读取失败: ' + str(exc)) from exc
        if proc.returncode or not output.is_file():
            raise RuntimeError('旧包色彩空间辅助读取失败；请检查材质包完整性。' + proc.stdout[-1200:])
        data = json.loads(output.read_text(encoding='utf8'))
        if not isinstance(data, dict):
            raise RuntimeError('旧包图像信息无效')
        data = {name: validate_descriptor(value) for name, value in data.items()}
    if _fingerprint(filepath) != key:
        raise RuntimeError('材质包在辅助读取期间发生变化，请重试')
    if len(_legacy_cache) >= 8:
        _legacy_cache.pop(next(iter(_legacy_cache)))
    _legacy_cache[key] = data
    return data, False


def restore_loaded(filepath, pairs, required, descriptors=None):
    """pairs come directly from libraries.load, never suffix/name guessing."""
    pairs = [(name, image) for name, image in pairs if image is not None and image in required]
    descriptors = descriptors or {}
    unresolved = [(name, image) for name, image in pairs if name not in descriptors
                  and marker(image) is None and not image.colorspace_settings.name]
    legacy, cached = legacy_descriptors(filepath) if unresolved else ({}, False)
    report = {'images': len(pairs), 'legacy_read': bool(unresolved), 'legacy_cached': cached,
              'data': 0, 'srgb': 0, 'named': 0}
    for name, image in pairs:
        data = descriptors.get(name) or marker(image)
        if data is None:
            data = legacy.get(name) if not image.colorspace_settings.name else describe(image)
        if data is None:
            raise RuntimeError('旧包缺少可恢复的图像用途: ' + name)
        assign(image, data)
        report[data['kind'].lower()] += 1
    return report


def _standard_matches(cfg, display):
    """A view called Standard may still contain AgX/ACES/a custom look."""
    import PyOpenColorIO as ocio
    native = ocio.Config.CreateFromFile(_native_config())
    if display not in native.getDisplays():
        return False
    reference = native.getProcessor('scene_linear', display, 'Standard',
                                    ocio.TRANSFORM_DIR_FORWARD).getDefaultCPUProcessor()
    current = cfg.getProcessor('scene_linear', display, 'Standard',
                              ocio.TRANSFORM_DIR_FORWARD).getDefaultCPUProcessor()
    # Include shadows, negative values, saturated channels and HDR. Checking
    # only [0, 1] or grayscale can miss gamut changes and highlight compression.
    samples = [0, 0, 0, .0001, .0001, .0001, .005, .005, .005,
               .18, .18, .18, 1, 1, 1, 4, 4, 4, 16, 16, 16, 64, 64, 64,
               1, 0, 0, 0, 1, 0, 0, 0, 1, .75, .2, .05,
               .05, .4, .8, 4, .1, .25, .1, 8, .2, -.125, .25, .5]
    expected = reference.applyRGB(samples)
    actual = current.applyRGB(samples)
    return all(math.isfinite(a) and math.isfinite(b)
               and abs(a-b) <= 1e-5 * max(1.0, abs(b)) for a, b in zip(actual, expected))


def postprocess_compatibility(scene):
    """Native post is calibrated for linear Rec.709 and an untonemapped view."""
    try:
        cfg = _config()
        rec709 = _canonical(cfg, _REC709)
        if rec709 is None or not cfg.getProcessor('scene_linear', rec709).isNoOp():
            raise RuntimeError('当前场景线性色域与 Linear Rec.709 不等价')
        display = scene.display_settings.display_device
        if 'Standard' not in cfg.getViews(display):
            raise RuntimeError('当前显示设备没有 Standard 显示变换')
        if not _standard_matches(cfg, display):
            raise RuntimeError('当前 Standard 实际处理与原生显示转换不等价，可能包含额外色调映射或校色')
    except ImportError:
        # No external dependency for material transfer. For post, accept only
        # Blender's native config with its explicit linear Rec.709 role.
        native = _native_config()
        active = os.environ.get('OCIO') or native
        try:
            import re
            text = Path(native).read_text(encoding='utf8')
            if (Path(active).resolve() != Path(native).resolve()
                    or not re.search(r'^  scene_linear: Linear Rec\.709\s*$', text, re.M)):
                raise RuntimeError('无法验证自定义 OCIO 的线性色域，请保留原后处理')
        except Exception as exc:
            return {'status': 'ocio_incompatible', 'message': '已跳过终末地通用后处理：' + str(exc)}
    except Exception as exc:
        return {'status': 'ocio_incompatible', 'message': '已跳过终末地通用后处理：' + str(exc)}
    # RNA menus may be restricted by active views, so validate against Blender
    # too, on temporary IDs, without touching the user's scene or its backup.
    probe = bpy.data.scenes.new('CMB OCIO compatibility probe')
    image = None
    try:
        probe.display_settings.display_device = scene.display_settings.display_device
        probe.view_settings.view_transform = 'Standard'
        probe.view_settings.look = 'None'
        image = bpy.data.images.new('CMB data compatibility probe', width=1, height=1, float_buffer=True)
        set_data(image, annotate=False)
    except Exception as exc:
        return {'status': 'ocio_incompatible', 'message': '已跳过终末地通用后处理：' + str(exc)}
    finally:
        if image is not None:
            bpy.data.images.remove(image)
        bpy.data.scenes.remove(probe)
    return {'status': 'compatible'}


def _worker():
    import sys
    args = sys.argv[sys.argv.index('--') + 1:]
    if len(args) != 2:
        raise RuntimeError('旧包图像辅助读取参数无效')
    filepath, output = args
    with bpy.data.libraries.load(filepath, link=False) as (source, dest):
        names = list(source.images)
        dest.images = list(names)
    data = {}
    for name, image in zip(names, dest.images):
        # Ignore unused Render Result / Viewer images. Required unresolved
        # images are rejected by restore_loaded instead of silently guessed.
        if image is not None and image.colorspace_settings.name:
            data[name] = marker(image) or describe(image)
    Path(output).write_text(json.dumps(data, ensure_ascii=False), encoding='utf8')


if __name__ == '__main__':
    _worker()
