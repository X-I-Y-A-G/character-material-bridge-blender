"""Target-authored outline normals for the Ruri XY-hemisphere decoder.

Never copy a source UV2 by vertex index. No source geometry is required. Sum
angle-weighted geometric face normals at coincident compatible vertices.
Geometry, shading normals and existing UV layers are not welded or rewritten.
"""
import math
from array import array
from collections import defaultdict

import bpy
from mathutils import Matrix, Vector

ATTRIBUTE = 'CMB_OutlineUV2'
API_REVISION = 523
DOMAIN_NODE = 'CMB_OutlineCornerDecode'
ENCODING = 'tangent_xy_positive_z_v1'
GENERATOR = 'position_face_angle_v2'


def width_controls(obj, localize=False):
    """Read actual GN widths; never create data from a panel draw callback."""
    from . import runtime, core
    if obj is None or obj.type != 'MESH' or core.role_of(obj) != 'result':
        return []
    if localize:
        nodes = runtime.receivers(obj)
    else:
        trees = {t for mod in obj.modifiers if mod.type == 'NODES' and mod.node_group
                 and mod.name.startswith('CMB ') for t in core.walk_trees(mod.node_group)}
        nodes = [n for tree in trees for n in tree.nodes if runtime._receiver(n)]
    return [(n, n.inputs['_OutlineWidth']) for n in sorted(nodes, key=lambda n: (n.id_data.name, n.name))
            if n.inputs.get('_OutlineWidth') is not None]


def adjust_width(objects, mode='MULTIPLY', width=0.5, factor=1.0):
    """Explicit, undoable operator edit. Values persist in the native GN stack.

    No temporary parameter row, animation channel, scene polling or global
    default is involved. Final packages already preserve these GN inputs.
    """
    from . import animation
    if mode not in {'SET', 'MULTIPLY'} or not all(math.isfinite(v) and 0 <= v <= 20 for v in (width, factor)):
        raise RuntimeError('描边宽度和倍率必须是 0 到 20 之间的有限数值')
    eligible = [o for o in dict.fromkeys(objects) if width_controls(o)]
    report = {'objects': [], 'changes': [], 'warnings': []}
    for obj in eligible:
        controls = width_controls(obj, localize=True)
        changed = False
        for node, socket in controls:
            path = socket.path_from_id('default_value')
            if socket.is_linked or any(c.data_path == path for c in animation.action_curves(socket.id_data)):
                report['warnings'].append(obj.name + ' / ' + node.name + '：宽度受连线或动画控制，已跳过')
                continue
            before = socket.default_value
            value = width if mode == 'SET' else before * factor
            if not math.isfinite(value) or value < 0:
                report['warnings'].append(obj.name + '：原宽度无效，已跳过')
                continue
            if abs(before - value) <= 1e-8:
                continue
            socket.default_value = value
            socket.id_data.update_tag()
            report['changes'].append({'object': obj.name, 'node': node.name,
                                      'before': before, 'after': socket.default_value})
            changed = True
        if changed:
            obj.update_tag(refresh={'DATA'})
            report['objects'].append(obj.name)
    return report


def _uv_reader(tree):
    readers = [n for n in tree.nodes if n.bl_idname == 'GeometryNodeInputNamedAttribute'
               and n.inputs.get('Name') and not n.inputs['Name'].is_linked
               and n.inputs['Name'].default_value in ('UV2', ATTRIBUTE)]
    if len(readers) != 1:
        return None
    # Recognize the decoder contract, not every UV2 reader in a model.
    names = {n.inputs['Name'].default_value for n in tree.nodes
             if n.bl_idname == 'GeometryNodeInputNamedAttribute'
             and n.inputs.get('Name') and not n.inputs['Name'].is_linked}
    inputs = {s.name for n in tree.nodes if n.type == 'GROUP_INPUT' for s in n.outputs}
    outputs = {s.name for n in tree.nodes if n.type == 'GROUP_OUTPUT' for s in n.inputs}
    if (not {'ruri_tangent', 'ruri_tangent_sign'}.issubset(names)
            or '_OutlineAverageNormal' not in inputs or 'offset' not in outputs
            or not any(n.bl_idname == 'ShaderNodeMath' and n.operation == 'SQRT' for n in tree.nodes)):
        return None
    return readers[0]


