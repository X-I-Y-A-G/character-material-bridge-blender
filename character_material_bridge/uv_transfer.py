"""Source UV payloads and conservative corner correspondence. No shader edits.

References are package data, never replacement character geometry. Only new UV
layers are written to recipients; existing layers and their active flags survive.
"""
import hashlib
import json
import math
import re
import struct
from collections import Counter, defaultdict, deque

import bpy

API_REVISION = 524
VERSION = 1
KEY = 'cmb_source_uv_payload_version'
UV_EPS = 3e-6
MAX_CANDIDATES = 256
MAX_LAYERS = 8
RESERVED = ('CMB_', 'ruri_', '.')


def primary_uv(mesh, recorded=''):
    if recorded:
        return mesh.uv_layers.get(recorded)
    return next((u for u in mesh.uv_layers if u.active_render), None)


def _values(collection, field, width, dtype='float32'):
    import numpy as np
    values = np.empty(len(collection) * width, dtype=dtype)
    collection.foreach_get(field, values)
    return values.reshape(-1, width)


def signature(mesh, primary):
    """Hash geometry, slot membership, layer names and every stored float bit."""
    import numpy as np
    digest = hashlib.sha256()
    for collection, field, width, dtype in (
            (mesh.vertices, 'co', 3, 'float32'),
            (mesh.edges, 'vertices', 2, 'int32'),
            (mesh.loops, 'vertex_index', 1, 'int32'),
            (mesh.loops, 'edge_index', 1, 'int32'),
            (mesh.polygons, 'loop_start', 1, 'int32'),
            (mesh.polygons, 'loop_total', 1, 'int32'),
            (mesh.polygons, 'material_index', 1, 'int32')):
        data = _values(collection, field, width, dtype)
        if not np.isfinite(data).all():
            raise RuntimeError('源 UV 参考网格包含无效数值')
        digest.update(struct.pack('<Q', len(collection)))
        digest.update(data.astype('<f4' if dtype == 'float32' else '<i4').tobytes())
    digest.update(json.dumps([primary, list(mesh.uv_layers.keys())], ensure_ascii=False).encode('utf8'))
    for uv in mesh.uv_layers:
        data = _values(uv.data, 'uv', 2)
        if not np.isfinite(data).all():
            raise RuntimeError('源 UV 层包含无效数值: ' + uv.name)
        digest.update(data.astype('<f4').tobytes())
    return digest.hexdigest()


def make_reference(source, carrier):
    from . import mesh_state
    materials = list(carrier.data.materials)
    slots = _values(source.data.polygons, 'material_index', 1, 'int32')
    mesh_state.make_reference(source, carrier)
    for key in (mesh_state.KEY, mesh_state.SIGNATURE, *mesh_state.META_KEYS):
        if key in carrier:
            del carrier[key]
    mesh = carrier.data
    mesh.name = 'CMB_SOURCE_UV_' + carrier.name
    uv_names = set(mesh.uv_layers.keys())
    for attr in list(mesh.attributes):
        if attr.name not in uv_names and attr.name != 'material_index' and not attr.is_required:
            mesh.attributes.remove(attr)
    for material in materials:
        mesh.materials.append(material)
    # Mesh.materials.clear() resets every polygon to slot zero. Restore the
    # original regions after stripping/rebinding the reference's material IDs.
    mesh.polygons.foreach_set('material_index', slots.ravel())
    uv = primary_uv(mesh)
    primary = uv.name if uv else ''
    carrier[KEY] = VERSION
    return {'version': VERSION, 'carrier': carrier.name, 'primary_uv': primary,
            'layers': list(mesh.uv_layers.keys()), 'signature': signature(mesh, primary),
            'vertices': len(mesh.vertices), 'corners': len(mesh.loops), 'faces': len(mesh.polygons)}


