"""Material libraries, source UV references, final meshes, and conservative matching."""
import hashlib
import json
import math
import os
import re
import uuid
from array import array
from collections import Counter
from datetime import datetime
from pathlib import Path

import bpy

FORMAT = 'character-material-bridge'
LEGACY_FORMAT = 'jue-material-bridge'  # 0.3.0 改名前（v0.1~v0.4 包）的格式标识；读取端继续接受
VERSION = 2  # v2 导出载体上全部材质槽并附 slot_used；读取端兼容 v1/v2。
API_REVISION = 527  # Render uploads use Blender image invalidation, not GPU free.
RUNTIME_BINDING_VERSION = 1
ADDON_VERSION = (0, 5, 26)
MANIFEST = 'CMB_MANIFEST.json'
LEGACY_MANIFEST = 'JMB_MANIFEST.json'  # 0.3.0 改名前的清单 Text 名；旧包（v0.1~v0.4）内仍是旧名
REPORT = 'CMB_Report.json'
TAG = 'cmb_role'
LEGACY_TAG = 'jmb_role'  # 0.3.0 改名前写入的角色 ID 键；识别端（restore/防重复/引用解析）新旧都认
SAFE_MODS = {'NODES', 'SUBSURF', 'SOLIDIFY', 'BEVEL', 'WEIGHTED_NORMAL', 'NORMAL_EDIT',
             'TRIANGULATE', 'EDGE_SPLIT', 'SMOOTH', 'CORRECTIVE_SMOOTH', 'LAPLACIANSMOOTH',
             'DECIMATE', 'WELD', 'MIRROR', 'ARRAY', 'DISPLACE', 'SIMPLE_DEFORM', 'CAST'}

# 名称桥接词典：MMD 侧中文区域词 → 源侧英文检索词。可按需扩展词条；刻意不收"影"这类单字歧义词。
# 2026-09-05 用户实测：eye 系词条（目/目白/目影）已全部撤除——该角色素材的 eye 系材质命名与
# 实际用途相反（M_eyewhiteshadow_common_01 实际承担目影视觉、目白区域在脸图集），词条化会误导；
# eye 系区域回归贴图+UV 证据与人工+预设。
NAME_BRIDGE = {
    '面': 'face', '髪': 'hair', '发': 'hair', '肌': 'body', '皮肤': 'skin',
    '睫': 'eyelash', '眉': 'eyebrow', '睫眉': 'eyebrow', '口内': 'mouth', '口': 'mouth',
    '发影': 'hairshadow', '髪影': 'hairshadow', '服': 'cloth', '衣': 'cloth',
}
EXACT_SCORE = 90    # 精确名
UV_WEIGHT = 40      # UV 直方图项（有共享贴图时才计入）
BRIDGE_MATERIAL_SCORE = 40  # 名称桥接·材质名命中（规范化 source_material 含检索词）
BRIDGE_OBJECT_SCORE = 12    # 名称桥接·仅对象名命中（与材质档不叠加；不参与 T4 唯一性）
AUTO_SCORE = 60     # T2 分数门槛
AUTO_MARGIN = 12    # T2/T4 领先门槛
PRESET_SUFFIX = '.mapping.json'
PARAMS_PRESET_SUFFIX = '.params.json'  # 0.4.0 参数预设（<包名>.params.json）

# 0.4.0 最终包防泄露扫描：MMD 资产特征命名常量表。闭包内任一数据块名/图像名（含图像
# filepath 基名）命中任一模式即整体拒绝导出。扫描目标是 MMD 工程侧命名；克隆材质/贴图
# 名里的 lizhiyan（李织烟罗马字）是 Ruri 源工程命名，允许、不在扫描范围。换目标角色/
# 工程时按其命名习惯扩展此表（模型中文名、日语骨骼别名等）。
MMD_NAME_PATTERNS = (
    (re.compile(r'mmd', re.IGNORECASE), 'MMD 标识（mmd_/mmd_tools 等）'),
    (re.compile(r'rigid', re.IGNORECASE), 'MMD 刚体（rigid/rigidbody）'),
    (re.compile(r'joint|\.jnt', re.IGNORECASE), 'MMD 关节（joint/joint.jnt）'),
    (re.compile(r'剛体|刚体|ジョイント'), 'MMD 刚体/关节（日文）'),
    (re.compile(r'李织烟'), 'MMD 模型中文名（按目标工程扩展）'),
    (re.compile(r'全ての親|センター|上半身|下半身|グルーブ|足ＩＫ|手ＩＫ|足首ＩＫ|手首|足首'),
     'MMD 标准骨骼名（按目标工程扩展）'),
)

# 0.2.3 脸部主光方向修正：源工程（RuriRipperImporter 约定）脸对象带 180° 绕 Z 旋转，
# WORLD→OBJECT 矢量变换在源/目标两侧产出的局部光方向差 180° → SDF 阴影等效转半圈。
# 只对源对象名含 face 的条目生效（用户视觉验收边界：cloth/hair/body/eye 系不需要）。
FACE_LIGHT_INPUT = 'C0_MainLight_direction'  # 主光方向链在 Face s1 组节点上的输入名（按图追踪定位，不按节点名硬编码）
FACE_LIGHT_FIX_PREFIX = 'CMB_LightDirFix'         # 0.3.0 起新插入修正节点的名字前缀，同时用于幂等判定
FACE_LIGHT_FIX_LEGACY_PREFIX = 'JMB_LightDirFix'  # 0.2.3 写入的旧前缀；幂等判定两种前缀都认
FACE_LIGHT_FIX_PREFIXES = (FACE_LIGHT_FIX_PREFIX, FACE_LIGHT_FIX_LEGACY_PREFIX)
FACE_LIGHT_DELTA_EPSILON = 0.01              # 欧拉差分量的"近零"阈值（rad）

# 0.3.0 改名（jue_material_bridge → character_material_bridge）：新数据一律写新名，
# 识别端对 0.2.x 时代写入的旧名（jue-material-bridge / JMB_MANIFEST.json / jmb_role /
# JMB_LightDirFix* / JMB_移植结果_*）保持兼容，旧包与既有移植结果无需迁移。
RESULT_COLLECTION_PREFIX = 'CMB_移植结果_'  # 0.3.0 起新建结果集合的名前缀
RESULT_COLLECTION_LEGACY_PREFIX = 'JMB_移植结果_'  # 0.2.x 写入的旧前缀；仅用于识别


def role_of(item):
    """Read the role recorded under the current or the legacy tag key (0.3.0 改名兼容)."""
    for key in (TAG, LEGACY_TAG):
        value = item.get(key)
        if value is not None:
            return value
    return None


def manifest_text_name(names):
    """Pure: which manifest Text name a package carries (new name first, then legacy)."""
    for name in (MANIFEST, LEGACY_MANIFEST):
        if name in names:
            return name
    return None


def is_result_collection_name(name):
    """Pure: recognize result-collection names under both the current and legacy prefix."""
    return str(name).startswith((RESULT_COLLECTION_PREFIX, RESULT_COLLECTION_LEGACY_PREFIX))