def _normal_mix(tree):
    for node in tree.nodes:
        if node.bl_idname != 'ShaderNodeMix' or node.data_type != 'VECTOR':
            continue
        factors = [link.from_node for link in node.inputs[0].links]
        for factor in factors:
            if factor.bl_idname != 'ShaderNodeMath' or factor.operation != 'MULTIPLY':
                continue
            links = [link for socket in factor.inputs for link in socket.links]
            if any(l.from_node.type == 'GROUP_INPUT' and l.from_socket.name == '_OutlineAverageNormal'
                   for l in links) and any(l.from_socket.name == 'Exists' for l in links):
                return node
    return None


def _decoder_contract(tree):
    """Verify connected decoder operations, not just the presence of a SQRT node.

    Unknown/octahedral variants must not receive positive-hemisphere XY data.
    Match topology independently of node names and ignore the camera/width leg.
    """
    uv, mix = _uv_reader(tree), _normal_mix(tree)
    if uv is None or mix is None or not mix.inputs[5].is_linked:
        return False
    def input_expr(node, index, seen):
        socket = node.inputs[index]
        if socket.is_linked:
            return expr(socket.links[0].from_socket, seen)
        v = getattr(socket, 'default_value', None)
        return float(v) if isinstance(v, (int, float)) else None
    def expr(socket, seen=frozenset()):
        node = socket.node
        if node.as_pointer() in seen:
            return None
        seen = seen | {node.as_pointer()}
        kind = node.bl_idname
        if kind == 'GeometryNodeInputNormal':
            return ('N',)
        if kind == 'GeometryNodeInputNamedAttribute':
            if node.inputs['Name'].is_linked:
                return None
            return ('attribute', 'UV' if node == uv else node.inputs['Name'].default_value)
        if kind == 'ShaderNodeSeparateXYZ':
            return (socket.name, input_expr(node, 0, seen))
        if kind == 'ShaderNodeVectorMath':
            indices = {'NORMALIZE': (0,), 'CROSS_PRODUCT': (0, 1), 'DOT_PRODUCT': (0, 1),
                       'ADD': (0, 1), 'SCALE': (0, 3)}.get(node.operation)
        elif kind == 'ShaderNodeMath':
            indices = {'SQRT': (0,), 'MAXIMUM': (0, 1), 'SUBTRACT': (0, 1)}.get(node.operation)
        else:
            return None
        return (node.operation,) + tuple(input_expr(node, i, seen) for i in indices) if indices else None
    n = ('N',); t = ('NORMALIZE', ('attribute', 'ruri_tangent'))
    u = ('attribute', 'UV'); s = ('attribute', 'ruri_tangent_sign')
    b = ('SCALE', ('CROSS_PRODUCT', t, n), s)
    z = ('SQRT', ('MAXIMUM', ('SUBTRACT', 1.0, ('DOT_PRODUCT', u, u)), 0.0))
    expected = ('NORMALIZE', ('ADD', ('ADD', ('SCALE', t, ('X', u)),
                                             ('SCALE', b, ('Y', u))), ('SCALE', n, z)))
    if expr(mix.inputs[5].links[0].from_socket) != expected:
        return False
    if not mix.inputs[4].is_linked or expr(mix.inputs[4].links[0].from_socket) not in (n, ('NORMALIZE', n)):
        return False
    factor = mix.inputs[0].links[0].from_node
    return any(link.from_node == uv and link.from_socket.name == 'Exists'
               for socket in factor.inputs for link in socket.links)


def decoder_trees(obj, verified=True):
    seen, found = set(), []
    def walk(tree):
        if tree is None or tree.as_pointer() in seen:
            return
        seen.add(tree.as_pointer())
        if (_uv_reader(tree) is not None and _normal_mix(tree) is not None
                and (not verified or _decoder_contract(tree))):
            found.append(tree)
        for node in tree.nodes:
            if node.type == 'GROUP':
                walk(node.node_tree)
    for mod in obj.modifiers:
        if mod.type == 'NODES':
            walk(mod.node_group)
    return found


def _pixel_floor_plans(tree):
    """Recognize lerp(raw, sign(raw)*minimum, abs(raw)<minimum), per axis."""
    plans = []
    def source(socket):
        return socket.links[0].from_socket if socket.is_linked else None
    for mix in tree.nodes:
        if mix.bl_idname != 'ShaderNodeMix' or mix.data_type != 'FLOAT':
            continue
        raw, floor, test = (source(mix.inputs[i]) for i in (2, 3, 0))
        if raw is None or floor is None or test is None:
            continue
        multiply, compare = floor.node, test.node
        if (multiply.bl_idname != 'ShaderNodeMath' or multiply.operation != 'MULTIPLY'
                or compare.bl_idname != 'ShaderNodeMath' or compare.operation != 'LESS_THAN'):
            continue
        absolute, minimum = source(compare.inputs[0]), source(compare.inputs[1])
        if (absolute is None or minimum is None or absolute.node.bl_idname != 'ShaderNodeMath'
                or absolute.node.operation != 'ABSOLUTE' or source(absolute.node.inputs[0]) != raw):
            continue
        for index in (0, 1):
            sign = source(multiply.inputs[index])
            if (sign is not None and sign.node.bl_idname == 'ShaderNodeMath'
                    and sign.node.operation == 'SIGN' and source(sign.node.inputs[0]) == raw
                    and source(multiply.inputs[1-index]) == minimum):
                plans.append((sign, absolute, minimum, multiply.inputs[index]))
    return plans


