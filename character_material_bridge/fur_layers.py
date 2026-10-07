"""Preserve authored MMD fur shells and recover their per-layer coordinates.

Explicit shell-material mappings select this path. Existing UV1 is validated;
missing layer data is inferred only from source-verified anchors and consistent
connected components. No geometry, weights or shape keys are replaced.
"""
import math
from collections import Counter, defaultdict

API_REVISION = 524


def prebuilt_profiles(obj, choices, entries, profiles):
    """Recognize prebuilt shell slots before material separation drops siblings."""
    counts = Counter(p.material_index for p in obj.data.polygons)
    result = set()
    for profile in profiles:
        for slot, count in counts.items():
            entry = entries.get(choices.get((obj.name, slot)), {})
            if (entry.get('carrier') == profile['carrier'] and entry.get('slot') == profile['shell_slot']
                    and count > profile['base_faces']):
                result.add(profile['id'])
    return result


def _layer_ids(mesh, values, profile):
    import numpy as np
    from . import uv_transfer as uv
    if not np.isfinite(values).all():
        return None
    labels = np.full(len(mesh.loops), -1, np.int32)
    for index, code in enumerate(profile['layers'][1:], 1):
        mask = np.max(np.abs(values - code), axis=1) <= 1e-6
        if np.any(labels[mask] >= 0):
            return None
        labels[mask] = index
    starts = uv._values(mesh.polygons, 'loop_start', 1, 'int32').ravel()
    sizes = uv._values(mesh.polygons, 'loop_total', 1, 'int32').ravel()
    if np.any(labels < 0) or not np.array_equal(np.repeat(labels[starts], sizes), labels):
        return None
    return labels[starts]


def _validate_stack(mesh, values, profile):
    """Every retained layer must contain the same target patch, including edits."""
    from . import fur, uv_transfer as uv
    labels = _layer_ids(mesh, values, profile)
    if labels is None or set(labels) != set(range(1, len(profile['layers']))):
        raise RuntimeError('已有外壳的 UV1 未覆盖配方中的全部非零层，或单个面的层号不一致')
    primary = uv.primary_uv(mesh)
    if primary is None:
        raise RuntimeError('已有毛绒缺少渲染主 UV，无法验证层结构')
    signatures = defaultdict(Counter)
    for polygon in mesh.polygons:
        signatures[int(labels[polygon.index])][fur.uv_digest(mesh, polygon, primary)] += 1
    first = next(iter(signatures.values()))
    if not all(row == first for row in signatures.values()):
        raise RuntimeError('已有外壳各层的 UV 区域不一致，未生成或删除任何层')
    return {'existing_shell_layers': len(signatures), 'faces_per_layer': sum(first.values())}


