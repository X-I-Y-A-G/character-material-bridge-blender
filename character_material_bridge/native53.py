"""Independent Blender 5.3 uniform transport for transferred numeric node inputs.

The material owns its values and its copied shader graph. No Importer runtime,
generated shader code, temporary material twin or parameter texture is required.
"""
from array import array
from collections import defaultdict
import json
import math
import re

import bpy

API_REVISION = 600
WIDTHS = {'VALUE': 1, 'INT': 1, 'BOOLEAN': 1, 'VECTOR': 3, 'RGBA': 4}
BULK_TYPES = frozenset({'VALUE', 'VECTOR', 'RGBA'})
MARKER = 'cmb_native53'
DEFAULTS = 'cmb_native53_defaults'
_cache = {}
STATS = defaultdict(int)


def invalidate():
    _cache.clear()


def _groups(material):
    tree = material.node_tree if material else None
    if tree is None:
        return ()
    return tuple(n for n in tree.nodes if n.type == 'GROUP' and n.node_tree)


def is_native(material):
    if material is None or material.node_tree is None:
        return False
    if material.get(MARKER):
        return True
    # Packed-table materials keep their existing transport and schema.
    from . import core
    if core.mat_param_image(material) is not None:
        return False
    return bool(core.material_uber_part(material) and any(
        n.node_tree.name.startswith(('Ruri ', 'CMB ')) and
        any(s.name.startswith('_') and s.type in WIDTHS for s in n.inputs)
        for n in _groups(material)))


def _bindings(material):
    """Cache addresses, never values; undo/load and structural edits invalidate it."""
    key = material.session_uid
    groups = _groups(material)
    signature = (material.node_tree.session_uid,
                 tuple((n.as_pointer(), n.node_tree.session_uid, len(n.inputs)) for n in groups))
    cached = _cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    entries = {}
    vector_names = {socket.name for node in groups for socket in node.inputs
                    if socket.type in {'VECTOR', 'RGBA'}}
    for node in groups:
        for index, socket in enumerate(node.inputs):
            name = socket.name
            if (socket.type not in WIDTHS or not re.fullmatch(r'_[A-Za-z0-9_]+', name)
                    or name.startswith(('_RuriRigBasis', '_RuriMaterialBasis', '__'))):
                continue
            animation = node.id_data.animation_data
            if socket.is_linked or (animation and any(
                    curve.data_path == socket.path_from_id('default_value') for curve in animation.drivers)):
                continue
            alpha_only = name.endswith('_w') and name[:-2] in vector_names
            if alpha_only:
                if node.inputs.get(name[:-2]) is not None:continue
                name = name[:-2]
            width = WIDTHS[socket.type]
            alpha = node.inputs.get(name + '_w') if width == 3 else None
            kind = 'V4' if alpha_only or width == 4 or alpha is not None else 'V3' if width == 3 else 'F'
            entry = entries.setdefault(name, {'kind': kind, 'targets': []})
            if entry['kind'] != kind:
                if {entry['kind'], kind} <= {'V3', 'V4'}:entry['kind'] = 'V4'
                else:raise RuntimeError('同名参数的节点输入类型不一致: ' + name)
            targets = [(index, (3,) if alpha_only else tuple(range(width)))]
            if alpha is not None:
                targets.append((list(node.inputs).index(alpha), (3,)))
            entry['targets'].append((node, targets))
    _cache[key] = (signature, entries)
    STATS['binding_builds'] += 1
    return entries


def names(material):
    return list(_bindings(material))


def schema(material):
    return {name: entry['kind'] for name, entry in _bindings(material).items()}


def kind(material, name):
    return _bindings(material)[name]['kind']


def read(material, name):
    entry = _bindings(material).get(name)
    if entry is None:
        return None
    result = [0.0] * (4 if entry['kind'] == 'V4' else 3 if entry['kind'] == 'V3' else 1)
    filled = set()
    for node, targets in entry['targets']:
        for index, lanes in targets:
            value = node.inputs[index].default_value
            values = tuple(value) if hasattr(value, '__len__') else (value,)
            for lane, item in zip(lanes, values):
                if lane not in filled:
                    result[lane] = float(item);filled.add(lane)
    return result[0] if entry['kind'] == 'F' else result


def values(material):
    return {name: read(material, name) for name in names(material)}