def _stabilize_pixel_floor(tree):
    # Below 0.001 of the minimum pixel displacement, sign is numerical noise.
    # Per-axis SIGN used to turn ±roundoff into opposite full-pixel offsets.
    for i, (sign, absolute, minimum, target) in enumerate(_pixel_floor_plans(tree)):
        epsilon = tree.nodes.new('ShaderNodeMath'); epsilon.operation = 'MULTIPLY'
        epsilon.name = 'CMB_OutlinePixelEpsilon_' + str(i)
        epsilon.inputs[1].default_value = .001
        tree.links.new(minimum, epsilon.inputs[0])
        significant = tree.nodes.new('ShaderNodeMath'); significant.operation = 'GREATER_THAN'
        tree.links.new(absolute, significant.inputs[0]); tree.links.new(epsilon.outputs[0], significant.inputs[1])
        stable = tree.nodes.new('ShaderNodeMath'); stable.operation = 'MULTIPLY'
        stable.name = 'CMB_OutlineStableSign_' + str(i)
        stable.label = 'CMB：抑制零附近正负舍入造成的像素跳变'
        tree.links.new(sign, stable.inputs[0]); tree.links.new(significant.outputs[0], stable.inputs[1])
        tree.links.new(stable.outputs[0], target)


def adapt_nodes(obj):
    """Copy only changed geometry-node paths. Decode per CORNER before interpolation.

    Encoding, T, sign and N must meet on the same corner: averaging these inputs
    independently on POINT is nonlinear and can cancel a mirrored tangent/sign.
    """
    copies = {}
    def adapt(tree):
        if tree is None:
            return tree
        ptr = tree.as_pointer()
        if ptr in copies:
            return copies[ptr]
        copies[ptr] = tree
        reader, mix = _uv_reader(tree), _normal_mix(tree)
        if reader is not None and mix is not None and _decoder_contract(tree):
            if (reader.inputs['Name'].default_value == ATTRIBUTE and tree.nodes.get(DOMAIN_NODE)
                    and not _pixel_floor_plans(tree)):
                return tree
            new = tree.copy()
            copies[ptr] = new
            new.name = tree.name + ' CMB smooth'
            _uv_reader(new).inputs['Name'].default_value = ATTRIBUTE
            if not new.nodes.get(DOMAIN_NODE):
                mix = _normal_mix(new)
                links = list(link for link in new.links if link.from_node == mix)
                if not links:
                    raise RuntimeError('描边平滑法线没有下游连接: ' + tree.name)
                sample = new.nodes.new('GeometryNodeFieldOnDomain')
                sample.name = DOMAIN_NODE
                sample.label = 'CMB：逐面角解码后再平均'
                sample.data_type = 'FLOAT_VECTOR'
                sample.domain = 'CORNER'
                output = links[0].from_socket
                for link in links:
                    target = link.to_socket
                    new.links.remove(link)
                    new.links.new(sample.outputs['Value'], target)
                new.links.new(output, sample.inputs['Value'])
            _stabilize_pixel_floor(new)
            return new
        changes = {}
        for node in tree.nodes:
            if node.type == 'GROUP':
                child = adapt(node.node_tree)
                if child != node.node_tree:
                    changes[node.name] = child
        if not changes:
            return tree
        new = tree.copy()
        copies[ptr] = new
        new.name = tree.name + ' CMB smooth'
        for name, child in changes.items():
            new.nodes[name].node_tree = child
        return new
    changed = 0
    for mod in obj.modifiers:
        if mod.type == 'NODES':
            new = adapt(mod.node_group)
            if new != mod.node_group:
                mod.node_group = new
                changed += 1
    return changed


def _group_key(obj):
    # Never stitch unrelated models, or parts that the user moved relative to one another.
    return (str(obj.get('jmb_original') or obj.name),
            tuple(round(x, 7) for row in obj.matrix_world for x in row))