def validate_manifest(manifest):
    """Pure: format/version gate + v1 slot_used backfill; accepts legacy and current formats.

    0.5.0 final 包归一化（package_kind=='final' 时）：0.4.x final 条目只有精简键集，
    预览/应用链对 source_material/source_object/requirements 等硬取键会裸 KeyError——
    在读取门一次性补齐匹配与应用所需键（fused-spec §4 V1；A03/A04/A10）。非 final
    包逐分支行为与 0.4.1 完全一致（I6）。"""
    if not isinstance(manifest, dict):
        raise RuntimeError('材质包清单必须是 JSON 对象')
    if manifest.get('format') not in (FORMAT, LEGACY_FORMAT) or manifest.get('version') not in (1, VERSION):
        raise RuntimeError('不支持的包格式/版本')
    binding_version=manifest.get('runtime_binding_version')
    if binding_version is not None and (type(binding_version) is not int or binding_version != RUNTIME_BINDING_VERSION):
        raise RuntimeError('不支持的运行绑定版本，请升级材质桥')
    minimum = manifest.get('minimum_addon_version')
    if minimum is not None:
        if not isinstance(minimum, (tuple, list)) or len(minimum) != 3 or not all(type(v) is int for v in minimum):
            raise RuntimeError('无效的最低插件版本')
        if tuple(minimum) > ADDON_VERSION:raise RuntimeError('此包需要更新版本的材质桥')
    if not isinstance(manifest.get('package_id'), str) or not manifest['package_id']:
        raise RuntimeError('材质包缺少有效 package_id')
    if not isinstance(manifest.get('entries'), list) or not manifest['entries']:
        raise RuntimeError('材质包条目必须是非空列表')
    manifest.setdefault('warnings', [])
    color_version = manifest.get('image_color_metadata_version')
    if color_version is not None and (type(color_version) is not int or color_version != 1):
        raise RuntimeError('不支持的图像色彩空间标记版本，请升级材质桥')
    if color_version is not None:
        colors = manifest.get('image_colorspaces')
        if not isinstance(colors, dict):raise RuntimeError('图像色彩空间清单无效')
        for name, data in colors.items():
            if (not isinstance(name, str) or not name or not isinstance(data, dict)
                    or type(data.get('version')) is not int or data['version'] != 1
                    or data.get('kind') not in {'DATA', 'SRGB', 'NAMED'}
                    or not isinstance(data.get('name'), str) or not data['name']):
                raise RuntimeError('图像色彩空间清单条目无效')
    profiles=manifest.get('fur_profiles', [])
    if not isinstance(profiles,list):raise RuntimeError('毛绒配方列表无效')
    if profiles:
        from . import fur
        for profile in profiles:fur.validate(profile)
    seen = set()
    final = manifest.get('package_kind') == 'final'
    if manifest.get('mesh_payload_version') not in (None, 1):
        raise RuntimeError('不支持的最终包参考网格版本，请升级插件')
    if manifest.get('mesh_payload_version') and not final:
        raise RuntimeError('参考网格只允许出现在最终包中')
    if 'source_uv_payload_version' in manifest or 'source_uv_references' in manifest:
        from . import uv_transfer
        uv_transfer.validate_manifest(manifest)
    for entry in manifest.get('entries') or []:  # v1 包只导出实际使用槽，全部按 in use 处理。
        if not isinstance(entry, dict):
            raise RuntimeError('材质包条目必须是 JSON 对象')
        if entry.get('runtime_bone_role') not in (None,'HEAD'):
            raise RuntimeError('不支持的运行骨骼语义，请升级插件')
        if entry.get('default_role') not in (None, 'CLOTHING', 'BODY', 'FACE', 'OTHER', 'UNKNOWN'):
            raise RuntimeError('材质包默认部位标记无效')
        for key in ('id', 'carrier'):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise RuntimeError('材质包条目缺少有效字段: ' + key)
        if entry['id'] in seen or entry['id'] == 'SKIP':
            raise RuntimeError('材质包条目编号重复或保留: ' + entry['id'])
        seen.add(entry['id'])
        if type(entry.get('slot')) is not int or entry['slot'] < 0:
            raise RuntimeError('材质包条目槽号无效: ' + entry['id'])
        entry.setdefault('slot_used', True)
        reference = entry.get('reference_mesh')
        if manifest.get('mesh_payload_version'):
            if (not isinstance(reference, dict) or reference.get('version') != 1
                    or not isinstance(reference.get('signature'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', reference['signature'])):
                raise RuntimeError('最终包条目缺少有效参考网格记录: ' + entry['id'])
        elif reference is not None:
            raise RuntimeError('材质包参考网格标记不完整: ' + entry['id'])
        if final:
            missing = [key for key in ('id', 'carrier', 'object', 'material', 'slot') if key not in entry]
            if missing:
                raise RuntimeError('最终包条目 ' + str(entry.get('id', '?')) + ' 缺少必需键: ' + ', '.join(missing))
            # A04：object 名既非移植结果部件命名（无 '__'）、条目又无 mmd_material 键 →
            # 包大概率由误选对象导出；归一化反解会把对象名当材质名，必须给可读报错。
            if '__' not in str(entry['object']) and 'mmd_material' not in entry:
                raise RuntimeError('最终包条目 ' + str(entry['id']) + ' 的对象名 ' + str(entry['object'])
                                   + ' 非移植结果部件（包可能由误选对象导出），请向包作者确认')
            entry['source_object'] = entry['object']
            # A03 限定：mmd_material（0.5.0 新包，属性直采）精确；rsplit 仅旧包兜底——
            # 材质名自身含 '__' 的旧包会错位 → 匹配失败走人工，文档化边界。
            entry['source_material'] = entry.get('mmd_material') or str(entry['object']).rsplit('__', 1)[-1]
            entry['cmb_final'] = True  # A10：应用侧据此跳过主光修正（修正已随包固化）
            entry.setdefault('textures', [])
            entry.setdefault('uv_histogram', [])
            entry.setdefault('requirements', {'attributes': ['ruri_tangent', 'ruri_tangent_sign'],
                                              'written_attributes': [], 'source_uv_layers': []})
        for key in ('source_object', 'source_material'):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise RuntimeError('材质包条目 ' + entry['id'] + ' 缺少有效字段: ' + key)
        entry.setdefault('textures', [])
        entry.setdefault('uv_histogram', [])
        req = entry.get('requirements')
        if not isinstance(req, dict) or not isinstance(req.get('attributes'), list):
            raise RuntimeError('材质包属性需求无效: ' + entry['id'])
        for values in (entry['textures'], req['attributes']):
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise RuntimeError('材质包字符串列表无效: ' + entry['id'])
        uv = entry['uv_histogram']
        if not isinstance(uv, list) or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in uv):
            raise RuntimeError('材质包 UV 摘要无效: ' + entry['id'])
    if not manifest.get('entries'):
        raise RuntimeError('材质包没有可移植条目')
    return manifest


def clean_words(value):
    """Casefold and strip separators so dictionary terms compare as plain substrings."""
    return re.sub(r'[\s._\-]+', '', str(value)).casefold()


def bridge_word(name):
    """Longest dictionary term contained in the region name, mapped to its search word."""
    clean = clean_words(name)
    hits = [term for term in NAME_BRIDGE if term in clean]
    return NAME_BRIDGE[max(hits, key=len)] if hits else ''


def normalized(value):
    value = str(value).replace('\\', '/').rsplit('/', 1)[-1].casefold()
    value = re.sub(r'\.\d{3}$', '', value)
    value = re.sub(r'\.(png|tga|dds|jpg|jpeg|bmp|tif|tiff|exr|webp)$', '', value)
    return re.sub(r'\.\d{3}$', '', value)


def walk_trees(tree, seen=None):
    seen = set() if seen is None else seen
    if not tree or tree.as_pointer() in seen:
        return
    seen.add(tree.as_pointer())
    yield tree
    for node in tree.nodes:
        yield from walk_trees(getattr(node, 'node_tree', None), seen)


def walk_trees_for_objects(objects):
    seen = set()
    for obj in objects:
        for slot in obj.material_slots:
            if slot.material:yield from walk_trees(slot.material.node_tree, seen)
        for mod in obj.modifiers:
            if mod.type == 'NODES' and mod.node_group:yield from walk_trees(mod.node_group, seen)


def image_keys(material):
    found = set()
    for tree in walk_trees(material.node_tree if material else None):
        for node in tree.nodes:
            img = getattr(node, 'image', None)
            if img:
                if img.get('cmb_data_role') in {'PARAMS','LIGHTS'} or img.name.startswith(('CMB Uber Params','CMB Light Table')):
                    continue
                for value in (img.name, img.filepath):
                    key = normalized(value)
                    if key and not re.match(r'(toon\d+|rurineutral|rurilighttable|ruri endfield)', key):
                        found.add(key)
    return sorted(found)


def uv_signature(obj, index):
    """A coarse histogram, never vertex positions, UV coordinates or topology."""
    from . import uv_transfer
    uv = uv_transfer.primary_uv(obj.data)
    if not uv:
        return []
    bins = [0] * 256
    for face in obj.data.polygons:
        if face.material_index == index:
            for loop in face.loop_indices:
                u, v = uv.data[loop].uv
                x, y = min(15, max(0, int(u * 16))), min(15, max(0, int(v * 16)))
                bins[y * 16 + x] += 1
    total = sum(bins)
    return [round(v / total, 5) for v in bins] if total else []


def signature_similarity(a, b):
    return sum(min(x, y) for x, y in zip(a, b)) if a and b else 0.0


def tangent_sign_mode(values):
    """Legacy manifest statistic only; a UV-island majority is NOT a basis convention."""
    samples = list(values)
    if not samples:
        return None
    positive = sum(1 for v in samples if v > 0)
    return 1 if positive * 2 > len(samples) else -1


def tangent_flip_count(expected_sign, rebuilt_values):
    """Compatibility shim: aggregate source signs cannot justify any target flip."""
    return 0


TANGENT_BASIS_VERSION = 1
TANGENT_ATTRIBUTES = {'ruri_tangent', 'ruri_tangent_sign'}


def tangent_uv_layer(mesh, preferred=''):
    """Match the recorded UV by name, otherwise use render UV, not the UV editor selection."""
    if preferred and mesh.uv_layers.get(preferred):
        return preferred
    return next((u.name for u in mesh.uv_layers if u.active_render),
                mesh.uv_layers.active.name if mesh.uv_layers.active else '')


def valid_tangent_basis(basis):
    return (isinstance(basis, dict) and basis.get('version') == TANGENT_BASIS_VERSION
            and basis.get('status') == 'calibrated'
            and basis.get('tangent_multiplier') in (-1, 1)
            and basis.get('sign_multiplier') in (-1, 1)
            and isinstance(basis.get('uv_layer'), str) and bool(basis['uv_layer']))


def calibrate_tangent_basis(obj):
    """Record two convention scalars by comparing corresponding SOURCE mesh corners.

    No source geometry/UV samples leave this function. Mirrored islands contribute
    paired ratios, never a vote on the raw +/- signs. Work on a mesh copy so export
    cannot change source tangent caches, normals, active UVs or custom attributes.
    """
    mesh = obj.data
    uv_name = tangent_uv_layer(mesh)
    result = {'version': TANGENT_BASIS_VERSION, 'status': 'unavailable', 'uv_layer': uv_name}
    attrs = [mesh.attributes.get(name) for name in ('ruri_tangent', 'ruri_tangent_sign')]
    if not uv_name or not mesh.loops:
        return dict(result, reason='源网格没有可用的渲染 UV/面角')
    if (any(a is None or a.domain != 'CORNER' for a in attrs)
            or attrs[0].data_type != 'FLOAT_VECTOR' or attrs[1].data_type != 'FLOAT'):
        return dict(result, reason='源网格缺少 CORNER 域的切线/符号属性对')
    source_t = array('f', [0.0]) * (len(mesh.loops) * 3)
    source_s = array('f', [0.0]) * len(mesh.loops)
    attrs[0].data.foreach_get('vector', source_t)
    attrs[1].data.foreach_get('value', source_s)
    copied = mesh.copy()
    try:
        copied.calc_tangents(uvmap=uv_name)
        dots, ratios = [], []
        for i, loop in enumerate(copied.loops):
            t = source_t[i * 3:i * 3 + 3]
            length = math.sqrt(sum(x * x for x in t))
            sign = source_s[i]
            if not math.isfinite(length) or length < 1e-8 or not math.isfinite(sign) or abs(sign) < .5:
                continue
            if loop.tangent.length_squared < .5 or abs(loop.bitangent_sign) < .5:
                continue
            dots.append(sum(x * y for x, y in zip(t, loop.tangent)) / length)
            ratios.append(1 if sign * loop.bitangent_sign > 0 else -1)
        if not dots:
            return dict(result, reason='源切线或 UV 退化，无法校准')
        tm = 1 if sum(dots) >= 0 else -1
        sm = 1 if sum(ratios) >= 0 else -1
        alignment = sum(d * tm >= .95 for d in dots) / len(dots)
        agreement = sum(s == sm for s in ratios) / len(ratios)
        coverage = len(dots) / len(mesh.loops)
        result.update(samples=len(dots), tangent_alignment=round(alignment, 6),
                      sign_agreement=round(agreement, 6), coverage=round(coverage, 6))
        if alignment < .95 or agreement < .99 or coverage < .95:
            return dict(result, reason='源切线不是统一的 MikkTSpace 约定变换；不能按全局符号近似')
        result.update(status='calibrated', tangent_multiplier=tm, sign_multiplier=sm)
        return result
    except RuntimeError as exc:
        return dict(result, reason='源切线校准失败: ' + str(exc))
    finally:
        bpy.data.meshes.remove(copied)


def rot_delta(source_rot, target_rot):
    """Pure: per-component euler difference wrapped to (-pi, pi]."""
    return [((s - t + math.pi) % (2 * math.pi)) - math.pi for s, t in zip(source_rot, target_rot)]


def face_light_plan(delta, threshold=FACE_LIGHT_DELTA_EPSILON):
    """Pure: which correction node kind fits a source-vs-target rotation delta.

    ('multiply180', delta) for a delta that is a 180-degree rotation about Z (the
    RuriRipperImporter face convention) -> VectorMath MULTIPLY (-1,-1,1);
    ('rotate_z', dz) for any other nonzero delta -> VectorRotate EULER (0,0,dz);
    ('none', None) for a near-zero delta (nothing to correct)."""
    if all(abs(d) <= threshold for d in delta):
        return 'none', None
    if (abs(delta[0]) <= threshold and abs(delta[1]) <= threshold
            and abs(abs(delta[2]) - math.pi) <= threshold):
        return 'multiply180', delta
    return 'rotate_z', delta[2]


def face_light_decision(source_object, source_rot, target_rot, enabled=True,
                        threshold=FACE_LIGHT_DELTA_EPSILON):
    """Pure: gate + plan for one applied entry (0.2.3 脸部主光方向修正).

    Returns {'action': 'insert'|'skip', 'plan': face_light_plan(...), 'reason': str}.
    Only source objects whose casefold name contains 'face' are eligible; packages
    without jmb_source_rot_euler (v0.3 或更旧) are skipped with a stated reason; a
    near-zero rotation delta or a disabled switch inserts nothing (silently for the
    switch, so旧包/开关关 never spam the report)."""
    if not enabled:
        return {'action': 'skip', 'plan': None, 'reason': '脸部主光方向修正开关已关闭'}
    if 'face' not in str(source_object).casefold():
        return {'action': 'skip', 'plan': None, 'reason': '非脸源对象'}
    if source_rot is None:
        return {'action': 'skip', 'plan': None, 'reason': '材质包无 jmb_source_rot_euler（v0.3 或更旧）'}
    kind, payload = face_light_plan(rot_delta(source_rot, target_rot), threshold)
    if kind == 'none':
        return {'action': 'skip', 'plan': None, 'reason': '源/目标旋转差近零'}
    return {'action': 'insert', 'plan': (kind, payload), 'reason': '旋转差 ' + '/'.join(f'{d:.2f}' for d in rot_delta(source_rot, target_rot))}


def mainlight_first_hops(vt_key, nodes, links, max_hops=3, target=FACE_LIGHT_INPUT):
    """Pure: which first-hop consumers of one node's output socket lead, within
    max_hops total links, to a Group node input named `target`.

    nodes: {key: {'group': bool}} — group nodes own a node_tree; links:
    [(from_key, from_socket, to_key, to_socket)]. Used to locate the face main-light
    chain (VectorTransform WORLD→OBJECT → SeparateXYZ → CombineXYZ → Face-s1 组的
    C0_MainLight_direction) programmatically instead of hardcoding node names."""
    hops, seen = [], {(vt_key, 'Vector')}
    for link in links:
        if link[0] != vt_key or link[1] != 'Vector':
            continue
        landing = (link[2], link[3])
        if landing in seen:
            continue
        if _reaches_group_input(link[2], link[3], max_hops - 1, nodes, links, target,
                                seen | {landing}):
            hops.append(landing)
            seen.add(landing)
    return hops


def _reaches_group_input(key, socket, hops_left, nodes, links, target, seen):
    node = nodes.get(key)
    if node is None:
        return False
    if node.get('group') and socket == target:
        return True
    if hops_left <= 0:
        return False
    for link in links:
        if link[0] != key:
            continue
        landing = (link[2], link[3])
        if landing in seen:
            continue
        if _reaches_group_input(link[2], link[3], hops_left - 1, nodes, links, target,
                                seen | {landing}):
            return True
    return False


def mesh_objects(objects):
    return [o for o in objects if o.type == 'MESH' and len(o.data.polygons)
            and not o.rigid_body and getattr(o, 'mmd_type', 'NONE') not in {'RIGID_BODY', 'JOINT'}]


def requirements(obj, material):
    reads, writes = set(), set()
    trees = list(walk_trees(material.node_tree if material else None))
    for mod in obj.modifiers:
        if mod.type == 'NODES':
            trees.extend(walk_trees(mod.node_group))
    for tree in trees:
        for node in tree.nodes:
            if node.bl_idname == 'ShaderNodeUVMap' and node.uv_map:
                reads.add(node.uv_map)
            elif node.bl_idname == 'ShaderNodeAttribute' and node.attribute_type == 'GEOMETRY':
                reads.add(node.attribute_name)
            elif node.bl_idname in {'GeometryNodeInputNamedAttribute', 'GeometryNodeStoreNamedAttribute'}:
                sock = node.inputs.get('Name')
                if sock and not sock.is_linked and sock.default_value:
                    (writes if 'Store' in node.bl_idname else reads).add(sock.default_value)
    # Conservatively retain reads even if also written; ordering/branches can matter.
    result = {'attributes': sorted(reads), 'written_attributes': sorted(writes),
              'source_uv_layers': [u.name for u in obj.data.uv_layers]}
    from . import fur
    regions=fur.saved_regions(obj)
    if regions:result['fur_regions']=regions
    attr = obj.data.attributes.get('ruri_tangent_sign') if getattr(obj.data, 'attributes', None) else None
    if attr is not None:
        try:
            sign = tangent_sign_mode(v.value for v in attr.data)
        except (AttributeError, TypeError, RuntimeError):
            sign = None
        if sign is not None:
            result['source_tangent_sign'] = sign
    if reads.intersection(TANGENT_ATTRIBUTES):
        result['tangent_basis'] = calibrate_tangent_basis(obj)
    return result


def id_blocks():
    return set(bpy.data.user_map())


def dependencies(roots):
    reverse = {}
    for dependency, users in bpy.data.user_map().items():
        for user in users:
            reverse.setdefault(user, set()).add(dependency)
    result, stack = set(), list(roots)
    while stack:
        item = stack.pop()
        if item not in result:
            result.add(item)
            stack.extend(reverse.get(item, ()))
    return result


def clear_custom(value):
    try:
        for key in list(value.keys()):
            del value[key]
    except (TypeError, AttributeError):
        pass


def remap_rna(value, mapper, seen=None, depth=0):
    """Remap native ID pointers including GN sockets and Blender 5.2 interfaces."""
    seen = set() if seen is None else seen
    if depth > 18 or not hasattr(value, 'bl_rna'):
        return
    ptr = value.as_pointer()
    if ptr in seen:
        return
    seen.add(ptr)
    for prop in value.bl_rna.properties:
        name = prop.identifier
        if name in {'rna_type', 'id_data', 'original', 'library', 'override_library', 'asset_data'}:
            continue
        try:
            child = getattr(value, name)
        except (AttributeError, RuntimeError):
            continue
        if prop.type == 'POINTER':
            if isinstance(child, bpy.types.ID):
                if getattr(child, 'is_embedded_data', False):
                    remap_rna(child, mapper, seen, depth + 1)
                elif not prop.is_readonly:
                    mapped = mapper(child)
                    if mapped != child:
                        setattr(value, name, mapped)
            elif child is not None:
                remap_rna(child, mapper, seen, depth + 1)
        elif prop.type == 'COLLECTION' and name not in {'users_collection', 'children', 'children_recursive'}:
            for child in child:
                remap_rna(child, mapper, seen, depth + 1)
    try:
        for key, child in list(value.items()):
            if isinstance(child, bpy.types.ID):
                value[key] = mapper(child)
            elif hasattr(child, 'items'):
                remap_custom(child, mapper)
    except TypeError:
        pass


def remap_custom(group, mapper):
    for key, child in list(group.items()):
        if isinstance(child, bpy.types.ID):
            group[key] = mapper(child)
        elif hasattr(child, 'items'):
            remap_custom(child, mapper)


class LibraryBuilder:
    def __init__(self):
        self.mapping = {}
        self.warnings = []
        self.created = set()

    def clone(self, source):
        if source in self.created:
            return source
        if source in self.mapping:
            return self.mapping[source]
        if isinstance(source, bpy.types.Object):
            dest = bpy.data.objects.new('CMB_REF_' + source.name, None)
            self.mapping[source] = dest
            self.created.add(dest)
            dest[TAG] = 'reference'
            dest['jmb_source_name'] = source.name
            dest['jmb_source_type'] = source.type
            self.warnings.append('需要在目标工程重绑定对象: ' + source.name)
            return dest
        if isinstance(source, bpy.types.Collection):
            dest = bpy.data.collections.new('CMB_REF_' + source.name)
            self.mapping[source] = dest
            self.created.add(dest)
            dest[TAG] = 'reference'
            dest['jmb_source_name'] = source.name
            self.warnings.append('需要在目标工程重绑定集合: ' + source.name)
            return dest
        if not isinstance(source, (bpy.types.Material, bpy.types.NodeTree, bpy.types.Image, bpy.types.Texture)):
            raise RuntimeError('不允许导出依赖类型: ' + source.bl_rna.identifier + ' / ' + source.name)
        dest = source.copy()
        self.mapping[source] = dest
        self.created.add(dest)
        clear_custom(dest)
        if hasattr(dest, 'animation_data_clear'):
            if getattr(source, 'animation_data', None):
                self.warnings.append('动画/驱动已冻结为当前值: ' + source.name)
            dest.animation_data_clear()
        tree = dest if isinstance(dest, bpy.types.NodeTree) else getattr(dest, 'node_tree', None)
        if tree:
            if tree.animation_data:
                self.warnings.append('节点动画/驱动已冻结为当前值: ' + source.name)
            tree.animation_data_clear()
            clear_custom(tree)
            for node in tree.nodes:
                if node.bl_idname in {'ShaderNodeScript', 'GeometryNodeBake'}:
                    raise RuntimeError('不能安全打包脚本/烘焙节点: ' + tree.name + '/' + node.name)
                clear_custom(node)
        if isinstance(dest, bpy.types.Image):
            from . import color_management
            color_management.record(dest, color_management.describe(source))
            if dest.source not in {'FILE', 'GENERATED'}:
                raise RuntimeError('暂不支持图像源类型: ' + dest.source + '/' + source.name)
            if source.packed_file and not source.is_dirty:
                packed = source.packed_file.data
                dest.pack(data=packed, data_len=len(packed))
            elif source.source == 'GENERATED' or source.is_dirty:
                pixels = array('f', [0.0]) * len(source.pixels)
                source.pixels.foreach_get(pixels)
                dest.pixels.foreach_set(pixels)
                dest.update()
                dest.pack()
            if not dest.packed_file:
                if source.filepath:
                    dest.filepath = bpy.path.abspath(source.filepath, library=source.library)
                try:
                    dest.pack()
                except RuntimeError as exc:
                    raise RuntimeError('纹理无法打包: ' + source.name + ': ' + str(exc)) from exc
            if not dest.packed_file:
                raise RuntimeError('纹理未打包: ' + source.name)
            dest.filepath_raw = '//textures/' + Path(source.filepath.replace('\\', '/')).name if source.filepath else '//textures/' + source.name + '.png'
        else:
            remap_rna(dest, self.clone)
        return dest


def mmd_name_hits(name):
    """Pure: the first matching MMD-asset pattern label for a data-block/image name, or None."""
    for pattern, label in MMD_NAME_PATTERNS:
        if pattern.search(str(name)):
            return label
    return None


def audit(roots, mmd_scan=False, reference_meshes=()):
    """Hard leak gate over the transitive closure of roots.

    mmd_scan (0.4.0，仅最终包路径启用): additionally refuses ARMATURE modifiers on
    closure objects, any Armature data-block, and any MMD-asset naming hit
    (MMD_NAME_PATTERNS) so MMD-side assets can never ride along in a final package."""
    closure = dependencies(roots)
    reference_meshes = set(reference_meshes)
    allowed = (bpy.types.Object, bpy.types.Mesh, bpy.types.Material, bpy.types.NodeTree,
               bpy.types.Image, bpy.types.Texture, bpy.types.Text, bpy.types.Collection)
    issues = []
    for item in closure:
        if not isinstance(item, allowed):
            issues.append(item.bl_rna.identifier + ': ' + item.name)
        if item.library:
            issues.append('链接库: ' + item.name)
        if isinstance(item, bpy.types.Mesh) and (item.shape_keys or
                (item not in reference_meshes and (len(item.vertices) or len(item.edges) or len(item.polygons)))):
            issues.append('包含实体网格: ' + item.name)
        if isinstance(item, bpy.types.Object):
            if item.type not in {'MESH', 'EMPTY'} or item.parent or item.constraints:
                issues.append('包含非空对象依赖: ' + item.name)
            if mmd_scan and any(mod.type == 'ARMATURE' for mod in item.modifiers):
                issues.append('对象带骨架修改器: ' + item.name)
        if isinstance(item, bpy.types.Armature):
            issues.append('骨架数据块: ' + item.name)
        if isinstance(item, bpy.types.Collection) and (item.objects or item.children):
            issues.append('引用集合不为空: ' + item.name)
        if isinstance(item, bpy.types.Image) and not item.packed_file:
            issues.append('未打包图像: ' + item.name)
        if mmd_scan:
            hit = mmd_name_hits(item.name)
            if not hit and isinstance(item, bpy.types.Image):
                hit = mmd_name_hits(Path(item.filepath.replace('\\', '/')).name)
            if hit:
                issues.append(f'MMD 资产命名({hit}): ' + item.name)
        if hasattr(item, 'animation_data') and item.animation_data:
            issues.append('动画依赖: ' + item.name)
        if isinstance(item, bpy.types.Text) and item.name != MANIFEST:
            issues.append('额外文本: ' + item.name)
    if issues:
        raise RuntimeError('包审计失败: ' + '; '.join(issues[:12]))
    permitted = reference_meshes & closure
    return {'datablocks': len(closure), 'mesh_vertices': sum(len(m.vertices) for m in permitted),
            'mesh_edges': sum(len(m.edges) for m in permitted),
            'mesh_faces': sum(len(m.polygons) for m in permitted),
            'reference_meshes': len(permitted),
            'armatures': 0, 'shape_keys': 0, 'actions': 0,
            'images': sum(isinstance(x, bpy.types.Image) for x in closure)}


def export_package(filepath, objects):
    from . import animation, runtime, lighting, uv_transfer
    runtime.synchronize(bpy.context.scene, for_render=True, force=True, reason='EXPORT')
    lighting.flush(bpy.context.scene)
    animation.flush(bpy.context.scene)
    filepath = os.path.abspath(filepath)
    if bpy.data.filepath and os.path.normcase(filepath) == os.path.normcase(bpy.data.filepath):
        raise RuntimeError('材质包不能覆盖当前源工程')
    if os.path.exists(filepath):
        raise RuntimeError('输出文件已存在，请使用新文件名，避免覆盖工程或旧包')
    objects = mesh_objects(objects)
    if not objects:
        raise RuntimeError('请先选择角色网格对象')
    if bpy.data.texts.get(MANIFEST):
        raise RuntimeError('当前工程已有 CMB_MANIFEST.json，请从源工程导出')
    before = id_blocks()
    builder = LibraryBuilder()
    manifest = {'format': FORMAT, 'version': VERSION, 'package_id': uuid.uuid4().hex,
                'blender_version': list(bpy.app.version), 'entries': [], 'warnings': builder.warnings}
    manifest.update(runtime_binding_version=RUNTIME_BINDING_VERSION, minimum_addon_version=list(ADDON_VERSION))
    manifest['image_color_metadata_version'] = 1
    manifest['image_colorspaces'] = {}
    manifest.update(source_uv_payload_version=uv_transfer.VERSION, source_uv_references={})
    uv_references = set()
    roots = set()
    try:
        for oi, obj in enumerate(objects):
            carrier = bpy.data.objects.new(f'CMB_CARRIER_{oi:03d}', bpy.data.meshes.new(f'CMB_EMPTY_{oi:03d}'))
            # Copy modifiers natively; retain all slot indices for Material Selection nodes.
            builder.created.update({carrier, carrier.data})
            carrier[TAG] = 'carrier'
            carrier['jmb_source_name'] = obj.name
            # 0.2.3: 记录源对象旋转，供应用侧脸部主光方向修正计算源/目标旋转差。
            source_rot = [round(a, 6) for a in obj.matrix_world.to_euler()]
            carrier['jmb_source_rot_euler'] = source_rot
            for slot in obj.material_slots:
                carrier.data.materials.append(builder.clone(slot.material) if slot.material else None)
            # Native operator handles GN interface changes across Blender versions.
            bpy.context.scene.collection.objects.link(carrier)
            try:
                for mod in obj.modifiers:
                    if mod.type == 'ARMATURE':
                        continue
                    if mod.type not in SAFE_MODS:
                        raise RuntimeError(f'{obj.name}: 暂不支持修改器 {mod.type}；请单独处理或移除后导出')
                    with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj, carrier], selected_editable_objects=[obj, carrier]):
                        result = bpy.ops.object.modifier_copy_to_selected(modifier=mod.name)
                    if 'FINISHED' not in result:
                        raise RuntimeError('无法复制修改器: ' + mod.name)
                for mod in carrier.modifiers:
                    if mod.type == 'NODES':
                        if any(getattr(b, 'packed', False) for b in mod.bakes):
                            raise RuntimeError('不允许导出几何节点烘焙缓存')
                        mod.bake_directory = ''
                        # Legacy GN sockets are ID properties. Drop unrelated custom payloads.
                        identifiers = {s.identifier for s in mod.node_group.interface.items_tree if s.item_type == 'SOCKET'} if mod.node_group else set()
                        allowed_keys = identifiers | {s + suffix for s in identifiers for suffix in ('_use_attribute', '_attribute_name')}
                        try:
                            for key in list(mod.keys()):
                                if key not in allowed_keys:
                                    del mod[key]
                        except TypeError:
                            pass
                    else:
                        clear_custom(mod)
                    remap_rna(mod, builder.clone)
            finally:
                bpy.context.scene.collection.objects.unlink(carrier)
            uv_info = uv_transfer.make_reference(obj, carrier)
            manifest['source_uv_references'][carrier.name] = uv_info
            uv_references.add(uv_transfer.validate_reference(carrier, uv_info))
            roots.add(carrier)
            from . import fur
            profile=fur.extract(obj,carrier.name)
            if profile is not None:
                manifest.setdefault('fur_profiles',[]).append(profile)
            elif any(slot.material and material_uber_part(slot.material)=='Fur' for slot in obj.material_slots) and not fur.saved_regions(obj):
                manifest['warnings'].append(obj.name+': 未识别到可安全重建的毛绒层拓扑；保留源节点，目标需自带完整壳层')
            for si, slot in enumerate(obj.material_slots):
                if slot.material is None:
                    continue
                used = any(p.material_index == si for p in obj.data.polygons)
                manifest['entries'].append({'id': f'{oi:03d}:{si:03d}', 'carrier': carrier.name,
                    'source_object': obj.name, 'source_material': slot.material.name, 'slot': si,
                    'slot_used': used,
                    'source_rot_euler': source_rot,  # 0.2.3 冗余记录，便于离线审计
                    # 0.4.0：Ruri Uber 参数列/部位（克隆器 clear_custom 会抹掉材质上的这两个
                    # 自定义属性，导出必须先采集进 manifest，应用侧再写回克隆材质）。
                    'param_col': material_param_col(slot.material),
                    'uber_part': material_uber_part(slot.material),
                    'default_role': material_default_role(slot.material, obj.name),
                    'runtime_bone_role': 'HEAD' if any(n.type == 'GROUP' and n.inputs.get('_RuriRigBasis0') is not None
                                                       for t in walk_trees(slot.material.node_tree) for n in t.nodes) else None,
                    'textures': image_keys(slot.material),
                    'uv_histogram': uv_signature(obj, si) if used else [],
                    'requirements': requirements(obj, slot.material),
                    'modifiers': [{'name': m.name, 'type': m.type} for m in carrier.modifiers]})
        # Refuse an empty or malformed library before publishing an unreadable file.
        validate_manifest(manifest)
        # Apply to private library clones so new source packages already carry
        # the Face fix. The author's original materials remain untouched.
        from . import face_outline
        face_materials = [dest for source, dest in builder.mapping.items()
                          if isinstance(source, bpy.types.Material) and material_uber_part(source) == 'Face']
        manifest['face_outline'] = face_outline.apply_materials(face_materials)
        builder.warnings.extend(manifest['face_outline']['warnings'])
        from . import hair_outline
        hair_materials = [dest for source, dest in builder.mapping.items()
                          if isinstance(source, bpy.types.Material) and material_uber_part(source) == 'Hair']
        manifest['hair_outline'] = hair_outline.apply_materials(hair_materials)
        builder.warnings.extend(manifest['hair_outline']['warnings'])
        text = bpy.data.texts.new(MANIFEST)
        roots.add(text)
        from . import color_management
        manifest['image_colorspaces'] = {image.name: color_management.describe(image)
                                        for image in dependencies(roots) if isinstance(image, bpy.types.Image)}
        manifest['audit'] = audit(roots, reference_meshes=uv_references)
        text.write(json.dumps(manifest, ensure_ascii=False, indent=2))
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        # Write only audited dependencies into a same-directory staging file.
        # An interrupted write must not leave a corrupt package at the final
        # path, and a file created after our precheck must never be overwritten.
        import shutil
        staged = str(Path(filepath).parent / ('.cmb-' + uuid.uuid4().hex + '.blend'))
        if os.path.exists(staged):raise RuntimeError('临时导出文件名冲突，请重试')
        published = False
        try:
            bpy.data.libraries.write(staged, roots, path_remap='RELATIVE', fake_user=True, compress=True)
            with open(staged, 'rb') as src, open(filepath, 'xb') as dst:
                published = True
                shutil.copyfileobj(src, dst)
        except Exception:
            if published:os.unlink(filepath)
            raise
        finally:
            if os.path.exists(staged):os.unlink(staged)
        return manifest
    finally:
        additions = id_blocks() - before
        if additions:
            bpy.data.batch_remove(ids=additions)


