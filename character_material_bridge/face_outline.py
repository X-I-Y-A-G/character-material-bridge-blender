"""Face shell brightness after shading, fed by the existing Uber parameter.

The supported Face graph uses its original brightness only in additional-light
albedo. Hold that input at 0.5 and apply 2*max(B,0) to the finished shell RGB.
All work is explicit initialization; no handlers or per-frame Python are added.
"""
import bpy
from . import core

API_REVISION = 517
BASELINE = 0.5
PARAMETER = '_OutlineColorBrightness'
GROUP_NAME = 'CMB Face Outline Brightness v1'


def _linked(socket):
    return socket.links[0].from_socket if len(socket.links) == 1 else None


def _group_valid(tree):
    """Recognize packaged copies even after LibraryBuilder clears ID properties."""
    if tree is None or tree.animation_data or not tree.name.startswith(GROUP_NAME):
        return False
    try:
        nodes = tree.nodes
        if len(nodes) != 8:
            return False
        gi, go = nodes['Input'], nodes['Output']
        if gi.type != 'GROUP_INPUT' or go.type != 'GROUP_OUTPUT':
            return False
        specs = (('Positive', 'MAXIMUM', 0.0), ('Scale', 'MULTIPLY', 2.0),
                 ('Delta', 'SUBTRACT', 1.0), ('Mask', 'MULTIPLY', None),
                 ('Gain', 'ADD', 1.0))
        for name, operation, constant in specs:
            node = nodes[name]
            if node.type != 'MATH' or node.operation != operation or node.use_clamp:
                return False
            if constant is not None and (node.inputs[1].is_linked or node.inputs[1].default_value != constant):
                return False
        color = nodes['Color']
        if color.type != 'VECT_MATH' or color.operation != 'SCALE':
            return False
        edges = ((gi.outputs['Brightness'], nodes['Positive'].inputs[0]),
                 (nodes['Positive'].outputs[0], nodes['Scale'].inputs[0]),
                 (nodes['Scale'].outputs[0], nodes['Delta'].inputs[0]),
                 (nodes['Delta'].outputs[0], nodes['Mask'].inputs[0]),
                 (gi.outputs['Mask'], nodes['Mask'].inputs[1]),
                 (nodes['Mask'].outputs[0], nodes['Gain'].inputs[0]),
                 (gi.outputs['Color'], color.inputs[0]),
                 (nodes['Gain'].outputs[0], color.inputs['Scale']),
                 (color.outputs[0], go.inputs['Color']))
        return len(tree.links) == len(edges) and all(_linked(dst) == src for src, dst in edges)
    except (KeyError, AttributeError, ReferenceError):
        return False


def installed(material):
    tree = material.node_tree if material else None
    return bool(tree and any(n.type == 'GROUP' and _group_valid(n.node_tree) for n in tree.nodes))


def _new_group(created):
    group = next((g for g in bpy.data.node_groups if g.library is None and _group_valid(g)), None)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(GROUP_NAME, 'ShaderNodeTree')
    created.append(group)
    for name, kind, default in (('Color', 'NodeSocketColor', (0, 0, 0, 1)),
                                ('Brightness', 'NodeSocketFloat', BASELINE),
                                ('Mask', 'NodeSocketFloat', 0.0)):
        socket = group.interface.new_socket(name=name, in_out='INPUT', socket_type=kind)
        socket.default_value = default
    group.interface.new_socket(name='Color', in_out='OUTPUT', socket_type='NodeSocketColor')
    gi = group.nodes.new('NodeGroupInput'); gi.name = 'Input'; gi.location = (-700, 0)
    go = group.nodes.new('NodeGroupOutput'); go.name = 'Output'; go.location = (550, 0)
    specs = (('Positive', 'MAXIMUM', 0.0), ('Scale', 'MULTIPLY', 2.0),
             ('Delta', 'SUBTRACT', 1.0), ('Mask', 'MULTIPLY', None), ('Gain', 'ADD', 1.0))
    previous = gi.outputs['Brightness']
    for i, (name, operation, constant) in enumerate(specs):
        node = group.nodes.new('ShaderNodeMath'); node.name = name; node.operation = operation
        node.location = (-500 + i * 160, -150)
        group.links.new(previous, node.inputs[0])
        if constant is None:
            group.links.new(gi.outputs['Mask'], node.inputs[1])
        else:
            node.inputs[1].default_value = constant
        previous = node.outputs[0]
    color = group.nodes.new('ShaderNodeVectorMath'); color.name = 'Color'; color.operation = 'SCALE'
    color.location = (350, 0)
    group.links.new(gi.outputs['Color'], color.inputs[0])
    group.links.new(previous, color.inputs['Scale'])
    group.links.new(color.outputs[0], go.inputs['Color'])
    return group