def smooth_targets(objects):
    """Angle-weight GEOMETRIC faces at coincident, deformation-compatible vertices.

    Source hair mean difference from authored UV2: ~0.73 degrees for position
    face-angle sums, ~27.89 for v0.5.3. Never average custom shading normals a
    second time, or discard a thin folded rim just because its angle is >120°.
    Coincident opposite sheets that cancel retain separate local directions.
    """
    batches = defaultdict(list)
    for obj in objects:
        batches[_group_key(obj)].append(obj)
    result, stats = {}, {'generator': GENERATOR, 'shared_position_groups': 0,
                        'shared_vertices': 0, 'cancellation_groups': 0,
                        'deformation_split_groups': 0, 'degenerate_vertices': 0}
    for batch in batches.values():
        verts = [v.co for o in batch for v in o.data.vertices]
        if not verts:
            continue
        span = max(max(v[i] for v in verts) - min(v[i] for v in verts) for i in range(3))
        tolerance = max(span * 1e-7, 1e-8)
        sums, masses, positions, signatures = {}, {}, defaultdict(list), {}
        def signature(oi, vi):
            key = (oi, vi)
            if key not in signatures:
                obj = batch[oi]; vertex = obj.data.vertices[vi]
                # MMD ordering / outline width are not bone deformation weights.
                weights = tuple(sorted((obj.vertex_groups[g.group].name, round(g.weight, 5))
                                       for g in vertex.groups if g.weight > 1e-6
                                       and obj.vertex_groups[g.group].name not in
                                       {'mmd_vertex_order', 'mmd_edge_scale'}))
                keys = obj.data.shape_keys
                morphs = tuple((k.name, tuple(round(float(x) / tolerance)
                                             for x in (k.data[vi].co - vertex.co)))
                               for k in keys.key_blocks) if keys else ()
                signatures[key] = (weights, morphs)
            return signatures[key]
        for oi, obj in enumerate(batch):
            mesh = obj.data
            for vertex in mesh.vertices:
                key = (oi, vertex.index)
                # Double accumulators avoid normalizing float32 cancellation noise.
                sums[key] = [0.0, 0.0, 0.0]; masses[key] = 0.0
                positions[tuple(round(float(x) / tolerance) for x in vertex.co)].append(key)
            mesh.calc_loop_triangles()
            for poly in mesh.loop_triangles:
                vis = list(poly.vertices)
                for j, vi in enumerate(vis):
                    a = mesh.vertices[vis[j - 1]].co - mesh.vertices[vi].co
                    b = mesh.vertices[vis[(j + 1) % len(vis)]].co - mesh.vertices[vi].co
                    angle = a.angle(b, 0.0) if a.length_squared and b.length_squared else 0.0
                    key = (oi, vi)
                    for axis in range(3):
                        sums[key][axis] += float(poly.normal[axis]) * angle
                    masses[key] += angle
        normals = {}
        for members in positions.values():
            compatible = defaultdict(list)
            for key in members:
                compatible[signature(*key) if len(members) > 1 else ()].append(key)
            if len(compatible) > 1:
                stats['deformation_split_groups'] += 1
            for keys in compatible.values():
                total = Vector(tuple(math.fsum(sums[k][axis] for k in keys) for axis in range(3)))
                mass = math.fsum(masses[k] for k in keys)
                if total.length > max(1e-12, mass * 1e-5):
                    normal = total.normalized()
                    for key in keys:
                        normals[key] = normal
                    if len(keys) > 1:
                        stats['shared_position_groups'] += 1
                        stats['shared_vertices'] += len(keys)
                else:
                    stats['cancellation_groups'] += 1
                    for key in keys:
                        normal = Vector(sums[key])
                        if normal.length <= max(1e-12, masses[key] * 1e-5):
                            normal = Vector((0, 0, 0))
                            stats['degenerate_vertices'] += 1
                        normals[key] = normal.normalized()
        for oi, obj in enumerate(batch):
            result[obj.name] = [normals[(oi, vi)] for vi in range(len(obj.data.vertices))]
    return result, stats