def _final_mapper(builder):
    """remap_rna mapper for the final-package path: MMD-side Object/Collection
    pointers become CMB_REF_ placeholders (MMD 资产绝不进包); cloned materials /
    node trees / images stay as direct references so调好的参数值随包带走."""
    def mapper(value):
        if isinstance(value, (bpy.types.Object, bpy.types.Collection)):
            return builder.clone(value)
        return value
    return mapper


def export_final_package(filepath, objects, plugin_version='', include_reference_mesh=True):
    """最终包：每对象一个载体（可携带参考网格）+ 原生复制的 NODES 修改器 +
    按槽直引用当前材质；manifest 精简版（package_kind='final'）。

    与 export_package（源工程导出）的区别：
      - 材质/节点树/图像不克隆、直接引用现有数据块——参数图里调好的值随之进包；
      - 修改器上的对象/集合引用仍换成空占位（MMD 侧对象绝不进包）；
      - 防泄露审计加开 mmd_scan（ARMATURE 修改器/骨架数据块/MMD 资产命名扫描）；
      - 条目精简为 对象/材质/槽/param_col/uber_part（不做匹配用证据）。
    已知边界：GN 树接口 socket 的对象默认值不经修改器 remap（不克隆树、也不改写用户
    工程），EMPTY 型引用按设计允许进包（与 CMB_REF_ 占位同类）；实体网格/骨架/MMD
    命名无论走哪条引用路径都会被闭包硬闸整体拒绝。
    导出成功后把摘要写进 CMB_Report.json Text 数据块（留在当前工程、不进包）。"""
    from . import animation, runtime, lighting
    runtime.synchronize(bpy.context.scene, for_render=True, force=True, reason='EXPORT')
    lighting.flush(bpy.context.scene)
    animation.flush(bpy.context.scene)
    filepath = os.path.abspath(filepath)
    if bpy.data.filepath and os.path.normcase(filepath) == os.path.normcase(bpy.data.filepath):
        raise RuntimeError('最终包不能覆盖当前工程')
    if os.path.exists(filepath):
        raise RuntimeError('输出文件已存在，请使用新文件名，避免覆盖工程或旧包')
    preset_path = filepath + PRESET_SUFFIX
    if os.path.exists(preset_path):
        raise RuntimeError('配套映射预设已存在，请使用新文件名，避免覆盖: ' + preset_path)
    objects = mesh_objects(objects)
    if not objects:
        raise RuntimeError('请先选择移植结果（或带克隆材质的网格）对象')
    # 0.5.0（A04）：最终包只收移植结果部件。误选普通网格导出的包，条目 object 名不含
    # '__'，应用端反解 source_material 会把对象名当材质名，全表无匹配且错误不可读。
    mapping_rows, mapping_keys, used_slots = [], set(), {}
    for oi, obj in enumerate(objects):
        role = role_of(obj)
        if role != 'result':
            raise RuntimeError('对象 ' + obj.name + ' 不是移植结果部件（'
                               + ('角色标记 ' + str(role) if role else '无移植结果标记')
                               + '）；最终包只能导出应用材质包后生成的结果对象')
        used = {p.material_index for p in obj.data.polygons}
        if len(used) != 1:
            raise RuntimeError(obj.name + ': 自动生成最终包映射需要一个实际使用的材质槽；'
                               '请先按材质分离，并确认部件不是空网格')
        si = next(iter(used))
        if si >= len(obj.material_slots) or obj.material_slots[si].material is None:
            raise RuntimeError(obj.name + ': 实际使用的材质槽为空，无法生成最终包映射')
        used_slots[obj.name] = si
        material_name = str(obj.get('jmb_part_mat') or obj.name.rsplit('__', 1)[-1])
        original_name = str(obj.get('jmb_original') or obj.name.rsplit('__', 1)[0])
        key = original_name + '::' + material_name
        if key in mapping_keys:
            raise RuntimeError('最终包映射目标重复: ' + key + '；请只选择该区域的一份移植结果')
        mapping_keys.add(key)
        mapping_rows.append({'object': original_name, 'material': material_name,
                             'choice': f'{oi:03d}:{si:03d}'})
    if bpy.data.texts.get(MANIFEST):
        raise RuntimeError('当前工程已有 CMB_MANIFEST.json，请勿在源工程导出最终包')
    before = id_blocks()
    builder = LibraryBuilder()  # 仅使用其 Object/Collection 空占位分支
    manifest = {'format': FORMAT, 'version': VERSION, 'package_kind': 'final',
                'package_id': uuid.uuid4().hex, 'blender_version': list(bpy.app.version),
                'entries': [], 'warnings': builder.warnings}
    manifest.update(runtime_binding_version=RUNTIME_BINDING_VERSION, minimum_addon_version=list(ADDON_VERSION))
    manifest['image_color_metadata_version'] = 1
    manifest['image_colorspaces'] = {}
    from . import mesh_state
    if include_reference_mesh:
        manifest['mesh_payload_version'] = mesh_state.VERSION
        manifest['minimum_addon_version'] = list(ADDON_VERSION)
    reference_meshes = set()
    roots = set()
    try:
        for oi, obj in enumerate(objects):
            carrier = bpy.data.objects.new(f'CMB_CARRIER_{oi:03d}', bpy.data.meshes.new(f'CMB_EMPTY_{oi:03d}'))
            builder.created.update({carrier, carrier.data})
            carrier[TAG] = 'carrier'
            carrier['jmb_source_name'] = obj.name
            reference_info = None
            if include_reference_mesh:
                reference_info = mesh_state.make_reference(obj, carrier)
                reference_meshes.add(carrier.data)
            for slot in obj.material_slots:
                carrier.data.materials.append(slot.material)  # 空槽保留 None，保住槽号
                if slot.material and material_param_col(slot.material) is None \
                        and not material_uber_part(slot.material):
                    manifest['warnings'].append('材质无 Ruri Uber 参数记录（可能非克隆材质）: '
                                                + slot.material.name)
            bpy.context.scene.collection.objects.link(carrier)
            try:
                for mod in obj.modifiers:
                    if mod.type == 'ARMATURE':
                        continue  # MMD 骨架绝不进包
                    if mod.type not in SAFE_MODS:
                        raise RuntimeError(f'{obj.name}: 暂不支持修改器 {mod.type}；请单独处理或移除后导出')
                    with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj, carrier], selected_editable_objects=[obj, carrier]):
                        result = bpy.ops.object.modifier_copy_to_selected(modifier=mod.name)
                    if 'FINISHED' not in result:
                        raise RuntimeError('无法复制修改器: ' + mod.name)
                for mod in carrier.modifiers:
                    if mod.type == 'NODES':
                        if any(getattr(b, 'packed', False) for b in mod.bakes):
                            raise RuntimeError('不允许导出几何节点烘焙缓存')
                        mod.bake_directory = ''
                        identifiers = {s.identifier for s in mod.node_group.interface.items_tree if s.item_type == 'SOCKET'} if mod.node_group else set()
                        allowed_keys = identifiers | {s + suffix for s in identifiers for suffix in ('_use_attribute', '_attribute_name')}
                        try:
                            for key in list(mod.keys()):
                                if key not in allowed_keys:
                                    del mod[key]
                        except TypeError:
                            pass
                    else:
                        clear_custom(mod)
                    remap_rna(mod, _final_mapper(builder))
            finally:
                bpy.context.scene.collection.objects.unlink(carrier)
            roots.add(carrier)
            for si, slot in enumerate(obj.material_slots):
                if slot.material is None:
                    continue
                manifest['entries'].append({'id': f'{oi:03d}:{si:03d}', 'carrier': carrier.name,
                    'object': obj.name, 'material': slot.material.name, 'slot': si,
                    'slot_used': si == used_slots[obj.name],
                    'param_col': material_param_col(slot.material),
                    'uber_part': material_uber_part(slot.material),
                    'default_role': material_default_role(slot.material, obj.name),
                    'runtime_bone_role': 'HEAD' if any(n.type == 'GROUP' and n.inputs.get('_RuriRigBasis0') is not None
                                                       for t in walk_trees(slot.material.node_tree) for n in t.nodes) else None,
                    # 0.5.0 增量三键（A03/A04），manifest version 仍 2：mmd_material 让应用端
                    # 免受材质名含 '__' 的 rsplit 歧义与 63 字符对象名截断之害（apply 写入的
                    # jmb_part_mat 属性优先，rsplit 仅旧结果兜底）；textures/requirements 让
                    # 最终包重新携带匹配证据与应用侧切线/属性需求。
                    'mmd_material': obj.get('jmb_part_mat') or obj.name.rsplit('__', 1)[-1],
                    'textures': image_keys(slot.material),
                    'requirements': requirements(obj, slot.material),
                    'modifiers': [{'name': m.name, 'type': m.type} for m in carrier.modifiers]})
                if reference_info is not None:
                    manifest['entries'][-1]['reference_mesh'] = reference_info
        # Final identities are MMD-side parts, not the original game recipes.
        # Normalize a copy so the on-disk manifest keeps its existing schema.
        normalized = validate_manifest(json.loads(json.dumps(manifest)))
        preset = build_preset(mapping_rows, manifest['package_id'], plugin_version,
                              entries=normalized['entries'])
        preset['package_kind'] = 'final'
        repeated_materials = sorted({r['material'] for r in mapping_rows}
                                    - set(preset['by_material']))
        if repeated_materials:
            manifest['warnings'].append('以下材质名属于多个目标对象，预设需按对象名区分，'
                                        '不会按材质名任选一个: ' + ', '.join(repeated_materials))
        manifest['mapping_preset'] = {'filename': os.path.basename(preset_path),
                                      'rows': len(mapping_rows)}
        verification = [dict(row, choice='') for row in mapping_rows]
        apply_preset(preset, verification, manifest=normalized)
        if [r['choice'] for r in verification] != [r['choice'] for r in mapping_rows]:
            raise RuntimeError('最终包映射自检失败，未写入文件')
        preset_json = json.dumps(preset, ensure_ascii=False, indent=2)
        text = bpy.data.texts.new(MANIFEST)
        roots.add(text)
        from . import color_management
        # Final packages reference author images directly; keep their portable
        # semantics in the manifest without modifying the author's properties.
        manifest['image_colorspaces'] = {image.name: color_management.describe(image)
                                        for image in dependencies(roots) if isinstance(image, bpy.types.Image)}
        manifest['audit'] = audit(roots, mmd_scan=True, reference_meshes=reference_meshes)
        text.write(json.dumps(manifest, ensure_ascii=False, indent=2))
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        # Stage in the same directory, preserving Blender's relative paths.
        # Publish exclusively; failures remove only this attempt's output files.
        import shutil
        staged = str(Path(filepath).parent / ('.cmb-' + uuid.uuid4().hex + '.blend'))
        if os.path.exists(staged):
            raise RuntimeError('临时导出文件名冲突，请重试')
        published = []
        try:
            bpy.data.libraries.write(staged, roots, path_remap='RELATIVE', fake_user=True, compress=True)
            with open(staged, 'rb') as src, open(filepath, 'xb') as dst:
                published.append(filepath)
                shutil.copyfileobj(src, dst)
            with open(preset_path, 'x', encoding='utf-8') as stream:
                published.append(preset_path)
                stream.write(preset_json)
        except Exception:
            for created in reversed(published):
                os.unlink(created)
            raise
        finally:
            if os.path.exists(staged):
                os.unlink(staged)
        materials = {s.material for o in objects for s in o.material_slots if s.material}
        deviations = sum(len(param_deviations(material)) for material in materials)
        write_report({'final_export': {'package': filepath, 'package_id': manifest['package_id'],
                                       'mapping_preset': preset_path, 'mapping_rows': len(mapping_rows),
                                       'carriers': len(objects), 'materials': len(materials),
                                       'param_deviations': deviations,
                                       'audit': manifest['audit'],
                                       'mesh_payload': bool(reference_meshes),
                                       'leak_scan': '骨架/形态键/动画/刚体不入包；参考网格仅在明确启用时携带',
                                       'warnings': manifest['warnings']}})
        return manifest
    finally:
        additions = id_blocks() - before
        # CMB_Report.json 是给用户的导出摘要，留在当前工程（其余会话新增全部清掉）。
        additions = {item for item in additions
                     if not (isinstance(item, bpy.types.Text) and item.name == REPORT)}
        if additions:
            bpy.data.batch_remove(ids=additions)