def _brightness_contract(node):
    """Known generated input: B -> Combine XYZ -> crossing X201, no extra uses."""
    tree = node.node_tree
    gi = [n for n in tree.nodes if n.type == 'GROUP_INPUT' and n.outputs.get(PARAMETER)]
    if len(gi) != 1:
        return False
    links = list(gi[0].outputs[PARAMETER].links)
    if len(links) != 3 or len({l.to_node for l in links}) != 1:
        return False
    combine = links[0].to_node
    if combine.type != 'COMBXYZ' or {l.to_socket.name for l in links} != {'X', 'Y', 'Z'}:
        return False
    return (len(combine.outputs[0].links) == 1
            and combine.outputs[0].links[0].to_node.type == 'GROUP_OUTPUT'
            and combine.outputs[0].links[0].to_socket.name == 'X201')


def _shell_contract(node):
    tree = node.node_tree
    inputs = [n for n in tree.nodes if n.type == 'GROUP_INPUT' and n.outputs.get('_RuriOutlineShellGate')]
    if len(inputs) != 1 or len(inputs[0].outputs['_RuriOutlineShellGate'].links) != 1:
        return False
    link = inputs[0].outputs['_RuriOutlineShellGate'].links[0]
    mix = link.to_node
    if mix.type != 'MIX' or mix.data_type != 'VECTOR' or link.to_socket != mix.inputs[0]:
        return False
    links = [l for s in mix.outputs for l in s.links]
    return len(links) == 1 and links[0].to_node.type == 'GROUP_OUTPUT' and links[0].to_socket.name == 'Z0_r_albedo'


def _surface_emissions(tree):
    # Visit each root node once. Never recursively expand all possible DAG paths.
    todo = [n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output]
    seen, result = set(), []
    while todo:
        node = todo.pop()
        if node in seen:
            continue
        seen.add(node)
        if node.type == 'EMISSION':
            result.append(node)
        sockets = [node.inputs['Surface']] if node.type == 'OUTPUT_MATERIAL' else node.inputs
        todo.extend(l.from_node for s in sockets for l in s.links)
    return result


def _reads_image(socket, image):
    todo, seen = [socket.node], set()
    while todo:
        node = todo.pop()
        if node in seen:
            continue
        seen.add(node)
        if getattr(node, 'image', None) == image:
            return True
        todo.extend(l.from_node for s in node.inputs for l in s.links)
    return False