def encode_target(obj, normals):
    """Project the target smooth normal into this target's own Ruri T/B/N basis.

    The verified decoder stores x/y and reconstructs +sqrt(1-x*x-y*y). It is NOT
    octahedral encoding. Back-hemisphere/degenerate cases fall back to N and report.
    """
    mesh = obj.data
    ta = mesh.attributes.get('ruri_tangent'); sa = mesh.attributes.get('ruri_tangent_sign')
    if (ta is None or sa is None or ta.domain != 'CORNER' or sa.domain != 'CORNER'
            or ta.data_type != 'FLOAT_VECTOR' or sa.data_type != 'FLOAT'):
        raise RuntimeError(obj.name + ': 请先重建正确的 CORNER 切线/符号属性对')
    values, fallback, max_error = array('f'), 0, 0.0
    reasons, samples = defaultdict(int), []
    for i, loop in enumerate(mesh.loops):
        n = mesh.corner_normals[i].vector.normalized()
        t = ta.data[i].vector.normalized()
        b = t.cross(n) * sa.data[i].value
        wanted = normals[loop.vertex_index]
        basis = Matrix((t, b, n)).transposed()
        coefficients = (basis.inverted() @ wanted if abs(basis.determinant()) > 1e-5
                        else Vector((0, 0, 0)))
        reason = ('degenerate_smooth_normal' if wanted.length_squared < .5
                  else 'invalid_basis' if n.length_squared < .5 or t.length_squared < .5
                  or abs(abs(sa.data[i].value) - 1) > .01 or coefficients.length_squared < 1e-10
                  or not all(math.isfinite(x) for x in (*n, *t, *coefficients, sa.data[i].value))
                  else 'outside_positive_hemisphere' if coefficients.z < 1e-5 else '')
        if reason:
            x, y = 0.0, 0.0
            fallback += 1
            reasons[reason] += 1
            if len(samples) < 16:
                samples.append({'corner': i, 'vertex': loop.vertex_index, 'reason': reason})
        else:
            # Solve the actual decoder basis, including small nonorthogonality
            # from imported custom normals. Unit coefficients preserve direction
            # because the decoder normalizes the reconstructed vector.
            coefficients.normalize()
            x, y = coefficients.x, coefficients.y
            radius = x*x + y*y
            if radius > 1:
                scale = math.sqrt((1 - 1e-8) / radius); x *= scale; y *= scale
            recovered = (t*x + b*y + n*math.sqrt(max(1-x*x-y*y, 0))).normalized()
            max_error = max(max_error, (recovered-wanted).length)
        values.extend((x, y))
    existing = mesh.attributes.get(ATTRIBUTE)
    if existing and (existing.domain != 'CORNER' or existing.data_type != 'FLOAT2'):
        raise RuntimeError(obj.name + ': 专用描边属性已存在但类型不同，未覆盖')
    return values, {'object': obj.name, 'loops': len(mesh.loops), 'fallback_corners': fallback,
                    'fallback_reasons': dict(reasons), 'fallback_samples': samples,
                    'max_decode_vector_error': max_error, 'encoding': ENCODING}


def rebuild(objects, preserve=()):
    objects = [o for o in objects if o.type == 'MESH' and len(o.data.polygons)]
    preserve = set(preserve)
    eligible = [o for o in objects if o not in preserve and decoder_trees(o)]
    report = {'rebuilt': [], 'skipped': [], 'warnings': []}
    for obj in objects:
        if obj in preserve:
            continue
        if len(decoder_trees(obj, verified=False)) != len(decoder_trees(obj)):
            report['skipped'].append({'object': obj.name,
                                     'reason': '描边解码链不是已核验的 XY 正半球公式，未适配未知节点'})
            if obj in eligible:
                eligible.remove(obj)
    if not eligible:
        return report
    normals, stats = smooth_targets(objects)
    report.update(stats)
    planned = []
    for obj in eligible:
        try:
            values, info = encode_target(obj, normals[obj.name])
            planned.append((obj, values, info))
        except RuntimeError as exc:
            report['skipped'].append({'object': obj.name, 'reason': str(exc)})
    for obj, values, info in planned:
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        mesh = obj.data
        active = mesh.uv_layers.active.name if mesh.uv_layers.active else ''
        render = next((u.name for u in mesh.uv_layers if u.active_render), '')
        attr = mesh.attributes.get(ATTRIBUTE)
        if attr is None:
            attr = mesh.attributes.new(ATTRIBUTE, 'FLOAT2', 'CORNER')
        attr.data.foreach_set('vector', values)
        if active and mesh.uv_layers.get(active):
            mesh.uv_layers.active_index = list(mesh.uv_layers.keys()).index(active)
        if render and mesh.uv_layers.get(render):
            mesh.uv_layers[render].active_render = True
        mesh.update()
        info['adapted_modifiers'] = adapt_nodes(obj)
        obj['cmb_outline_encoding'] = ENCODING
        obj['cmb_outline_generator'] = GENERATOR
        report['rebuilt'].append(info)
        if info['fallback_corners']:
            report['warnings'].append(obj.name + ': ' + str(info['fallback_corners'])
                                      + ' 个面角的基底退化或超出正半球，保留普通法线')
    return report