def read_manifest(filepath):
    """Read only the manifest Text from a package; accepts new (CMB) and legacy (JMB)
    manifest Text names and both format identifiers (0.3.0 改名兼容，见 validate_manifest)."""
    loaded = []
    try:
        with bpy.data.libraries.load(filepath, link=False) as (source, dest):
            name = manifest_text_name(source.texts)
            if not name:
                raise RuntimeError('不是角色材质包：缺少清单（兼容 CMB/JMB 清单）')
            dest.texts = [name]
        loaded = dest.texts
        return validate_manifest(json.loads(loaded[0].as_string()))
    finally:
        for item in loaded:
            if item:
                bpy.data.texts.remove(item)


def rank_rows(rows_data, manifest):
    """Pure, bpy-free matching core: rank every row, then decide the confirm layer.

    rows_data: [{'object', 'slot', 'material', 'textures': iterable, 'uv_histogram': list}]
    Returns rows with object/slot/material/choice/status/reason/candidates/textures.
    Layers: T1 exact name, T2 score>=60 & margin>=12, T3 decisive UV, T4 unique material-name
    bridge without any texture evidence, T5 per-key group elimination (iterated to a fixed point).
    """
    # Count unique source materials so object variants do not dilute texture evidence.
    per_material = {}
    for entry in manifest['entries']:
        per_material.setdefault(entry['source_material'], set()).update(entry['textures'])
    frequency = Counter(k for values in per_material.values() for k in values)
    cleaned = [(clean_words(e['source_material']), clean_words(e['source_object']))
               for e in manifest['entries']]
    keys_of = [set(e.get('textures') or []) for e in manifest['entries']]
    results = []
    for row in rows_data:
        keys = set(row.get('textures') or [])
        uv = row.get('uv_histogram') or []
        word = bridge_word(row.get('material', ''))
        ranked = []
        for entry, (clean_material, clean_object) in zip(manifest['entries'], cleaned):
            common = keys.intersection(entry.get('textures') or [])
            exact = bool(row.get('material')
                         and normalized(row['material']) == normalized(entry['source_material']))
            evidence = sum(1 / frequency[k] for k in common)
            uv_score = signature_similarity(uv, entry.get('uv_histogram') or [])
            # 名称桥接两档：材质名命中优先，仅对象名命中次之；两档不叠加，精确名互斥。
            bridge_kind = ''
            if not exact and word:
                if word in clean_material:
                    bridge_kind = 'material'
                elif word in clean_object:
                    bridge_kind = 'object'
            bridge = word if bridge_kind else ''
            score = ((EXACT_SCORE if exact else 0) + min(80, evidence * 65)
                     + (uv_score * UV_WEIGHT if common else 0)
                     + (BRIDGE_MATERIAL_SCORE if bridge_kind == 'material'
                        else BRIDGE_OBJECT_SCORE if bridge_kind == 'object' else 0))
            ranked.append({'id': entry['id'], 'score': round(score, 2), 'textures': sorted(common),
                           'uv_similarity': round(uv_score, 3), 'exact_name': exact, 'bridge': bridge,
                           'bridge_kind': bridge_kind})
        ranked.sort(key=lambda x: (-x['score'], x['id']))
        top = ranked[0] if ranked else None
        second = ranked[1] if len(ranked) > 1 else None
        score = top['score'] if top else 0.0
        margin = score - second['score'] if second else score
        choice, status, reason = '', '', ''
        if top is None or score <= 0:
            status, reason = '无匹配', '无有效证据'
        elif top['exact_name'] and sum(c['exact_name'] for c in ranked) == 1:
            choice, status = top['id'], '自动·精确名'
            reason = '区域名与源材质名精确一致'
        elif score >= AUTO_SCORE and margin >= AUTO_MARGIN:
            choice, status = top['id'], '自动·评分'
            reason = f'评分 {score:g}≥{AUTO_SCORE} 且领先 {margin:g}≥{AUTO_MARGIN}'
        elif (top['textures'] and top['uv_similarity'] >= 0.5
              and top['uv_similarity'] - (second['uv_similarity'] if second else 0) >= 0.2):
            gap = top['uv_similarity'] - (second['uv_similarity'] if second else 0)
            choice, status = top['id'], '自动·UV'
            reason = f'UV {top["uv_similarity"]:.2f}≥0.5 且领先 {gap:.2f}≥0.2'
        elif (not any(c['textures'] for c in ranked)
              and top['bridge_kind'] == 'material'
              and sum(1 for c in ranked if c['bridge_kind'] == 'material') == 1
              and margin >= AUTO_MARGIN):
            choice, status = top['id'], '自动·名称'
            reason = f'全候选无贴图证据且唯一材质名桥接({top["bridge"]})'
        else:
            status = '待确认·' + ('分数不足' if score < AUTO_SCORE else '领先不足')
            reason = f'评分 {score:g}，领先 {margin:g}，需人工确认'
        results.append({'object': row.get('object', ''), 'slot': row.get('slot', 0),
                        'material': row.get('material', ''), 'choice': choice, 'status': status,
                        'reason': reason, 'candidates': ranked[:5], 'textures': sorted(keys)})
    # Reuse is allowed. ALL known texture evidence must agree on one recipe;
    # conflicting unique textures cannot be resolved by whichever key sorts first.
    sources_by_key = {}
    for entry, keys in zip(manifest['entries'], keys_of):
        for key in keys:
            sources_by_key.setdefault(key, set()).add(entry['id'])
    for row in results:
        if row['choice']:
            continue
        sources = [sources_by_key[k] for k in row['textures'] if k in sources_by_key]
        possible = set.intersection(*sources) if sources else set()
        if len(possible) == 1:
            row['choice'] = possible.pop()
            row['status'] = '自动·唯一贴图'
            row['reason'] = '包内已知贴图证据共同指向唯一源组合'
    return results


