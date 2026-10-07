"""Keep Hair CP13.W on the surface and force it to zero on outline shells.

Only the material root is rewired; shared generated shader stages are untouched.
The adapter is native shader math, so no runtime callbacks or parameter writes
are needed. The serialized graph, not custom metadata, proves idempotence.
"""
import bpy
from . import core
from .face_outline import _linked, _reads_image

API_REVISION = 522
PARAMETER = '_CharacterParams13_w'
MASK = '_RuriOutlineShellGate'
GROUP_NAME = 'CMB Hair Outline CP13 W v1'


def _group_valid(tree):
    if tree is None or tree.animation_data or not tree.name.startswith(GROUP_NAME):
        return False
    try:
        nodes = tree.nodes
        if len(nodes) != 5:
            return False
        gi, go = nodes['Input'], nodes['Output']
        clamp, inverse, multiply = nodes['Shell'], nodes['Surface'], nodes['Value']
        if gi.type != 'GROUP_INPUT' or go.type != 'GROUP_OUTPUT':
            return False
        if (clamp.type != 'CLAMP' or clamp.clamp_type != 'MINMAX'
                or clamp.inputs['Min'].is_linked or clamp.inputs['Min'].default_value != 0.0
                or clamp.inputs['Max'].is_linked or clamp.inputs['Max'].default_value != 1.0):
            return False
        if (inverse.type != 'MATH' or inverse.operation != 'SUBTRACT' or inverse.use_clamp
                or inverse.inputs[0].is_linked or inverse.inputs[0].default_value != 1.0
                or multiply.type != 'MATH' or multiply.operation != 'MULTIPLY' or multiply.use_clamp):
            return False
        edges = ((gi.outputs['Mask'], clamp.inputs['Value']),
                 (clamp.outputs['Result'], inverse.inputs[1]),
                 (gi.outputs['W'], multiply.inputs[0]),
                 (inverse.outputs[0], multiply.inputs[1]),
                 (multiply.outputs[0], go.inputs['W']))
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
    for name, default in (('W', 1.0), ('Mask', 0.0)):
        socket = group.interface.new_socket(name=name, in_out='INPUT', socket_type='NodeSocketFloat')
        socket.default_value = default
    group.interface.new_socket(name='W', in_out='OUTPUT', socket_type='NodeSocketFloat')
    gi = group.nodes.new('NodeGroupInput'); gi.name = 'Input'; gi.location = (-500, 0)
    go = group.nodes.new('NodeGroupOutput'); go.name = 'Output'; go.location = (300, 0)
    clamp = group.nodes.new('ShaderNodeClamp'); clamp.name = 'Shell'; clamp.clamp_type = 'MINMAX'
    clamp.inputs['Min'].default_value = 0; clamp.inputs['Max'].default_value = 1
    clamp.location = (-300, -100)
    inverse = group.nodes.new('ShaderNodeMath'); inverse.name = 'Surface'; inverse.operation = 'SUBTRACT'
    inverse.inputs[0].default_value = 1; inverse.location = (-100, -100)
    multiply = group.nodes.new('ShaderNodeMath'); multiply.name = 'Value'; multiply.operation = 'MULTIPLY'
    multiply.location = (100, 0)
    group.links.new(gi.outputs['Mask'], clamp.inputs['Value'])
    group.links.new(clamp.outputs['Result'], inverse.inputs[1])
    group.links.new(gi.outputs['W'], multiply.inputs[0])
    group.links.new(inverse.outputs[0], multiply.inputs[1])
    group.links.new(multiply.outputs[0], go.inputs['W'])
    return group