def _infer(source, target, profile):
    import numpy as np
    from . import uv_transfer as uv
    if not len(source.polygons) or not source.uv_layers.get('UV1') or uv.primary_uv(source) is None:
        raise RuntimeError('目标缺少有效毛绒 UV1，包内也没有源 UV 参考网格；请重新导出包含源 UV 的材质包')
    if uv.primary_uv(target) is None:
        raise RuntimeError('目标缺少渲染主 UV，无法恢复毛绒层号')
    sp, su, _, sf = uv._mesh_data(source, uv.primary_uv(source), profile['shell_slot'])
    tp, tu, ti, tf = uv._mesh_data(target, uv.primary_uv(target))
    codes = uv._values(source.uv_layers['UV1'].data, 'uv', 2)
    if not sf or not tf or not all(np.isfinite(a).all() for a in (sp, su, tp, tu, codes)):
        raise RuntimeError('毛绒参考或目标数据为空/含无效数值')
    # Repeated UV shapes are expected across layers. Cache only UV candidates;
    # each actual target face still gets its own position and topology checks.
    step = uv.UV_EPS * 4
    buckets = defaultdict(list)
    for face_id, loops in sf:
        center = su[list(loops)].mean(axis=0)
        buckets[(len(loops), math.floor(center[0]/step), math.floor(center[1]/step))].append((face_id, loops))
    candidates, cache = [], {}
    seeds, seed_source = defaultdict(list), {}
    for _, face in tf:
        values = tu[list(face)]; key = values.tobytes()
        if key not in cache:
            center = values.mean(axis=0)
            cx, cy = math.floor(center[0]/step), math.floor(center[1]/step)
            nearby = [p for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                      for p in buckets.get((len(face), cx+dx, cy+dy), ())]
            row = []
            if len(nearby) <= uv.MAX_CANDIDATES:
                for face_id, loops in nearby:
                    for offset in range(len(loops)):
                        order = loops[offset:] + loops[:offset]
                        if np.max(np.abs(su[list(order)] - values)) <= uv.UV_EPS:
                            row.append((face_id, order))
            cache[key] = row
        row = cache[key]; candidates.append(row)
        if row:
            group = tuple(sorted({c[0] for c in row}))
            seeds[group].append(tp[list(face)].mean(axis=0))
            if group not in seed_source:
                seed_source[group] = np.asarray([sp[list(c[1])].mean(axis=0) for c in row]).mean(axis=0)
    tolerance = max(float(np.linalg.norm(np.ptp(tp, axis=0))) * 1e-6, 5e-7)
    fit = uv._fit([seed_source[k] for k in seeds],
                  [np.asarray(v).mean(axis=0) for v in seeds.values()], tolerance)
    if fit is None:
        raise RuntimeError('没有足够的毛绒 UV 锚点验证源与目标空间关系')
    matrix, shift = fit; aligned = sp @ matrix.T + shift
    labels = []
    for (_, face), row in zip(tf, candidates):
        valid = [c for c in row if np.max(np.linalg.norm(aligned[list(c[1])] - tp[list(face)], axis=1)) <= tolerance]
        labels.append({tuple(float(v) for v in codes[c[1][0]]) for c in valid})
    # Virtual connectivity crosses exact coincident MMD vertex splits (UV or
    # normal seams). It does not weld or edit the real mesh. Layers touching at
    # a point with conflicting verified labels are rejected, not guessed.
    parent = list(range(len(target.vertices)))
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    for _, face in tf:
        ids = ti[list(face)]
        for value in ids[1:]:
            parent[find(int(value))] = find(int(ids[0]))
    positions = {}
    for vertex in target.vertices:
        key = tuple(vertex.co)
        if key in positions:
            parent[find(vertex.index)] = find(positions[key])
        else:
            positions[key] = vertex.index
    components = defaultdict(list)
    for index, (_, face) in enumerate(tf):
        components[find(int(ti[face[0]]))].append(index)
    values = np.empty((len(target.loops), 2), np.float32)
    unknown = conflicting = anchors = 0
    for indices in components.values():
        options = set().union(*(labels[i] for i in indices))
        if not options:
            unknown += 1; continue
        if len(options) != 1:
            conflicting += 1; continue
        code = next(iter(options))
        anchors += sum(bool(labels[i]) for i in indices)
        for index in indices:
            values[list(tf[index][1])] = code
    if unknown or conflicting:
        raise RuntimeError('毛绒层号无法唯一恢复：%d 个连通区域缺少锚点，%d 个区域层号冲突' % (unknown, conflicting))
    return values, {'method': 'REFERENCE_AND_TOPOLOGY', 'components': len(components),
                    'anchor_faces': anchors, 'position_tolerance': tolerance}


def reuse(obj, carrier, profile):
    import numpy as np
    from . import fur, uv_transfer as uv
    mesh = obj.data
    layer = mesh.uv_layers.get('UV1')
    values = uv._values(layer.data, 'uv', 2) if layer else None
    if values is not None and _layer_ids(mesh, values, profile) is not None:
        stats = {'method': 'EXISTING_UV1'}
    else:
        values, stats = _infer(carrier.data, mesh, profile)
    stats.update(_validate_stack(mesh, values, profile))
    # Source semantics are bound to a managed attribute so an unrelated target
    # UV1 is preserved. This is the same attribute the generated-shell path uses.
    attr = mesh.attributes.get(fur.UV_ATTRIBUTE)
    if attr and (attr.data_type != 'FLOAT_VECTOR' or attr.domain != 'CORNER'):
        raise RuntimeError('毛绒层属性同名冲突：' + fur.UV_ATTRIBUTE)
    if attr is None:
        attr = mesh.attributes.new(fur.UV_ATTRIBUTE, 'FLOAT_VECTOR', 'CORNER')
    vectors = np.zeros((len(mesh.loops), 3), np.float32); vectors[:, :2] = values
    attr.data.foreach_set('vector', vectors.ravel())
    # Restore the missing native UV1 when room permits, while preserving render
    # and editor selection. The managed attribute also works at the UV limit.
    if layer is None and len(mesh.uv_layers) < uv.MAX_LAYERS:
        active = mesh.uv_layers.active.name if mesh.uv_layers.active else ''
        render = uv.primary_uv(mesh); render_name = render.name if render else ''
        new = mesh.uv_layers.new(name='UV1', do_init=False)
        if new:
            new.data.foreach_set('uv', values.ravel())
        if active:mesh.uv_layers.active_index = list(mesh.uv_layers.keys()).index(active)
        if render_name:mesh.uv_layers[render_name].active_render = True
    material = obj.data.materials[profile['shell_slot']]
    fur.adapt_shader(material)
    for modifier in obj.modifiers:
        if modifier.type == 'NODES' and modifier.node_group and modifier.name.startswith('CMB '):
            fur._adapt_vertex(modifier.node_group, {profile['base_slot']: profile['base_slot'],
                                                     profile['shell_slot']: profile['shell_slot']})
    mesh.update(); obj.update_tag(refresh={'DATA'})
    return dict(object=obj.name, source=profile['source_object'], mode='REUSED_EXISTING',
                layers=len(profile['layers']), generated_shell_faces=0,
                preserved_faces=len(mesh.polygons), **stats)