def row_menu_items(evidence, entries):
    """Pure: one row's dropdown — 恒定顺序：SKIP 在前，其后全部条目按 id 升序。

    0.4.1 根治 cmb.preview 自动匹配错位：Blender 动态枚举按 int 索引存取，若 items
    顺序随行证据（evidence）变化，赋值帧与读取帧不一致时同一索引会解析成错 identifier。
    现在顺序与证据彻底解耦——候选证据只用来装饰 label（分数/命中提示），损坏证据只
    降级 label，永不改变顺序。"""
    try:
        candidates = json.loads(evidence) if evidence else []
        if not isinstance(candidates, list):
            raise ValueError('候选证据不是列表')
    except ValueError:
        candidates = []  # 损坏证据：label 退化为纯名称，顺序不变
    ids = {e['id'] for e in entries}
    hints_of = {c['id']: c for c in candidates
                if isinstance(c, dict) and c.get('id') in ids}
    items = [('SKIP', '跳过 / 待确认', '')]
    for entry in sorted(entries, key=lambda x: x['id']):
        candidate = hints_of.get(entry['id'])
        if candidate is None:
            items.append((entry['id'], entry['source_material'] + ' | ' + entry['source_object'],
                          entry['id']))
            continue
        hints = []
        if candidate.get('exact_name'):
            hints.append('精确名')
        if candidate.get('bridge'):
            hints.append('名称桥接')
        if candidate.get('textures'):
            hints.append('贴图')
        if candidate.get('uv_similarity', 0) >= 0.5:
            hints.append('UV')
        label = (f"{candidate.get('score', 0):g} · {entry['source_material']} | {entry['source_object']}"
                 + (' [' + '/'.join(hints) + ']' if hints else ''))
        items.append((entry['id'], label, entry['id']))
    return items


def build_preset(rows, package_id='', plugin_version='', entries=None):
    """Pure: serialize row choices into a mapping preset dict (SKIP rows stored as '')."""
    mapping, by_material, conflicts = {}, {}, set()
    for row in rows:
        choice = row.get('choice') or ''
        if choice == 'SKIP':  # UI 层的跳过标记统一落盘为空串
            choice = ''
        mapping[row['object'] + '::' + row['material']] = choice
        if row['material'] in by_material and by_material[row['material']] != choice:
            conflicts.add(row['material'])
        by_material.setdefault(row['material'], choice)
    for name in conflicts:
        by_material.pop(name, None)
    return {'package_id': package_id, 'plugin_version': plugin_version,
            'created': datetime.now().isoformat(timespec='seconds'),
            'map': mapping, 'by_material': by_material,
            'recipes': {e['id']: {k: e.get(k) for k in ('source_object', 'source_material', 'slot')}
                        for e in entries or []}}


def preset_target_name(name, object_name=False):
    """Only Blender numeric suffixes; keep material numbers, case and spacing."""
    pattern = r'\.\d{3,}(?=_mesh(?:\.\d{3,})?$|$)' if object_name else r'\.\d{3,}$'
    return re.sub(pattern, '', name)


def apply_preset(preset, rows, manifest=None, report=None):
    """Pure: apply a preset dict to rows in place; object::material key first, then by_material.

    Returns the number of rows whose choice changed."""
    if not isinstance(preset, dict):
        raise RuntimeError('映射预设必须是 JSON 对象')
    if preset.get('kind') == 'cmb-params-preset':
        raise RuntimeError('这是参数预设，请在“参数调节”面板载入；此处需要 .mapping.json')
    mapping = preset.get('map') or {}
    by_material = preset.get('by_material') or {}
    if not isinstance(mapping, dict) or not isinstance(by_material, dict):
        raise RuntimeError('映射预设的 map/by_material 必须是对象')
    for table in (mapping, by_material):
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in table.items()):
            raise RuntimeError('映射预设的名称和条目值必须是字符串')
    scoped, names = {}, {}
    for key, value in mapping.items():
        if key.count('::') != 1:
            continue
        obj, material = key.split('::', 1)
        pair = (preset_target_name(obj, True), preset_target_name(material))
        scoped.setdefault(pair, set()).add('' if value == 'SKIP' else value)
        names.setdefault(pair[1], set()).add('' if value == 'SKIP' else value)
    for material, value in by_material.items():
        names.setdefault(preset_target_name(material), set()).add('' if value == 'SKIP' else value)
    entries = (manifest or {}).get('entries', [])
    valid_ids = {e['id'] for e in entries}
    recipes = preset.get('recipes') or {}
    pending = []
    changed = 0
    result = {'total': len(rows), 'matched': 0, 'changed': 0, 'unchanged': 0,
              'matches': [], 'unmatched': [], 'ambiguous': []}
    for index, row in enumerate(rows):
        key = row['object'] + '::' + row['material']
        if key in mapping:
            choice, method = mapping[key], 'EXACT_OBJECT_MATERIAL'
        elif row['material'] in by_material:
            choice, method = by_material[row['material']], 'EXACT_MATERIAL'
        else:
            pair = (preset_target_name(row['object'], True), preset_target_name(row['material']))
            candidates = scoped.get(pair)
            method = 'SUFFIX_OBJECT_MATERIAL'
            if candidates is None:
                candidates = names.get(pair[1])
                method = 'SUFFIX_MATERIAL'
            if not candidates:
                result['unmatched'].append({'index': index, 'object': row['object'], 'material': row['material']})
                continue
            if len(candidates) != 1:
                result['ambiguous'].append({'index': index, 'object': row['object'], 'material': row['material'],
                                            'reason': '去除 Blender 数字尾号后存在不同映射，保留当前选择'})
                continue
            choice = next(iter(candidates))
        if not isinstance(choice, str):
            raise RuntimeError('映射预设条目不是字符串: ' + key)
        if choice == 'SKIP':
            choice = ''
        if manifest is not None and choice:
            if preset.get('package_id') != manifest.get('package_id'):
                identity = recipes.get(choice) if isinstance(recipes, dict) else None
                if not isinstance(identity, dict) or any(k not in identity for k in ('source_object', 'source_material', 'slot')):
                    raise RuntimeError('跨包预设缺少源组合身份，请在原包中用新版重新保存预设')
                matches = [e['id'] for e in entries if all(e.get(k) == identity[k]
                           for k in ('source_object', 'source_material', 'slot'))]
                if len(matches) != 1:
                    hint = ('；当前是最终包，请载入该最终包旁的 .blend.mapping.json，'
                            '不要使用源材质包的旧映射预设'
                            if (manifest or {}).get('package_kind') == 'final' else '')
                    raise RuntimeError('跨包预设的源组合不存在或不唯一: ' + str(identity) + hint)
                choice = matches[0]
            if choice not in valid_ids:
                raise RuntimeError('映射预设引用不存在的条目: ' + choice)
        pending.append((row, choice))
        result['matches'].append({'index': index, 'choice': choice, 'method': method})
    for row, choice in pending:
        if choice != row.get('choice'):
            row['choice'] = choice
            changed += 1
    result.update(matched=len(pending), changed=changed, unchanged=len(pending)-changed)
    if report is not None:
        report.clear()
        report.update(result)
    return changed


def rank_entries(obj, slot, manifest):
    """Rank one material slot; thin wrapper kept for API compatibility over rank_rows."""
    material = obj.material_slots[slot].material
    rows = rank_rows([{'object': obj.name, 'slot': slot,
                       'material': material.name if material else '',
                       'textures': image_keys(material), 'uv_histogram': uv_signature(obj, slot)}],
                     manifest)
    return rows[0]['candidates']


def analyze(objects, manifest):
    rows_data = []
    for obj in mesh_objects(objects):
        if role_of(obj) == 'result':
            raise RuntimeError('所选对象已移植，请先恢复，或选择其他原始角色')
        used = sorted({p.material_index for p in obj.data.polygons})
        for slot in used:
            if slot >= len(obj.material_slots) or not obj.material_slots[slot].material:
                continue
            material = obj.material_slots[slot].material
            rows_data.append({'object': obj.name, 'slot': slot, 'material': material.name,
                              'textures': image_keys(material),
                              'uv_histogram': uv_signature(obj, slot)})
    return rank_rows(rows_data, manifest)


def write_report(data):
    text = bpy.data.texts.get(REPORT) or bpy.data.texts.new(REPORT)
    text.clear()
    text.write(json.dumps(data, ensure_ascii=False, indent=2))