def _parameter_contract(stage):
    """The verified Hair stage only replicates CP13.W into crossing X835."""
    inputs = [n for n in stage.node_tree.nodes if n.type == 'GROUP_INPUT' and n.outputs.get(PARAMETER)]
    if len(inputs) != 1:
        return False
    links = list(inputs[0].outputs[PARAMETER].links)
    if len(links) != 3 or len({link.to_node for link in links}) != 1:
        return False
    combine = links[0].to_node
    if combine.type != 'COMBXYZ' or {link.to_socket.name for link in links} != {'X', 'Y', 'Z'}:
        return False
    outputs = list(combine.outputs[0].links)
    return (len(outputs) == 1 and outputs[0].to_node.type == 'GROUP_OUTPUT'
            and outputs[0].to_socket.name == 'X835')


def _plan(material):
    tree = material.node_tree
    if tree is None or material.library:
        raise RuntimeError('Hair 材质不是可编辑的本地节点材质')
    stages = [n for n in tree.nodes if n.type == 'GROUP' and n.node_tree and n.inputs.get(PARAMETER)]
    shells = [n for n in tree.nodes if n.type == 'GROUP' and n.node_tree and n.inputs.get(MASK)]
    if len(stages) != 1 or len(shells) != 1 or not _parameter_contract(stages[0]):
        raise RuntimeError('Hair CP13.W 着色链不是已核验的节点版本，保留原节点')
    mask = _linked(shells[0].inputs[MASK])
    if (mask is None or mask.node.type != 'ATTRIBUTE' or mask.node.attribute_name != 'ruri_outline'
            or mask.node.attribute_type != 'GEOMETRY' or mask.name not in {'Fac', 'Factor'}):
        raise RuntimeError('Hair 描边遮罩未连接 ruri_outline 几何属性')
    destination = stages[0].inputs[PARAMETER]
    image = core.mat_param_image(material)
    if image is None or not core.stamp_matches(material):
        raise RuntimeError('Hair 参数图或布局不匹配')
    markers = [n for n in tree.nodes if n.type == 'GROUP' and n.node_tree
               and n.node_tree.name.startswith(GROUP_NAME)]
    if markers:
        if len(markers) != 1 or not _group_valid(markers[0].node_tree):
            raise RuntimeError('已有 Hair 描边 CP13.W 修正被修改，未重复叠加')
        adapter = markers[0]
        source = _linked(adapter.inputs['W'])
        if (_linked(destination) != adapter.outputs['W'] or _linked(adapter.inputs['Mask']) != mask
                or source is None or not _reads_image(source, image)):
            raise RuntimeError('已有 Hair 描边 CP13.W 修正连接被修改，请检查节点')
        return None
    source = _linked(destination)
    if source is None or not _reads_image(source, image):
        raise RuntimeError('Hair CP13.W 缺少有效参数图连接')
    return tree, source, destination, mask


def _install(plan, created, undo):
    tree, source, destination, mask = plan
    adapter = tree.nodes.new('ShaderNodeGroup')
    adapter.name = 'CMB Hair Outline CP13 W'
    adapter.label = '头发 CP13.W · 描边为 0 / 表面原值'
    def rollback():
        tree.nodes.remove(adapter)
        tree.links.new(source, destination)
        tree.update_tag()
    undo.append(rollback)
    adapter.node_tree = _new_group(created)
    adapter.width = 260
    adapter.location = (destination.node.location.x - 300, destination.node.location.y - 200)
    tree.links.new(source, adapter.inputs['W'])
    tree.links.new(mask, adapter.inputs['Mask'])
    tree.links.new(adapter.outputs['W'], destination)
    tree.update_tag()


def apply(objects, transaction=None):
    """Call after lighting.initialize has isolated result materials from sources."""
    materials = {slot.material for obj in objects for slot in obj.material_slots
                 if slot.material and core.material_uber_part(slot.material) == 'Hair'}
    return apply_materials(materials, transaction)


def apply_materials(materials, transaction=None):
    """Explicit Hair materials, including library clones with cleared metadata."""
    report = {'repaired': [], 'already': [], 'warnings': []}
    plans = []
    for material in sorted(set(materials), key=lambda m: m.name):
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