def _plan(material):
    tree = material.node_tree
    if tree is None or material.library:
        raise RuntimeError('材质不是可编辑的本地节点材质')
    stages = [n for n in tree.nodes if n.type == 'GROUP' and n.node_tree and n.inputs.get(PARAMETER)]
    shells = [n for n in tree.nodes if n.type == 'GROUP' and n.node_tree and n.inputs.get('_RuriOutlineShellGate')]
    emissions = _surface_emissions(tree)
    if len(stages) != 1 or len(shells) != 1 or len(emissions) != 1:
        raise RuntimeError('Face 着色链结构不唯一，保留原节点')
    stage, shell, emission = stages[0], shells[0], emissions[0]
    if not _brightness_contract(stage) or not _shell_contract(shell):
        raise RuntimeError('Face 描边公式不是已核验的节点版本，保留原节点')
    brightness, color = stage.inputs[PARAMETER], emission.inputs['Color']
    mask = _linked(shell.inputs['_RuriOutlineShellGate'])
    if (mask is None or mask.node.type != 'ATTRIBUTE' or mask.node.attribute_name != 'ruri_outline'
            or mask.node.attribute_type != 'GEOMETRY' or mask.name not in {'Fac', 'Factor'}):
        raise RuntimeError('Face 描边遮罩未连接 ruri_outline 几何属性')
    image = core.mat_param_image(material)
    if image is None or not core.stamp_matches(material):
        raise RuntimeError('Face 参数图或布局不匹配')
    markers = [n for n in tree.nodes if n.type == 'GROUP' and n.node_tree
               and n.node_tree.name.startswith(GROUP_NAME)]
    if markers:
        if len(markers) != 1 or not _group_valid(markers[0].node_tree):
            raise RuntimeError('已有描边亮度修正节点被修改，未重复叠加')
        adapter = markers[0]
        source = _linked(adapter.inputs['Brightness'])
        if (brightness.is_linked or abs(brightness.default_value - BASELINE) > 1e-7
                or _linked(color) != adapter.outputs['Color'] or _linked(adapter.inputs['Color']) is None
                or _linked(adapter.inputs['Mask']) != mask or source is None or not _reads_image(source, image)):
            raise RuntimeError('已有描边亮度修正连接被修改，请检查节点')
        return None
    source, base_color = _linked(brightness), _linked(color)
    if source is None or not _reads_image(source, image) or base_color is None:
        raise RuntimeError('Face 亮度参数或最终颜色缺少有效连接')
    from . import animation
    if any(curve.data_path == brightness.path_from_id('default_value') for curve in animation.action_curves(tree)):
        raise RuntimeError('Face 节点亮度输入有独立动画，请使用材质桥参数通道')
    return (tree, brightness, source, color, base_color, mask)


def _install(plan, created, undo):
    tree, brightness, source, color, base_color, mask = plan
    old_default = brightness.default_value
    adapter = tree.nodes.new('ShaderNodeGroup')
    adapter.name = 'CMB Face Outline Brightness'
    adapter.label = 'Face 描边亮度 · 0.5 = 原始'
    # Record before any operation that may fail, including group construction.
    def rollback():
        tree.nodes.remove(adapter)
        brightness.default_value = old_default
        tree.links.new(source, brightness)
        tree.links.new(base_color, color)
    undo.append(rollback)
    adapter.node_tree = _new_group(created)
    adapter.width = 250
    adapter.location = (color.node.location.x - 290, color.node.location.y - 160)
    tree.links.new(source, adapter.inputs['Brightness'])
    tree.links.new(base_color, adapter.inputs['Color'])
    tree.links.new(mask, adapter.inputs['Mask'])
    for link in list(brightness.links):
        tree.links.remove(link)
    brightness.default_value = BASELINE
    tree.links.new(adapter.outputs['Color'], color)
    tree.update_tag()


def apply(objects, transaction=None):
    """Apply after lighting.initialize has isolated CMB materials from sources."""
    materials = {s.material for obj in objects for s in obj.material_slots
                 if s.material and core.material_uber_part(s.material) == 'Face'}
    return apply_materials(materials, transaction)


def apply_materials(materials, transaction=None):
    """Explicit Face materials, including export clones whose metadata was cleared."""
    materials = sorted(set(materials), key=lambda m: m.name)
    report = {'repaired': [], 'already': [], 'warnings': []}
    plans = []
    for material in materials:
        try:
            plan = _plan(material)
        except RuntimeError as exc:
            report['warnings'].append(material.name + '：' + str(exc))
            continue
        if plan is None:
            report['already'].append(material.name)
        else:
            plans.append((material, plan))
    created, undo = [], []
    def rollback():
        for restore in reversed(undo):
            restore()
        for group in created:
            if group.users == 0:
                bpy.data.node_groups.remove(group)
    try:
        for material, plan in plans:
            _install(plan, created, undo)
            report['repaired'].append(material.name)
    except Exception:
        rollback()
        raise
    if transaction is not None and undo:
        transaction.append(rollback)
    return report