def file_digest(filepath):
    digest = hashlib.sha256()
    with open(filepath, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def copy_missing_tangents(obj, requested, warnings, source_tangent_sign=None, tangent_basis=None,
                          check_missing=True):
    """Rebuild BOTH target attributes, including stale/partial pairs from previous runs.

    The old name/sign argument remain callable for older integrations. Raw source
    sign majorities are deliberately ignored. Missing calibration falls back to
    native MikkTSpace with an explicit warning, never a guessed global flip.
    """
    mesh = obj.data
    basis = tangent_basis if valid_tangent_basis(tangent_basis) else None
    requested = set(requested)
    outcome = None
    if requested.intersection(TANGENT_ATTRIBUTES):
        uv_name = tangent_uv_layer(mesh, basis['uv_layer'] if basis else '')
        if not uv_name:
            warnings.append(obj.name + ': 没有目标 UV，无法重建切线')
            return None
        if basis is None:
            reason = tangent_basis.get('reason', '') if isinstance(tangent_basis, dict) else ''
            warnings.append(obj.name + ': 缺少可靠的逐角切线校准，使用目标原生 MikkTSpace；'
                            '不再按源 sign 众数翻转。请用新版重新导出源材质包。' + reason)
        elif uv_name != basis['uv_layer']:
            warnings.append(obj.name + ': 无同名 UV ' + basis['uv_layer']
                            + '，使用目标渲染 UV ' + uv_name)
        # Compute fully before touching attributes; calc_tangents may reject n-gons.
        copied = mesh.copy()
        try:
            copied.calc_tangents(uvmap=uv_name)
            tm = basis['tangent_multiplier'] if basis else 1
            sm = basis['sign_multiplier'] if basis else 1
            tangents = array('f', (v * tm for loop in copied.loops for v in loop.tangent))
            signs = array('f', (loop.bitangent_sign * sm for loop in copied.loops))
            for name, kind, prop, values in (
                    ('ruri_tangent', 'FLOAT_VECTOR', 'vector', tangents),
                    ('ruri_tangent_sign', 'FLOAT', 'value', signs)):
                attr = mesh.attributes.get(name)
                if attr is not None and (attr.domain != 'CORNER' or attr.data_type != kind):
                    mesh.attributes.remove(attr)
                    attr = None
                if attr is None:
                    attr = mesh.attributes.new(name, kind, 'CORNER')
                attr.data.foreach_set(prop, values)
            mesh.update()
            if basis:
                # Persist only convention scalars, so later UV edits can be repaired.
                obj['cmb_tangent_basis'] = json.dumps(basis, ensure_ascii=False)
            elif 'cmb_tangent_basis' in obj:
                del obj['cmb_tangent_basis']
            outcome = {'object': obj.name, 'uv_layer': uv_name, 'loops': len(signs),
                       'tangent_multiplier': tm, 'sign_multiplier': sm,
                       'calibrated': bool(basis), 'positive': sum(s > 0 for s in signs),
                       'negative': sum(s < 0 for s in signs)}
            warnings.append(obj.name + ': 已成对重建目标切线/符号（UV=' + uv_name
                            + '，T×' + str(tm) + '，sign×' + str(sm) + '），保留镜像 UV 手性')
        except RuntimeError as exc:
            warnings.append(obj.name + ': 切线重建失败 ' + str(exc))
        finally:
            bpy.data.meshes.remove(copied)
    missing = set(requested) - set(mesh.attributes.keys())
    if missing and check_missing:
        warnings.append(obj.name + ': 缺少属性 ' + ', '.join(sorted(missing)) + '；未伪造源 UV/顶点色')
    return outcome


def repair_result_tangents(objects, manifest=None):
    """Repair result meshes without replacing materials, modifiers or user edits.

    New results carry their own calibration. For legacy results, a newly exported
    package may be used only when its source-material identity has one unambiguous
    convention. Entry numbers from a different package are never treated as IDs.
    """
    report = {'repaired': [], 'skipped': [], 'warnings': []}
    entries = (manifest or {}).get('entries', [])
    for obj in mesh_objects(objects):
        if role_of(obj) != 'result':
            report['skipped'].append({'object': obj.name, 'reason': '不是移植结果'})
            continue
        try:
            basis = json.loads(obj.get('cmb_tangent_basis', '{}'))
        except (ValueError, TypeError):
            basis = None
        if not valid_tangent_basis(basis):
            candidates = []
            if obj.get('jmb_package') == (manifest or {}).get('package_id'):
                candidates = [e for e in entries if e['id'] == obj.get('jmb_entry')]
            else:
                names = {normalized(m.name) for m in obj.data.materials if m}
                candidates = [e for e in entries if normalized(e.get('source_material', '')) in names
                              and (not obj.get('cmb_source_object')
                                   or e.get('source_object') == obj['cmb_source_object'])]
            calibrated = [e.get('requirements', {}).get('tangent_basis') for e in candidates]
            conventions = {(b['uv_layer'], b['tangent_multiplier'], b['sign_multiplier'])
                           for b in calibrated if valid_tangent_basis(b)}
            basis = next((b for b in calibrated if valid_tangent_basis(b)), None)
            if len(conventions) != 1 or len(calibrated) != sum(valid_tangent_basis(b) for b in calibrated):
                basis = None
        if not valid_tangent_basis(basis):
            report['skipped'].append({'object': obj.name,
                                     'reason': '没有唯一的切线校准；请载入同角色用新版重新导出的源材质包'})
            continue
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        outcome = copy_missing_tangents(obj, TANGENT_ATTRIBUTES, report['warnings'], tangent_basis=basis)
        if outcome:
            report['repaired'].append(outcome)
        else:
            report['skipped'].append({'object': obj.name, 'reason': '目标 UV/切线重建失败，见报告'})
    write_report(report)
    return report


def _new_face_light_node(tree, kind, payload):
    """Build the direction-correction node chosen by face_light_plan.

    multiply180 -> VectorMath MULTIPLY (-1,-1,1)（= 绕 Z 旋转 180°，用户已视觉验收）;
    rotate_z -> VectorRotate EULER (0,0,Δz) for any other nonzero delta shape."""
    if kind == 'rotate_z':
        node = tree.nodes.new('ShaderNodeVectorRotate')
        node.rotation_type = 'EULER_XYZ'
        node.inputs['Rotation'].default_value = (0.0, 0.0, float(payload))
        node.name = FACE_LIGHT_FIX_PREFIX + 'Z'
        node.label = 'CMB 脸主光方向 Z 轴修正'
    else:
        node = tree.nodes.new('ShaderNodeVectorMath')
        node.operation = 'MULTIPLY'
        node.inputs[1].default_value = (-1.0, -1.0, 1.0)
        node.name = FACE_LIGHT_FIX_PREFIX + '180'
        node.label = 'CMB 脸主光方向 180°Z 修正'
    return node


def fix_face_mainlight(material, source_object, source_rot, target_rot, warnings, enabled=True):
    """0.2.3: correct the cloned face material's main-light direction after a transfer.

    Locates the WORLD→OBJECT VectorTransform chain whose Vector output reaches a Group
    input named C0_MainLight_direction within 3 links (traced programmatically, see
    mainlight_first_hops), then re-routes only those first hops through a correction
    node. The secondary-light chain and non-face materials are never touched; an
    existing CMB_/JMB_LightDirFix* node (0.3.0 前后两种前缀) fed by the same VectorTransform
    makes the call a no-op (幂等). Returns 1 when a correction is present afterwards, 0 otherwise;
    actionable skips (旧包无旋转记录、未定位到主光链) append a reason to warnings."""
    decision = face_light_decision(source_object, source_rot, target_rot, enabled)
    if decision['action'] != 'insert':
        if enabled and source_rot is None and 'face' in str(source_object).casefold():
            warnings.append(str(material.name if material else source_object)
                            + ': 材质包无 jmb_source_rot_euler（v0.3 或更旧），跳过脸部主光方向修正')
        return 0
    tree = material.node_tree if material else None
    if not tree:
        return 0
    desc_nodes = {node.name: {'group': getattr(node, 'node_tree', None) is not None}
                  for node in tree.nodes}
    desc_links = [(link.from_node.name, link.from_socket.name, link.to_node.name, link.to_socket.name)
                  for link in tree.links]
    chains, already = [], False
    for vt in [n for n in tree.nodes if n.bl_idname == 'ShaderNodeVectorTransform'
               and n.convert_from == 'WORLD' and n.convert_to == 'OBJECT']:
        if any(link.from_node == vt and link.from_socket.name == 'Vector'
               and link.to_node.name.startswith(FACE_LIGHT_FIX_PREFIXES) for link in tree.links):
            already = True  # 幂等：该矢量变换已直连修正节点
            continue
        hops = mainlight_first_hops(vt.name, desc_nodes, desc_links)
        if hops:
            chains.append((vt, hops))
    if not chains:
        if already:
            return 1
        warnings.append(material.name + ': 未在克隆树中定位到主光方向链（' + FACE_LIGHT_INPUT
                        + '），脸部主光方向未修正')
        return 0
    if len(chains) > 1:
        warnings.append(material.name + ': 定位到 ' + str(len(chains)) + ' 条主光方向链，已全部处理')
    fix = _new_face_light_node(tree, *decision['plan'])
    fixed = 0
    for vt, hops in chains:
        rerouted = []
        for link in list(tree.links):
            # bpy_struct 包装器不稳定，必须用 RNA 相等（==）而不是 is 比较节点。
            if (link.from_node == vt and link.from_socket.name == 'Vector'
                    and (link.to_node.name, link.to_socket.name) in hops):
                rerouted.append(link.to_socket)
                tree.links.remove(link)
        tree.links.new(vt.outputs['Vector'], fix.inputs[0])
        for socket in rerouted:
            tree.links.new(fix.outputs['Vector'], socket)
        fixed += bool(rerouted)
    kind, payload = decision['plan']
    if kind == 'multiply180':
        warnings.append(material.name + ': 已插主光方向 180°Z 修正（源对象旋转 '
                        + f'{float(payload[2]):.2f}rad）')
    else:
        warnings.append(material.name + ': 已插主光方向 Z 轴修正（Δz=' + f'{float(payload):.2f}rad）')
    return 1 if fixed or already else 0


def split_copy(obj, collection):
    duplicate = obj.copy()
    duplicate.data = obj.data.copy()
    collection.objects.link(duplicate)
    duplicate.hide_set(False)
    duplicate.hide_viewport = False
    duplicate.hide_select = False
    duplicate[TAG] = 'result'
    duplicate['jmb_original'] = obj.name
    # Keep Blender's native separation: shape keys, weights and loop attributes follow faces.
    bpy.ops.object.select_all(action='DESELECT')
    duplicate.select_set(True)
    bpy.context.view_layer.objects.active = duplicate
    if len({p.material_index for p in duplicate.data.polygons}) > 1:
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.separate(type='MATERIAL')
        finally:
            if bpy.context.object and bpy.context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
    return [o for o in collection.objects if o.get('jmb_original') == obj.name]


def copy_modifier_snapshots(source, target):
    """Copy and own the imported stack; upstream refreshers must not delete it.

    Ruri's scene-wide vertex-stage refresh deletes its exact-name modifier when
    a material lacks ruri_uber_stack. CMB materials deliberately carry frozen
    nodes, not the original builder's complete runtime metadata. Retaining the
    upstream modifier name lets a later camera change destroy the snapshot.
    Ownership is expressed by a persistent name on the modifier AND root tree;
    never enable upstream rebuilding by fabricating its material metadata.
    """
    copied = []
    for modifier in source.modifiers:
        before = {m.as_pointer() for m in target.modifiers}
        with bpy.context.temp_override(object=source, active_object=source,
                                       selected_objects=[source, target], selected_editable_objects=[source, target]):
            outcome = bpy.ops.object.modifier_copy_to_selected(modifier=modifier.name)
        added = [m for m in target.modifiers if m.as_pointer() not in before]
        if 'FINISHED' not in outcome or len(added) != 1 or added[0].type != modifier.type:
            raise RuntimeError('修改器移植失败: ' + modifier.name)
        dest = added[0]
        if dest.type == 'NODES':
            if dest.node_group is None:
                raise RuntimeError('几何节点修改器缺少节点组: ' + modifier.name)
            if not dest.name.startswith('CMB '):
                dest.name = 'CMB ' + modifier.name
            # Each object's root holds its own modifier-specific group inputs.
            dest.node_group = dest.node_group.copy()
            if not dest.node_group.name.startswith('CMB '):
                dest.node_group.name = 'CMB ' + modifier.node_group.name
        copied.append(dest)
    return copied


def apply_transfer(filepath, objects, manifest, rows, rebuild_tangents=True, fix_face_light=True,
                   rebuild_outline=True, restore_final_mesh=True, transfer_source_uv=True):
    if bpy.context.mode != 'OBJECT':
        raise RuntimeError('请在物体模式下移植')
    manifest = validate_manifest(manifest)
    objects = mesh_objects(objects)
    if any(role_of(o) == 'result' for o in objects):
        raise RuntimeError('所选对象已移植，请先恢复原角色，避免重复叠加')
    active_originals = set()
    for c in bpy.data.collections:
        if role_of(c) == 'transaction':
            refs = c.get('cmb_original_refs')
            if refs is not None:
                active_originals.update(o for o in refs.values() if isinstance(o, bpy.types.Object))
            else:
                active_originals.update(bpy.data.objects.get(name) for name in
                                        json.loads(c.get('jmb_original_visibility', '{}')))
    if any(o in active_originals for o in objects):
        raise RuntimeError('此原角色已有移植结果，请先恢复旧结果后重试')
    choices = {(r['object'], r['slot']): r['choice'] for r in rows if r['choice']}
    if not choices:
        raise RuntimeError('没有已确认的匹配，请在列表中选择源条目')
    by_id = {e['id']: e for e in manifest['entries']}
    invalid = set(choices.values()) - set(by_id)
    if invalid:
        raise RuntimeError('匹配引用不存在的源条目: ' + ', '.join(sorted(invalid)))
    objects = [o for o in objects if any(k[0] == o.name for k in choices)]
    if not objects:
        raise RuntimeError('没有与所选对象对应的已确认匹配')
    original_visibility = {o.name: {'hide_set': o.hide_get(), 'hide_render': o.hide_render} for o in objects}
    original_selected = list(bpy.context.selected_objects)
    original_active = bpy.context.view_layer.objects.active
    before = id_blocks()
    report = {'package_id': manifest['package_id'], 'applied': [], 'skipped': [], 'warnings': []}
    collection = None
    post_installed = False
    rollback_actions = []
    scene = bpy.context.scene
    channel_count = len(getattr(scene, 'cmb_anim_channels', ()))
    post_state = {key: value.to_dict() if hasattr(value, 'to_dict') else value
                  for key, value in scene.items() if key.startswith('cmb_post_')}
    try:
        carriers_needed = {by_id[c]['carrier'] for c in choices.values()}
        from . import fur
        fur_profiles=[p for p in manifest.get('fur_profiles',[]) if any(fur.applicable(p,by_id[c]) for c in choices.values())]
        carriers_needed.update(p['carrier'] for p in fur_profiles)
        with bpy.data.libraries.load(filepath, link=False) as (source, dest):
            if not carriers_needed.issubset(source.objects):
                raise RuntimeError('材质包缺少载体')
            dest.objects = sorted(carriers_needed)
            # Explicit image IDs provide an exact source-name -> loaded-ID map,
            # including pre-existing .001 names and destination collisions.
            image_names = list(source.images)
            dest.images = list(image_names)
        carrier_map = dict(zip(sorted(carriers_needed), dest.objects))
        for choice in choices.values():
            entry = by_id[choice]
            carrier = carrier_map.get(entry['carrier'])
            if (carrier is None or carrier.type != 'MESH' or entry['slot'] >= len(carrier.material_slots)
                    or carrier.material_slots[entry['slot']].material is None):
                raise RuntimeError('材质包载体或材质槽无效: ' + entry['id'])
        from . import mesh_state, uv_transfer
        references = {o.data for o in carrier_map.values()
                       if manifest.get('mesh_payload_version') == mesh_state.VERSION
                       and o.get(mesh_state.KEY) == mesh_state.VERSION}
        source_uv_refs = manifest.get('source_uv_references', {})
        for name, carrier in carrier_map.items():
            if name in source_uv_refs:
                references.add(uv_transfer.validate_reference(carrier, source_uv_refs[name]))
        audit(set(carrier_map.values()), reference_meshes=references)
        from . import color_management
        required_images = {item for item in dependencies(set(carrier_map.values()))
                           if isinstance(item, bpy.types.Image)}
        if manifest.get('image_color_metadata_version'):
            missing_colors = {name for name, image in zip(image_names, dest.images)
                              if image in required_images and name not in manifest['image_colorspaces']}
            if missing_colors:raise RuntimeError('材质包缺少图像色彩空间记录: ' + ', '.join(sorted(missing_colors)))
        report['image_colorspaces'] = color_management.restore_loaded(
            filepath, list(zip(image_names, dest.images)), required_images, manifest.get('image_colorspaces'))
        # Only the images used by the selected carriers should survive import.
        unused_images = {image for image in dest.images
                         if image is not None and image not in required_images and image not in before}
        if unused_images:
            bpy.data.batch_remove(ids=unused_images)
        if transfer_source_uv and manifest.get('package_kind') != 'final' and not source_uv_refs:
            report['warnings'].append('该旧源包未携带 UV 数据，继续按原流程移植；需要从源工程重新导出才能补齐 UV')
        restored_parts = set()
        prebuilt_fur_parts = set()
        source_uv_jobs = []
        attribute_checks = []
        if manifest.get('package_kind') == 'final' and not manifest.get('mesh_payload_version'):
            report['warnings'].append('该最终包未携带参考网格，无法还原表面法线/描边校正；需要从校正工程重新导出')
        # 0.5.0（A05）：引用预扫描——载体载入后、任何修改动作前，一次性核对全部 CMB_REF_
        # 占位能否在目标工程按名解析；缺失即整单中止并给出完整名单（判定口径与下方逐条
        # 解析兜底一致：精确同名 + 对象类型匹配）。
        imported_refs = id_blocks() - before
        missing_refs = set()
        for item in imported_refs:
            if isinstance(item, (bpy.types.Object, bpy.types.Collection)) and role_of(item) == 'reference':
                name = item.get('jmb_source_name', '')
                pool = bpy.data.objects if isinstance(item, bpy.types.Object) else bpy.data.collections
                resolved = pool.get(name)
                if not (resolved and resolved not in imported_refs
                        and (not isinstance(item, bpy.types.Object)
                             or resolved.type == item.get('jmb_source_type'))):
                    missing_refs.add(name)
        if missing_refs:
            raise RuntimeError('缺少对象/集合引用，已中止（未做任何修改）。最终包应用要求目标工程'
                               '存在同名对象: ' + ', '.join(sorted(missing_refs))
                               + '；若你的模型改过名，请向包作者索取映射说明')
        collection = bpy.data.collections.new(RESULT_COLLECTION_PREFIX + manifest['package_id'][:8])
        bpy.context.scene.collection.children.link(collection)
        collection[TAG] = 'transaction'
        collection['jmb_original_visibility'] = json.dumps(original_visibility)
        collection['cmb_original_refs'] = {o.name: o for o in objects}
        report['collection'] = collection.name
        for obj in objects:
            if not any(k[0] == obj.name for k in choices):
                continue
            from . import fur_layers
            existing_fur = fur_layers.prebuilt_profiles(obj, choices, by_id, fur_profiles)
            old_mats = [s.material for s in obj.material_slots]
            # Record the source slot in temporary material metadata via identity mapping.
            parts = split_copy(obj, collection)
            for part in parts:
                used = {p.material_index for p in part.data.polygons}
                if len(used) != 1:
                    raise RuntimeError('按材质分离后仍存在多个材质区域: ' + part.name)
                slot = next(iter(used))
                target_mat = part.material_slots[slot].material if slot < len(part.material_slots) else None
                source_slots = [i for i, mat in enumerate(old_mats) if mat == target_mat]
                possible = {choices[(obj.name, i)] for i in source_slots if (obj.name, i) in choices}
                if len(possible) > 1:
                    raise RuntimeError('同一材质占用多个槽且映射不同，需先整理材质槽')
                choice = next(iter(possible), None)
                if not choice:
                    report['skipped'].append({'object': part.name, 'material': target_mat.name if target_mat else ''})
                    continue
                entry = by_id[choice]
                carrier = carrier_map[entry['carrier']]
                part.name = obj.name + '__' + (target_mat.name if target_mat else 'Material')
                # 0.5.0（A03）：把 MMD 材质名另存部件自定义属性——最终包导出优先读它，
                # 免疫 63 字符对象名截断与材质名含 '__' 的 rsplit 反解歧义。
                part['jmb_part_mat'] = target_mat.name if target_mat else 'Material'
                part.data.materials.clear()
                for mat in carrier.data.materials:
                    part.data.materials.append(mat)
                for face in part.data.polygons:
                    face.material_index = entry['slot']
                # 0.4.0：把导出时采集的 Uber 参数列/部位写回克隆材质（克隆器 clear_custom
                # 已抹掉原属性）；参数面板靠它们定位参数图列与部位表。旧包无键则跳过。
                cloned = part.material_slots[entry['slot']].material
                if entry.get('param_col') is not None:
                    cloned['ruri_param_col'] = int(entry['param_col'])
                if entry.get('uber_part'):
                    cloned['ruri_uber_part'] = str(entry['uber_part'])
                from . import defaults
                defaults.bind(cloned, entry)
                # 0.2.3 脸部主光方向修正：换槽完成后对克隆材质树执行（门槛/定位/幂等见
                # face_light_decision 与 fix_face_mainlight；只动主光链，副光链不碰）。
                # 0.5.0（A10）：final 包条目跳过——修正已随包固化，且 final 载体不记录
                # jmb_source_rot_euler，走旧逻辑只会产出误导性的「v0.3 或更旧」提示。
                if not entry.get('cmb_final'):
                    src_rot = carrier.get('jmb_source_rot_euler')
                    fix_face_mainlight(part.material_slots[entry['slot']].material,
                                       entry['source_object'],
                                       list(src_rot) if src_rot is not None else None,
                                       list(obj.matrix_world.to_euler()),
                                       report['warnings'], enabled=fix_face_light)
                # Import retains target rig and other MMD modifiers; copy the source stack after them.
                copied_modifiers = copy_modifier_snapshots(carrier, part)
                part['jmb_entry'] = choice
                part['jmb_package'] = manifest['package_id']
                part['cmb_source_object'] = entry['source_object']
                part['cmb_source_material'] = entry['source_material']
                mesh_result = None
                if entry.get('reference_mesh'):
                    if restore_final_mesh:
                        mesh_result = mesh_state.restore_reference(carrier, part, entry['reference_mesh'])
                        restored_parts.add(part)
                        report.setdefault('mesh_restorations', []).append(mesh_result)
                    else:
                        report['warnings'].append(part.name + ': 已关闭参考网格还原，未继承最终包网格校正')
                if transfer_source_uv and entry['carrier'] in source_uv_refs:
                    source_uv_jobs.append((carrier, part, source_uv_refs[entry['carrier']], entry['slot']))
                fur.restore_regions(part,entry.get('requirements',{}).get('fur_regions',[]))
                for profile in fur_profiles:
                    if not fur.applicable(profile,entry):continue
                    fur_report=fur.attach(part,carrier_map[profile['carrier']],profile,manifest,entry,existing_fur)
                    if fur_report:
                        report.setdefault('fur_shells',[]).append(fur_report)
                        if fur_report.get('mode') == 'REUSED_EXISTING':prebuilt_fur_parts.add(part)
                if rebuild_tangents and not (mesh_result and mesh_result['tangents']):
                    basis_result = copy_missing_tangents(
                        part, entry['requirements']['attributes'], report['warnings'],
                        tangent_basis=entry['requirements'].get('tangent_basis'), check_missing=False)
                    if basis_result:
                        report.setdefault('tangent_rebuilds', []).append(basis_result)
                attribute_checks.append((part, entry['requirements']['attributes']))
                report['applied'].append({'object': part.name, 'entry': choice,
                                          'source_material': entry['source_material'], 'modifiers': len(copied_modifiers),
                                          'modifier_names': [m.name for m in copied_modifiers]})
            obj.hide_set(True)
            obj.hide_render = True
        # Source reference placeholders carry no geometry; resolve only exact names/types.
        imported = id_blocks() - before
        unresolved = []
        for item in list(imported):
            if isinstance(item, (bpy.types.Object, bpy.types.Collection)) and role_of(item) == 'reference':
                name = item.get('jmb_source_name', '')
                pool = bpy.data.objects if isinstance(item, bpy.types.Object) else bpy.data.collections
                resolved = pool.get(name)
                if resolved and resolved not in imported and (not isinstance(item, bpy.types.Object) or resolved.type == item.get('jmb_source_type')):
                    item.user_remap(resolved)
                else:
                    unresolved.append(name)
        if unresolved:
            raise RuntimeError('缺少对象/集合引用，已回滚。请先在目标工程建立对应名称的对象/集合: ' + ', '.join(unresolved))
        if rebuild_outline:
            from . import outline
            outline_report = outline.rebuild(list(collection.objects), preserve=restored_parts)
            report['outline_smoothing'] = outline_report
            report['warnings'].extend(outline_report['warnings'])
            report['warnings'].extend(item['reason'] for item in outline_report['skipped'])
        # Copy after the existing geometry algorithms: newly added UV names must
        # not change their tangent-layer choice or consume a needed outline slot.
        for job in source_uv_jobs:
            uv_result = uv_transfer.transfer(*job, fur_layer_hint=job[1] in prebuilt_fur_parts)
            report.setdefault('source_uv_transfers', []).append(uv_result)
            for layer in uv_result['layers']:
                if layer['status'] == 'SKIPPED':
                    report['warnings'].append(uv_result['object'] + ' / ' + layer['source_layer'] + ': UV 已跳过，' + layer['reason'])
                elif layer['status'] == 'CONFLICT_COPY':
                    report['warnings'].append(uv_result['object'] + ' / ' + layer['source_layer'] + ': 保留目标原层，源数据另存为 ' + layer['target_layer'])
        for part, requested in attribute_checks:
            missing = set(requested) - set(part.data.attributes.keys())
            if missing:
                report['warnings'].append(part.name + ': 缺少属性 ' + ', '.join(sorted(missing)) + '；未伪造源 UV/顶点色')
        # Remove temporary package carriers/references; materials/groups stay used by results.
        temporary_meshes = {o.data for o in carrier_map.values()}
        from . import runtime
        from . import defaults, postprocess
        if manifest.get('package_kind') != 'final':
            default_report = defaults.apply(list(collection.objects), bpy.context.scene)
            report['recommended_defaults'] = default_report
            report['warnings'].extend(default_report['warnings'])
        from . import lighting
        report['runtime_initialization'] = lighting.initialize(
            scene, objects=list(collection.objects), transaction=rollback_actions)
        report['warnings'].extend(report['runtime_initialization']['warnings'])
        # Avoid localizing or changing an existing character's node trees while
        # this new transfer can still fail and remove its newly created IDs.
        camera_report = runtime.sync_scene(scene, objects=list(collection.objects))
        report['camera_sync'] = camera_report
        report['warnings'].extend(camera_report['warnings'])
        temporary_refs = {item for item in imported if isinstance(item, (bpy.types.Object, bpy.types.Collection))
                          and role_of(item) == 'reference'}
        bpy.data.batch_remove(ids=set(carrier_map.values()))
        for item in temporary_meshes | temporary_refs:
            # Only this import's scaffolding; never globally purge unused assets.
            if item.users == int(item.use_fake_user):
                item.use_fake_user = False
                bpy.data.batch_remove(ids={item})
        bpy.ops.object.select_all(action='DESELECT')
        for part in collection.objects:
            part.select_set(True)
        if collection.objects:
            bpy.context.view_layer.objects.active = next(iter(collection.objects))
        post_report = postprocess.install(bpy.context.scene, automatic=True)
        report['postprocess'] = post_report
        post_installed = post_report['status'] == 'installed'
        if post_report.get('message'):
            report['warnings'].append(post_report['message'])
        write_report(report)
        # The transaction is now committed. Refresh the independent light table
        # and bone basis through the normal guarded lifecycle coordinator.
        runtime._safe_sync(scene, force=True)
        return report
    except Exception:
        if post_installed:
            from . import postprocess
            postprocess.restore(bpy.context.scene)
        for rollback in reversed(rollback_actions):
            rollback()
        channels = getattr(scene, 'cmb_anim_channels', None)
        if channels is not None:
            while len(channels) > channel_count:
                channels.remove(len(channels) - 1)
        for key in list(scene.keys()):
            if key.startswith('cmb_post_'):
                del scene[key]
        for key, value in post_state.items():
            scene[key] = value
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.data.batch_remove(ids=id_blocks() - before)
        for obj in objects:
            state = original_visibility[obj.name]
            obj.hide_set(state['hide_set'])
            obj.hide_render = state['hide_render']
        for obj in original_selected:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = original_active
        raise


def restore(collection):
    if role_of(collection) != 'transaction':
        raise RuntimeError('不是移植结果集合')
    states = json.loads(collection['jmb_original_visibility'])
    refs = collection.get('cmb_original_refs')
    originals = {name: refs.get(name) if refs is not None else bpy.data.objects.get(name) for name in states}
    missing = [name for name, obj in originals.items() if not isinstance(obj, bpy.types.Object)]
    if missing:
        raise RuntimeError('原角色不存在，已保留全部移植结果，不能恢复: ' + ', '.join(missing))
    if collection.children:
        raise RuntimeError('结果集合含子集合，请先移出再恢复')
    results = list(collection.objects)
    for obj in results:
        if role_of(obj) != 'result':
            raise RuntimeError('结果集合中含有其他对象，请先移出，防止误删除')
    meshes = {o.data for o in results}
    bpy.data.batch_remove(ids=set(results) | {collection})
    bpy.data.batch_remove(ids={m for m in meshes if m.users == 0})
    for name, state in states.items():
        obj = originals[name]
        if obj:
            obj.hide_set(state['hide_set'])
            obj.hide_render = state['hide_render']
    return len(results)


def rebuild_outline_smoothing(objects):
    """Use all parts of the selected original in each transaction for seam continuity."""
    from . import outline
    targets = set()
    for obj in objects:
        if obj.type != 'MESH' or role_of(obj) != 'result':
            continue
        targets.add(obj)
        for collection in obj.users_collection:
            if role_of(collection) == 'transaction':
                targets.update(o for o in collection.objects if o.type == 'MESH'
                               and role_of(o) == 'result'
                               and o.get('jmb_original') == obj.get('jmb_original'))
    if not targets:
        raise RuntimeError('请选择移植结果网格')
    report = outline.rebuild(sorted(targets, key=lambda o: o.name))
    write_report({'outline_smoothing': report})
    return report


# --------------------------------------------------------------------------
# Ruri Uber 参数引擎（0.4.0 params-expose）。
#
# 材质基础参数编码在一张 1024x51 RGBA-float 图（"Ruri Endfield Uber Params"，
# 全库一张数据块）里：一材质=一列（列号=材质属性 ruri_param_col），texel 行=参数槽，
# 每行 RGBA=4 个 float。9 个部位（Standard/Face/Eyes/Hair/Fur/Eyebrow/VFX/
# OverlayShadow/LiquidAg）各有独立参数编排——跨部位同行号语义不同，必须按 part
# 查表。权威布局在插件目录 ruri_params_layout.json（按名索引，勿硬编码 texel；
# 上游 codegen stamp 变更须重提取）。

LAYOUT_FILE = 'ruri_params_layout.json'

CURATED_PBR = ('_Smoothness', '_Metallic', '_Specular', '_RoughnessIntensity',
               '_MetallicIntensity', '_SpecularIntensity', '_CharacterParams0',
               '_CharacterParams6', '_CharacterParams7', '_CharacterParams13')
CURATED_NPR = ('_ColorAdjustmentRimWidth', '_ColorAdjustmentRimIntensity',
               '_ColorAdjustmentRimColor', '_SDFRimColor', '_SkinRimOffScale',
               '_FaceRimOffScale', '_CharacterParams8', '_CharacterParams9',
               '_CharacterParams14')

_layout_cache = None


def load_layout(path=None, reload=False):
    """Load and cache the bundled uber-param layout table (ruri_params_layout.json).

    Validates _meta.stamp (16-hex codegen stamp; the whole table is a single
    codegen artifact — a missing/malformed stamp means the file is not the
    authoritative extraction) and the table geometry declared by _meta."""
    global _layout_cache
    if _layout_cache is not None and not reload:
        return _layout_cache
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LAYOUT_FILE)
    with open(path, encoding='utf-8') as stream:
        layout = json.load(stream)
    meta = layout.get('_meta') or {}
    if not re.fullmatch(r'[0-9a-f]{16}', str(meta.get('stamp') or '')):
        raise RuntimeError('参数布局表缺少有效 codegen stamp，须重提取: ' + path)
    if not int(meta.get('mat_table_w', 0)) or not int(meta.get('mat_table_h', 0)):
        raise RuntimeError('参数布局表缺少表尺寸（mat_table_w/h）: ' + path)
    for part, data in (layout.get('parts') or {}).items():
        if not isinstance((data or {}).get('param_index'), dict):
            raise RuntimeError('参数布局表部位缺少 param_index: ' + str(part))
    _layout_cache = layout
    return _layout_cache