def validate_manifest(manifest):
    version = manifest.get('source_uv_payload_version')
    refs = manifest.get('source_uv_references')
    if version is None:
        if refs is not None:
            raise RuntimeError('源 UV 数据缺少版本标记')
        return
    if type(version) is not int or version != VERSION or manifest.get('package_kind') == 'final':
        raise RuntimeError('不支持的源 UV 数据版本或包类型')
    if not isinstance(refs, dict) or not refs:
        raise RuntimeError('源 UV 参考记录缺失')
    for name, info in refs.items():
        if (not isinstance(name, str) or not name or not isinstance(info, dict)
                or type(info.get('version')) is not int or info['version'] != VERSION
                or info.get('carrier') != name or not isinstance(info.get('primary_uv'), str)
                or not isinstance(info.get('signature'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', info['signature'])):
            raise RuntimeError('源 UV 参考记录无效: ' + str(name))
        layers = info.get('layers')
        if (not isinstance(layers, list)
                or not all(isinstance(n, str) and n for n in layers) or len(set(layers)) != len(layers)
                or (info['primary_uv'] and info['primary_uv'] not in layers)):
            raise RuntimeError('源 UV 层记录无效: ' + name)
        if any(type(info.get(k)) is not int or info[k] < 0 for k in ('vertices', 'corners', 'faces')):
            raise RuntimeError('源 UV 网格数量记录无效: ' + name)
    if any(e.get('carrier') not in refs for e in manifest.get('entries', []) if isinstance(e, dict)):
        raise RuntimeError('材质条目缺少对应的源 UV 参考记录')


def validate_reference(carrier, info):
    if carrier.type != 'MESH' or carrier.get(KEY) != VERSION:
        raise RuntimeError('源 UV 参考载体标记无效: ' + carrier.name)
    mesh = carrier.data
    uv = primary_uv(mesh)
    if (mesh.shape_keys or carrier.vertex_groups or any(v.groups for v in mesh.vertices)
            or len(mesh.vertices) != info['vertices'] or len(mesh.loops) != info['corners']
            or len(mesh.polygons) != info['faces'] or list(mesh.uv_layers.keys()) != info['layers']
            or (uv.name if uv else '') != info['primary_uv']
            or signature(mesh, info['primary_uv']) != info['signature']):
        raise RuntimeError('源 UV 参考数据校验失败: ' + carrier.name)
    return mesh


def _mesh_data(mesh, uv, slot=None):
    vertices = _values(mesh.vertices, 'co', 3, 'float64')
    indices = _values(mesh.loops, 'vertex_index', 1, 'int32').ravel()
    return (vertices[indices], _values(uv.data, 'uv', 2, 'float64'), indices,
            [(p.index, tuple(p.loop_indices)) for p in mesh.polygons
             if slot is None or p.material_index == slot])


def _fit(source, target, tolerance):
    """Proper similarity transform, with a bounded trim of outlying seed faces."""
    import numpy as np
    a, b = np.asarray(source), np.asarray(target)
    if len(a) < 3:
        return None
    keep = np.ones(len(a), dtype=bool)
    result = None
    for _ in range(4):
        x, y = a[keep], b[keep]
        if len(x) < 3:
            break
        ac, bc = x.mean(axis=0), y.mean(axis=0)
        u, singular, vh = np.linalg.svd((x-ac).T @ (y-bc))
        if singular[1] <= max(singular[0] * 1e-10, 1e-20):
            return None
        correction = np.ones(3)
        if np.linalg.det(vh.T @ u.T) < 0:
            correction[-1] = -1
        rotation = vh.T @ np.diag(correction) @ u.T
        scale = float((singular * correction).sum() / np.square(x-ac).sum())
        if not math.isfinite(scale) or scale <= 1e-12:
            return None
        matrix = scale * rotation
        shift = bc - ac @ matrix.T
        result = matrix, shift
        distance = np.linalg.norm(a @ matrix.T + shift - b, axis=1)
        next_keep = distance <= max(tolerance * 4, float(np.median(distance)) * 2)
        if np.array_equal(keep, next_keep):
            break
        keep = next_keep
    return result


def _injective(candidates):
    """Check for a complete face assignment without choosing ambiguous UV values."""
    assignment, owners = {}, {}
    choices = [sorted({c[0] for c in row}) for row in candidates]
    for start in sorted(range(len(choices)), key=lambda i: len(choices[i])):
        parents = {start: None}
        queue = deque([start])
        found = None
        while queue and found is None:
            face = queue.popleft()
            for source in choices[face]:
                owner = owners.get(source)
                if owner is None:
                    found = face, source
                    break
                if owner not in parents:
                    parents[owner] = face
                    queue.append(owner)
        if found is None:
            return False
        face, source = found
        while face is not None:
            old = assignment.get(face)
            assignment[face] = source
            owners[source] = face
            face, source = parents[face], old
    return True


def correspondence(source, target, source_uv, slot, fur_layer_hint=False):
    import numpy as np
    target_uv = primary_uv(target)
    if source_uv is None or target_uv is None:
        return None, {'reason': '缺少可用的渲染主 UV'}
    sp, su, si, sf = _mesh_data(source, source_uv, slot)
    tp, tu, ti, tf = _mesh_data(target, target_uv)
    source_codes = target_codes = None
    if fur_layer_hint:
        layer = source.uv_layers.get('UV1')
        attr = target.attributes.get('CMB_FurUV1')
        if layer and attr and attr.data_type == 'FLOAT_VECTOR' and attr.domain == 'CORNER':
            source_codes = _values(layer.data, 'uv', 2)
            target_codes = _values(attr.data, 'vector', 3)[:, :2]
    if not sf or not tf:
        return None, {'reason': '源材质区域或目标没有网格面'}
    if not all(np.isfinite(a).all() for a in (sp, su, tp, tu)):
        return None, {'reason': '匹配数据含非有限数值'}
    # Centroid cells plus adjacent cells avoid UV quantization-boundary misses.
    step = UV_EPS * 4
    buckets = defaultdict(list)
    for face_id, loops in sf:
        center = su[list(loops)].mean(axis=0)
        buckets[(len(loops), math.floor(center[0]/step), math.floor(center[1]/step))].append((face_id, loops))
    candidates, seed_s, seed_t = [], [], []
    for _, face in tf:
        uv = tu[list(face)]
        center = uv.mean(axis=0)
        cx, cy = math.floor(center[0]/step), math.floor(center[1]/step)
        nearby = [p for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                  for p in buckets.get((len(face), cx+dx, cy+dy), ())]
        if len(nearby) > MAX_CANDIDATES:
            return None, {'reason': '重叠主 UV 候选过多，未尝试猜测对应关系'}
        row = []
        for face_id, loops in nearby:
            # The fur subsystem already verified a constant layer code per
            # face. Use it only as an extra constraint, never relax UV/position
            # or injectivity checks, and avoid comparing all 19 identical UVs.
            if source_codes is not None and (abs(float(source_codes[loops[0], 0])-float(target_codes[face[0], 0])) > 1e-6
                    or abs(float(source_codes[loops[0], 1])-float(target_codes[face[0], 1])) > 1e-6):
                continue
            for offset in range(len(loops)):
                order = loops[offset:] + loops[:offset]
                if np.max(np.abs(su[list(order)] - uv)) <= UV_EPS:
                    row.append((face_id, order))
        if len(row) == 1:
            seed_s.extend(sp[list(row[0][1])])
            seed_t.extend(tp[list(face)])
        candidates.append(row)
    extent = float(np.linalg.norm(np.ptp(tp, axis=0)))
    tolerance = max(extent * 1e-4, 1e-6)
    fit = _fit(seed_s, seed_t, tolerance)
    if fit is not None:
        matrix, shift = fit
        aligned = sp @ matrix.T + shift
        for (_, face), row in zip(tf, candidates):
            row[:] = [c for c in row if np.max(np.linalg.norm(aligned[list(c[1])]-tp[list(face)], axis=1)) <= tolerance]
    else:
        return None, {'reason': '没有足够的唯一主 UV 锚点验证空间对应关系'}
    before = Counter(len(c) for c in candidates)
    if not all(candidates):
        return None, {'reason': '主 UV 或位置无法完整匹配', 'faces': len(tf),
                      'unmatched_faces': before[0], 'position_tolerance': tolerance}
    # Anchors can propagate across MMD vertex splits. If source vertices were
    # merged instead, do not impose a false one-source-vertex constraint.
    source_vertices = {int(si[loop]) for row in candidates for c in row for loop in c[1]}
    use_topology = len(source_vertices) <= len(target.vertices)
    anchors = defaultdict(set)
    for (_, face), row in zip(tf, candidates):
        if len(row) == 1:
            for sl, tl in zip(row[0][1], face):
                anchors[int(ti[tl])].add(int(si[sl]))
    if any(len(v) > 1 for v in anchors.values()):
        use_topology = False
    for _ in range(32):
        changes = 0
        fixed = Counter(row[0][0] for row in candidates if len(row) == 1)
        if any(count > 1 for count in fixed.values()):
            return None, {'reason': '多个目标面指向同一源面，未复制 UV'}
        for (_, face), row in zip(tf, candidates):
            if len(row) <= 1:
                continue
            valid = [c for c in row if c[0] not in fixed]
            if use_topology:
                anchored = [c for c in valid if all(not anchors.get(int(ti[tl]))
                            or int(si[sl]) in anchors[int(ti[tl])] for sl, tl in zip(c[1], face))]
                # Empty topology evidence can indicate a split/merge boundary;
                # keep geometric candidates instead of forcing a correspondence.
                if anchored:
                    valid = anchored
            if len(valid) < len(row):
                row[:] = valid
                changes += 1
            if len(row) == 1:
                fixed[row[0][0]] += 1
                for sl, tl in zip(row[0][1], face):
                    anchors[int(ti[tl])].add(int(si[sl]))
        if not changes:
            break
    if not all(candidates) or not _injective(candidates):
        return None, {'reason': '无法建立不重复使用源面的完整对应关系'}
    return (tf, candidates), {'faces': len(tf), 'corners': len(target.loops),
                             'unique_faces': sum(len(c) == 1 for c in candidates),
                             'ambiguous_faces': sum(len(c) > 1 for c in candidates),
                             'topology_anchors': use_topology, 'position_tolerance': tolerance}


def _write_layer(mesh, name, values, primary_name):
    import numpy as np
    old = mesh.uv_layers.get(name)
    if old and np.array_equal(_values(old.data, 'uv', 2), values):
        return {'source_layer': name, 'target_layer': old.name, 'status': 'REUSED'}
    conflict = old is not None or name == primary_name or name.startswith(RESERVED) or mesh.attributes.get(name) is not None
    destination = 'CMB_Source_' + name if conflict else name
    # Reuse an identical prior conflict copy; never overwrite it on a re-import.
    for layer in mesh.uv_layers:
        if layer.name == destination and np.array_equal(_values(layer.data, 'uv', 2), values):
            return {'source_layer': name, 'target_layer': layer.name, 'status': 'REUSED'}
    if len(mesh.uv_layers) >= MAX_LAYERS:
        return {'source_layer': name, 'status': 'SKIPPED', 'reason': '已达到 8 层 UV 上限'}
    layer = mesh.uv_layers.new(name=destination, do_init=False)
    if layer is None:
        return {'source_layer': name, 'status': 'SKIPPED', 'reason': 'Blender 无法创建 UV 层'}
    layer.data.foreach_set('uv', values.ravel())
    return {'source_layer': name, 'target_layer': layer.name,
            'status': 'CONFLICT_COPY' if conflict else 'COPIED'}


def transfer(carrier, target, info, slot, fur_layer_hint=False):
    import numpy as np
    mesh = target.data
    source = carrier.data
    report = {'object': target.name, 'source': info['carrier'], 'layers': []}
    if not info['layers']:
        report['matching'] = {'reason': '源网格没有 UV 层'}
        return report
    mapping, stats = correspondence(source, mesh, primary_uv(source, info['primary_uv']), slot, fur_layer_hint)
    report['matching'] = stats
    if mapping is None:
        report['layers'] = [{'source_layer': name, 'status': 'SKIPPED', 'reason': stats['reason']}
                            for name in info['layers']]
        return report
    faces, candidates = mapping
    planned = []
    for name in info['layers']:
        data = _values(source.uv_layers[name].data, 'uv', 2)
        values = np.empty((len(mesh.loops), 2), np.float32)
        conflicts = 0
        for (_, face), row in zip(faces, candidates):
            selected = data[list(row[0][1])]
            if any(not np.array_equal(selected, data[list(c[1])]) for c in row[1:]):
                conflicts += 1
            values[list(face)] = selected
        if conflicts:
            report['layers'].append({'source_layer': name, 'status': 'SKIPPED',
                                     'reason': '重叠候选的本层数值不一致', 'ambiguous_faces': conflicts})
        else:
            planned.append((name, values))
    # Missing auxiliary layers get the available slots before conflict copies.
    planned.sort(key=lambda row: (row[0] == info['primary_uv'],
                                 mesh.attributes.get(row[0]) is not None or row[0].startswith(RESERVED)))
    active = mesh.uv_layers.active.name if mesh.uv_layers.active else ''
    render = primary_uv(mesh)
    render_name = render.name if render else ''
    if planned and mesh.users > 1:
        target.data = mesh.copy()
        mesh = target.data
    try:
        for name, values in planned:
            report['layers'].append(_write_layer(mesh, name, values, render_name))
    finally:
        if active and mesh.uv_layers.get(active):
            mesh.uv_layers.active_index = list(mesh.uv_layers.keys()).index(active)
        if render_name and mesh.uv_layers.get(render_name):
            mesh.uv_layers[render_name].active_render = True
    mesh.update()
    return report