def default_value(material, name):
    try:
        saved = json.loads(material.get(DEFAULTS, '{}'))
    except (TypeError, ValueError):
        saved = {}
    if name in saved:
        return saved[name]
    from . import core
    declared = core.default_value(core.material_uber_part(material), name)
    return declared if declared is not None else read(material, name)


def capture(source, destination):
    """Copy only the CMB schema/default snapshot; never the Importer's ownership."""
    if not is_native(source):
        return
    destination[MARKER] = 1
    destination[DEFAULTS] = source.get(DEFAULTS) or json.dumps(values(source), separators=(',', ':'))
    from . import core
    part = core.material_uber_part(source)
    if part:
        destination['ruri_uber_part'] = part
    from . import environment53
    scene = getattr(bpy.context, 'scene', None)
    if scene is not None:environment53.capture(source, destination, scene)


def prepare(material, updates):
    """Validate the entire edit before touching RNA, including linked/driven inputs."""
    if material.get('ruri_uber_stack'):
        raise RuntimeError('源材质由 RuriRipper 管理，请先移植或在 Ruri 面板调参')
    bindings = _bindings(material)
    result = {'written': 0, 'unknown': [], 'changed': False}
    edits = defaultdict(dict)
    for _part, name, value in updates:
        entry = bindings.get(name)
        if entry is None:
            result['unknown'].append(name)
            continue
        size = 4 if entry['kind'] == 'V4' else 3 if entry['kind'] == 'V3' else 1
        try:
            components = [float(value)] if size == 1 else [float(v) for v in value]
            if len(components) != size or any(not math.isfinite(v) or abs(v) > 3.4e38 for v in components):
                raise ValueError('分量数量或数值无效')
            components = array('f', components)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError('原生参数值无效: ' + name) from exc
        for node, targets in entry['targets']:
            for index, lanes in targets:
                socket = node.inputs[index]
                expected_name = name + '_w' if lanes == (3,) else name
                if socket.name != expected_name or WIDTHS.get(socket.type) != len(lanes):
                    invalidate()
                    raise RuntimeError('参数节点结构已改变，请刷新参数列表: ' + name)
                animation = node.id_data.animation_data
                path = socket.path_from_id('default_value')
                if socket.is_linked or (animation and any(fc.data_path == path for fc in animation.drivers)):
                    raise RuntimeError('参数输入已有连线或驱动，未覆盖: ' + name)
                edits[node][index] = tuple(components[lane] for lane in lanes)
        result['written'] += 1
    plans = []
    for node, changes in edits.items():
        if all(socket.type in BULK_TYPES for socket in node.inputs):
            size = sum(WIDTHS[s.type] for s in node.inputs)
            before = array('f', [0.0]) * size
            node.inputs.foreach_get('default_value', before)
            after = array('f', before)
            offset = 0
            for index, socket in enumerate(node.inputs):
                width = WIDTHS[socket.type]
                if index in changes:
                    after[offset:offset+width] = array('f', changes[index])
                offset += width
            if before != after:
                plans.append((node, before, after, True))
        else:
            # A group may also have image/object sockets. The batch API cannot
            # flatten those; write only changed numeric sockets in that case.
            for index, components in changes.items():
                socket = node.inputs[index]
                old = socket.default_value
                before = tuple(old) if hasattr(old, '__len__') else float(old)
                after = components if len(components) > 1 else components[0]
                if before != after:
                    plans.append((socket, before, after, False))
    result['changed'] = bool(plans)
    return result, plans


def write(material, updates):
    result, plans = prepare(material, updates)
    applied = []
    try:
        for target, before, after, bulk in plans:
            applied.append((target, before, bulk))
            if bulk:
                target.inputs.foreach_set('default_value', after)
                STATS['node_batches'] += 1
            else:
                target.default_value = after
                STATS['socket_fallback_writes'] += 1
        if plans:
            material.node_tree.update_tag()
            STATS['material_updates'] += 1
    except Exception:
        for target, before, bulk in reversed(applied):
            if bulk:
                target.inputs.foreach_set('default_value', before)
            else:
                target.default_value = before
        raise
    return result


def has_native_lighting(material):
    from . import core
    return any(n.bl_idname == 'ShaderNodeLightAccumulation'
               for tree in core.walk_trees(material.node_tree if material else None) for n in tree.nodes)


def requires_light_table(material):
    from . import core, lighting
    if any(lighting.is_light_table(image) for tree in core.walk_trees(material.node_tree if material else None)
           for node in tree.nodes for _, _, image in lighting.image_refs(node)):
        return True
    return not (is_native(material) or has_native_lighting(material))