def param_slot(part, name):
    """(texel, comp, type) of one parameter in one part's table, or None.

    Cross-part tables allocate texels independently (same row means different
    things per part), so lookups must always go through (part, name)."""
    index = (load_layout().get('parts') or {}).get(part, {}).get('param_index') or {}
    cell = index.get(name)
    if not cell:
        return None
    return int(cell['t']), int(cell['c']), str(cell['type'])


def param_names(part):
    """All parameter names of one part, in table order."""
    return [entry.get('name') for entry in (load_layout().get('parts') or {}).get(part, {}).get('params') or []
            if entry.get('name')]


def param_ui_kind(part, name):
    """Use declared authoring semantics; V4 is storage, not necessarily color."""
    for entry in (load_layout().get('parts') or {}).get(part, {}).get('params') or []:
        if entry.get('name') == name:
            return entry.get('ui_kind') or ('VECTOR' if entry.get('type') in ('V3', 'V4') else 'VALUE')
    return 'VALUE'


def param_search_text(part, name, label=''):
    """RGBA.w aliases resolve to their existing row, never duplicate storage slots."""
    terms = [str(name), str(label)]
    slot = param_slot(part, name)
    if slot and slot[2] == 'V4':
        terms += [name + '_w', 'W 第四分量']
        if param_ui_kind(part, name) == 'COLOR':
            terms += ['Alpha RGBA']
    if name == '_BaseColor':
        terms += ['底色 底色Alpha 底色 Alpha 透明度']
    return ' '.join(terms).casefold()


def material_default_role(material, source_object=''):
    from . import defaults
    return defaults.resolve(material, source_object=source_object)


def default_value(part, name):
    """Layout default of one parameter: float for F, [r,g,b,a] for V4, [r,g,b] for
    V3; None when the part does not define it."""
    for entry in (load_layout().get('parts') or {}).get(part, {}).get('params') or []:
        if entry.get('name') != name:
            continue
        rgb = [float(v) for v in entry.get('rgb') or ()]
        kind = entry.get('type')
        if kind == 'F':
            return rgb[0] if rgb else 0.0
        if kind == 'V4':
            return rgb + [float(entry.get('a', 1.0))]
        if kind == 'V3':
            return rgb[:3]
        return None
    return None


def value_differs(part, name, value, epsilon=1e-4):
    """Whether one current value deviates from the layout default (None never counts)."""
    default = default_value(part, name)
    if default is None or value is None:
        return False
    if not isinstance(value, (list, tuple)):
        return abs(float(value) - float(default)) > epsilon
    values = list(value)
    return len(values) != len(default) or any(
        abs(float(a) - float(b)) > epsilon for a, b in zip(values, default))


def read_param(pixels_flat, width, col, part, name):
    """Pure: read one parameter from a flat RGBA pixel buffer (row-major,
    width*height*4 floats; one material per column).

    F -> float; V4 -> [r,g,b,a]; V3 -> [r,g,b]. None when the part lacks the
    parameter; RuntimeError on an out-of-range column."""
    slot = param_slot(part, name)
    if slot is None:
        return None
    texel, comp, kind = slot
    width = int(width)
    if not (0 <= int(col) < width):
        raise RuntimeError(f'参数列号越界: {col}（有效范围 0..{width - 1}）')
    base = (texel * width + int(col)) * 4
    if pixels_flat is None or base + 4 > len(pixels_flat):
        raise RuntimeError('参数图高度/像素缓冲不足，无法读取: ' + str(part) + '/' + str(name))
    if kind == 'F':
        return float(pixels_flat[base + comp])
    values = [float(pixels_flat[base + offset]) for offset in range(3)]
    if kind == 'V4':
        values.append(float(pixels_flat[base + 3]))
    return values


def params_write_pixels(pixels, width, col, updates):
    """Pure: apply [(part, name, value)] onto a flat RGBA pixel buffer for one
    material column. F writes its single channel; V4 rgb+alpha; V3 rgb only.

    Returns {'written': n, 'unknown': [part/name...]} for names absent from the
    part table (caller decides whether that is fatal). Column 0 is the shared
    defaults column of the uber template and is never a material's column, so
    writes must target 1..width-1."""
    width = int(width)
    if not (0 < int(col) < width):
        raise RuntimeError(f'参数列号越界: {col}（有效范围 1..{width - 1}，0 列为模板默认列）')
    unknown, written, edits = [], 0, []
    for part, name, value in updates:
        slot = param_slot(part, name)
        if slot is None:
            unknown.append(str(part) + '/' + str(name))
            continue
        texel, comp, kind = slot
        base = (texel * width + int(col)) * 4
        if base + 4 > len(pixels):
            raise RuntimeError('参数图高度/像素缓冲不足: ' + str(part) + '/' + str(name))
        try:
            if kind == 'F':
                changes = [(base + comp, float(value))]
            else:
                vector = list(value) if isinstance(value, (list, tuple)) else [float(value)] * 3
                if len(vector) not in (3, 4):
                    raise ValueError('颜色/向量需要 3 或 4 个分量')
                changes = [(base + i, float(vector[i])) for i in range(3)]
                if kind == 'V4':
                    changes.append((base + 3, float(vector[3]) if len(vector) == 4 else 1.0))
            if any(not math.isfinite(v) or abs(v) > 3.4e38 for _, v in changes):
                raise ValueError('数值必须是有限浮点数')
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError('参数值无效 ' + str(part) + '/' + str(name) + ': ' + str(exc)) from exc
        edits.extend(changes)
        written += 1
    for index, value in edits:
        pixels[index] = value
    return {'written': written, 'unknown': unknown}


def write_params_batch(image, columns, pack=False):
    """Validate all columns before one GPU upload. Animation never packs per frame."""
    if image is None:
        raise RuntimeError('材质树未引用 Uber 参数图，无法写参数')
    width, height = int(image.size[0]), int(image.size[1])
    if not width or not height:
        raise RuntimeError('参数图尺寸异常: ' + image.name)
    buffer = array('f', [0.0]) * (width * height * 4)
    image.pixels.foreach_get(buffer)
    previous = array('f', buffer)
    previous_alpha = image.alpha_mode
    result = {'written': 0, 'unknown': [], 'changed': False}
    for col, updates in columns:
        report = params_write_pixels(buffer, width, col, updates)
        result['written'] += report['written']
        result['unknown'].extend(report['unknown'])
    result['changed'] = buffer != previous or (result['written'] > 0 and image.alpha_mode != 'CHANNEL_PACKED')
    if result['changed'] or (pack and (image.is_dirty or not image.packed_file)):
        image.pixels.foreach_set(buffer)
        # 参数图是原始浮点通道存储（真机数据块 alpha_mode 即 CHANNEL_PACKED）。STRAIGHT/
        # PREMUL 图在 pack→保存→重载时会被 Blender 按 alpha 预乘/还原（rgb×a），静默改写
        # 数值；写路径统一归一到 CHANNEL_PACKED，保证像素经包往返不变。
        if image.alpha_mode != 'CHANNEL_PACKED':
            image.alpha_mode = 'CHANNEL_PACKED'
        image.update()
        image.update_tag()
        # update() marks the full texture dirty. Do not free graphics resources
        # from frame handlers on a render worker while the UI may use them.
        try:
            if pack:
                image.pack()
        except Exception as exc:
            image.pixels.foreach_set(previous)
            image.alpha_mode = previous_alpha
            image.update()
            raise RuntimeError('参数图打包失败，数值已撤回: ' + image.name) from exc
    return result


def write_params(image, col, updates):
    """Manual/preset write; also update persistent channels in the current Scene."""
    updates = list(updates)
    result = write_params_batch(image, [(col, updates)], pack=True)
    from . import animation
    scene = getattr(bpy.context, 'scene', None)
    if scene is not None:
        animation.adopt_updates(scene, image, col, updates)
    return result


def param_col_from_nodes(nodes):
    """Pure: the column number carried by RuriMatCol value nodes in a node iterable.

    Template groups keep 0.0 placeholders under the same label, so only a positive
    value counts as a real column; None when none exists."""
    for node in nodes:
        if getattr(node, 'bl_idname', '') != 'ShaderNodeValue' or getattr(node, 'label', '') != 'RuriMatCol':
            continue
        try:
            value = float(node.outputs[0].default_value)
        except (AttributeError, IndexError, TypeError, ValueError):
            continue
        if value > 0:
            return int(round(value))
    return None


def material_param_col(material):
    """ruri_param_col property first, then the tree's RuriMatCol value node.

    The cloner's clear_custom wipes ID properties on cloned materials, so the
    exported manifest value is authoritative after a transfer; the node scan is
    the fallback for materials that never went through one."""
    if material is None:
        return None
    value = material.get('ruri_param_col')
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    if not material.node_tree:
        return None
    return param_col_from_nodes(node for tree in walk_trees(material.node_tree) for node in tree.nodes)


UBER_PART_GROUP_KEYWORDS = ('Face', 'Standard', 'Hair', 'Eyes', 'VFX', 'OverlayShadow', 'LiquidAg', 'Fur')
UBER_GROUP_NAME_PREFIX = 'ruri endfield uber '  # 组树名统一前缀（casefold 比较），部位词在其后


def uber_part_from_group_names(names):
    """Pure: infer the uber part from node-group names (0.4.1 旧包无属性兜底)。

    部位组树名形如 'Ruri Endfield Uber Face s1'——'s<数字>' 记号前的完整词必须与部位
    关键词全等（'FaceShadow s0' 不匹配 Face；共享系 'Z2 s0'、顶点动画系
    'Vertex Fur' 不投票）；无 s<数字> 记号时要求名字与关键词全等；克隆重名的
    Blender 后缀 '.001' 剥离后再匹配。多个关键词同时出现取票数最多者，平票取
    关键词表序靠前者；无任何命中返回 None。"""
    votes = []
    for name in names:
        stem = str(name).strip()
        if stem.casefold().startswith(UBER_GROUP_NAME_PREFIX):
            stem = stem[len(UBER_GROUP_NAME_PREFIX):].strip()
        stem = re.sub(r'\.\d{1,9}$', '', stem)  # 克隆重名后缀 Ruri Endfield Uber Face s0.001
        match = re.match(r'^(.+?)\s+s\d+$', stem)
        word = match.group(1) if match else stem
        if word in UBER_PART_GROUP_KEYWORDS:
            votes.append(word)
    if not votes:
        return None
    return sorted(set(votes), key=lambda word: (-votes.count(word),
                                                UBER_PART_GROUP_KEYWORDS.index(word)))[0]


def material_uber_part_groups(material):
    """Every node-group name referenced anywhere in the material's trees (part
    inference input; duplicates kept so votes count references)."""
    return [getattr(node, 'node_tree').name
            for tree in walk_trees(material.node_tree if material else None)
            for node in tree.nodes if getattr(node, 'node_tree', None)]


def material_uber_part(material):
    """ruri_uber_part property (which part's table this material reads); 0.4.1 起
    旧包兜底：属性缺失（克隆器 clear_custom 抹掉、且 0.4.0 之前导出的包 manifest 无
    uber_part 键）时按材质树引用的组树名推断部位（GROUP 组成按部位可区分，
    body/皮肤=Face 组系）；两者皆无返回 None。

    注意：body/皮肤系材质的 part 是 Face——部位来自 Unity shader 名，不得按材质名猜。"""
    part = material.get('ruri_uber_part') if material else None
    if part:
        return str(part)
    if material is None or not material.node_tree:
        return None
    return uber_part_from_group_names(material_uber_part_groups(material))


def material_uber_part_inferred(material):
    """Whether material_uber_part came from group-name inference (property absent);
    the params panel annotates such rows with （推断）."""
    return bool(material is not None and not material.get('ruri_uber_part')
                and material_uber_part(material))


def material_param_col_inferred(material):
    """Whether material_param_col came from the RuriMatCol node scan (property absent);
    the params panel annotates such rows with （推断）."""
    return bool(material is not None and material.get('ruri_param_col') is None
                and material_param_col(material) is not None)


def mat_param_image(material):
    """The uber-params data image referenced by the material's node trees
    (identified by an image name containing 'Uber Params'). All cloned materials
    share one cloned copy of the single source datablock."""
    for tree in walk_trees(material.node_tree if material else None):
        for node in tree.nodes:
            image = getattr(node, 'image', None)
            if image and (image.get('cmb_data_role') == 'PARAMS' or 'uber params' in image.name.casefold()):
                return image
    return None


def stamp_matches(material):
    """Whether the material's recorded uber stamp agrees with the layout table.

    Materials without a recorded stamp (non-uber or stripped) pass vacuously."""
    stamp = material.get('ruri_uber_stamp') if material else None
    if not stamp:
        return True
    return str(stamp) == str(load_layout()['_meta']['stamp'])


def param_group(name):
    """Auto group for the prefix filter: the stem up to the first camelCase
    boundary ('_ColorAdjustmentRimWidth' -> 'ColorAdjustment')."""
    stem = str(name)[1:] if str(name).startswith('_') else str(name)
    for index in range(1, len(stem) - 1):
        if stem[index - 1].islower() and stem[index].isupper():
            return stem[:index]
    return stem


def curated_names(part, kind):
    """Curated名单 ∩ 当前部位表（不存在的自动隐藏）。NPR 附加该部位全部 _Fresnel* 行
    （仅 VFX 部位表含菲涅尔组）。"""
    present = set(param_names(part))
    wanted = list(CURATED_PBR if kind == 'PBR' else CURATED_NPR)
    if kind != 'PBR':
        wanted += sorted(name for name in present if name.startswith('_Fresnel'))
    return [name for name in wanted if name in present]


def param_group_label(part, name):
    """(interface group, row label) of one param from the merged authoritative
    INTERFACE data (ruri_params_layout.json parts[*].params group/label); ('', '')
    for engine-internal params without a panel group, or when absent entirely."""
    for entry in (load_layout().get('parts') or {}).get(part, {}).get('params') or []:
        if entry.get('name') == name:
            return str(entry.get('group') or ''), str(entry.get('label') or '')
    return '', ''


def interface_groups(part):
    """Interface group names present in one part's table, in table order; '' is
    prepended when the part has engine-internal (ungrouped) params."""
    groups, ungrouped = [], False
    for entry in (load_layout().get('parts') or {}).get(part, {}).get('params') or []:
        group = str(entry.get('group') or '')
        if group:
            if group not in groups:
                groups.append(group)
        else:
            ungrouped = True
    return ([''] if ungrouped else []) + groups


def read_material_column(material):
    """Pixel snapshot of one material's params column: (part, col, width, pixels),
    or None when the material has no readable part/column/params image."""
    part = material_uber_part(material)
    col = material_param_col(material)
    image = mat_param_image(material)
    if not part or not col or image is None:
        return None
    width, height = int(image.size[0]), int(image.size[1])
    if not width or not height:
        return None
    pixels = array('f', [0.0]) * (width * height * 4)
    image.pixels.foreach_get(pixels)
    return part, int(col), width, pixels


def param_deviations(material):
    """{param_name: current_value} of the material's params that deviate from the
    layout defaults (value_differs); {} when there is no readable params column."""
    snapshot = read_material_column(material)
    if snapshot is None:
        return {}
    part, col, width, pixels = snapshot
    result = {}
    for name in param_names(part):
        try:
            value = read_param(pixels, width, col, part, name)
        except RuntimeError:
            continue
        if value is not None and value_differs(part, name, value):
            result[name] = value
    return result


def build_params_preset(objects, package_id='', plugin_version=''):
    """Save an explicit current-value snapshot, including original layout defaults.

    Returns {'package_id', 'plugin_version', 'created', 'kind', 'mats'} with
    mats keyed '<对象名>::<材质名>' -> {'part', 'col', 'params': {名: 值}}."""
    from . import animation
    animation.flush(bpy.context.scene)
    mats, seen = {}, set()
    for obj in objects:
        if obj.type != 'MESH':
            continue
        for slot in obj.material_slots:
            material = slot.material
            if not material or material.name in seen:
                continue
            seen.add(material.name)
            snapshot = read_material_column(material)
            if snapshot is None:
                continue
            if not stamp_matches(material):
                raise RuntimeError('材质布局版本不一致，不能保存参数: ' + material.name)
            part, col, width, pixels = snapshot
            current = {name: read_param(pixels, width, col, part, name)
                       for name in param_names(part)}
            current = {name: value for name, value in current.items() if value is not None}
            if current:
                mats[obj.name + '::' + material.name] = {
                    'object': obj.name, 'material': material.name,
                    'part': part, 'col': col, 'params': current}
    return {'package_id': package_id, 'plugin_version': plugin_version,
            'created': datetime.now().isoformat(timespec='seconds'),
            'kind': 'cmb-params-preset', 'value_mode': 'snapshot', 'mats': mats}


def apply_params_preset(preset):
    """Apply explicit stored values; old sparse presets remain valid overlays.

    Returns {'applied', 'params', 'skipped'}; skipped carries '<键>（原因）' strings."""
    if not isinstance(preset, dict) or not isinstance(preset.get('mats'), dict):
        raise RuntimeError('参数预设需要 mats 对象')
    written = 0
    skipped = []
    planned, physical_values, by_image = [], {}, {}
    for key, record in (preset.get('mats') or {}).items():
        if not isinstance(key, str) or not isinstance(record, dict) or not isinstance(record.get('params', {}), dict):
            raise RuntimeError('参数预设条目格式无效')
        material_name = record.get('material') or (key.split('::', 1)[-1] if '::' in key else key)
        if not isinstance(material_name, str):
            raise RuntimeError('参数预设材质名无效: ' + key)
        material = bpy.data.materials.get(material_name)
        if material is None:
            skipped.append(key + '（无此材质）')
            continue
        part = material_uber_part(material)
        col = material_param_col(material)
        image = mat_param_image(material)
        if not part or not col or image is None:
            skipped.append(key + '（材质无参数列/部位/参数图）')
            continue
        if record.get('part') and record['part'] != part:
            skipped.append(key + f"（部位不符 {record['part']}≠{part}）")
            continue
        if not stamp_matches(material):
            raise RuntimeError('材质布局版本不一致，不能套用参数: ' + material.name)
        updates = [(part, name, value) for name, value in (record.get('params') or {}).items()]
        if not updates:
            continue
        width, height = image.size
        buffer = array('f', [0.0]) * (width * height * 4)
        image.pixels.foreach_get(buffer)
        # Validate every record before the first image write.
        params_write_pixels(buffer, width, col, updates)
        # Distinct material names can point at one physical image column. Detect
        # contradictory requests by texel/component, including shader aliases.
        for update_part, name, value in updates:
            slot = param_slot(update_part, name)
            if slot is None:
                continue
            texel, component, kind = slot
            components = (component,) if kind == 'F' else range(4 if kind == 'V4' else 3)
            for component in components:
                index = (texel * width + col) * 4 + component
                physical_key = (image, index)
                previous = physical_values.get(physical_key)
                if previous is not None and previous[0] != buffer[index]:
                    raise RuntimeError('参数预设共享列数值冲突，未写入任何参数: '
                                       + previous[1] + ' / ' + key)
                physical_values[physical_key] = (buffer[index], key)
        columns = by_image.setdefault(image, {})
        merged = columns.setdefault(col, {})
        for update_part, name, value in updates:
            merged[(update_part, name)] = value
        planned.append((image, col, updates))
    snapshots = {}
    for image, _, _ in planned:
        if image not in snapshots:
            buffer = array('f', [0.0]) * len(image.pixels)
            image.pixels.foreach_get(buffer)
            snapshots[image] = (buffer, image.alpha_mode)
    try:
        for image, columns in by_image.items():
            batch = [(col, [(part, name, value) for (part, name), value in updates.items()])
                     for col, updates in columns.items()]
            result = write_params_batch(image, batch, pack=True)
            written += result['written']
            if result['unknown']:
                skipped.append(image.name + '（未知参数 ' + ', '.join(result['unknown']) + '）')
    except Exception:
        for image, (buffer, alpha_mode) in snapshots.items():
            image.pixels.foreach_set(buffer)
            image.alpha_mode = alpha_mode
            image.update()
            image.pack()
        raise
    # Publish channel edits only after the entire preset transaction succeeds.
    from . import animation
    for image, col, updates in planned:
        for scene in bpy.data.scenes:
            animation.adopt_updates(scene, image, col, updates)
    return {'applied': len(planned), 'params': written, 'skipped': skipped}
