"""Shared environment records read by CMB-owned shaders through native attributes.

Only an unambiguous Background and optional Environment Texture/Mapping chain
is bound. Arbitrary procedural World graphs keep their authored connections.
"""
from collections import defaultdict
import json
import bpy

API_REVISION = 600
KEY = 'cmb_environment53'
PREFIX = 'CMB53 World '
COLOR = 'cmb53_environment_color'
STRENGTH = 'cmb53_environment_strength'
STATS = defaultdict(int)
_states = {}
LAST_WARNING = ''


def clear():
    global LAST_WARNING
    _states.clear()
    LAST_WARNING = ''


def describe(scene, depsgraph=None):
    original = scene.world
    world = original.evaluated_get(depsgraph) if original and depsgraph is not None else original
    if world is None:return None
    if not world.use_nodes or world.node_tree is None:
        return {'color': COLOR, 'strength': STRENGTH, 'image': None, 'mapping': None,
                'values': {COLOR: tuple(world.color), STRENGTH: 1.0},
                'signature': (original.session_uid, 'COLOR')}
    outputs = [n for n in world.node_tree.nodes if n.type == 'OUTPUT_WORLD' and n.is_active_output]
    if len(outputs) != 1 or not outputs[0].inputs['Surface'].is_linked:return None
    background = outputs[0].inputs['Surface'].links[0].from_node
    if background.type != 'BACKGROUND' or background.inputs['Strength'].is_linked:return None
    image = mapping = None
    color = background.inputs['Color']
    if color.is_linked:
        source = color.links[0].from_node
        if source.type != 'TEX_ENVIRONMENT' or source.image is None:return None
        image = source.image
        if source.inputs['Vector'].is_linked:
            mapping = source.inputs['Vector'].links[0].from_node
            if mapping.type != 'MAPPING':return None
            if any(mapping.inputs[name].is_linked for name in ('Location','Rotation','Scale')):return None
    values = {COLOR: tuple(background.inputs['Color'].default_value),
              STRENGTH: float(background.inputs['Strength'].default_value)}
    for name, default in (('Location',(0,0,0)),('Rotation',(0,0,0)),('Scale',(1,1,1))):
        values['cmb53_environment_' + name.lower()] = tuple(mapping.inputs[name].default_value) if mapping else default
    return {'color': COLOR, 'strength': STRENGTH, 'image': image, 'mapping': mapping, 'values': values,
            'strength_value': float(background.inputs['Strength'].default_value),
            'signature': (original.session_uid, background.name, image.session_uid if image else 0,
                          mapping.name if mapping else '', mapping.vector_type if mapping else '')}


def capture(source, destination, scene):
    """Record proven World samplers before source ownership metadata is stripped."""
    stored = source.get(KEY)
    if stored:
        destination[KEY] = stored
        return
    descriptor = describe(scene)
    if descriptor is None or descriptor['image'] is None:return
    records = []
    for node in source.node_tree.nodes:
        if node.type != 'TEX_ENVIRONMENT' or node.image != descriptor['image']:continue
        targets = []
        for link in node.outputs['Color'].links:
            scale = link.to_node
            if (scale.type == 'VECT_MATH' and scale.operation == 'SCALE' and
                    not scale.inputs['Scale'].is_linked and
                    abs(scale.inputs['Scale'].default_value-descriptor['strength_value']) < 1e-7):
                targets.extend((edge.to_node.name, edge.to_socket.identifier) for edge in scale.outputs[0].links)
            else:
                targets.append((link.to_node.name, link.to_socket.identifier))
        records.append({'node': node.name, 'targets': targets})
    if records:
        destination[KEY] = json.dumps(records, separators=(',', ':'))


def _attribute(tree, name, path):
    node = tree.nodes.get(PREFIX + name)
    if node is None:
        node = tree.nodes.new('ShaderNodeAttribute');node.name = PREFIX + name
    node.attribute_type = 'VIEW_LAYER';node.attribute_name = path
    node.label = 'CMB 实时环境 / ' + name
    return node


def _publish(scene, descriptor, depsgraph):
    """Publish one Scene record; cost does not scale with material/character count."""
    if descriptor is None:return
    targets = [scene]
    if depsgraph is not None:
        evaluated = scene.evaluated_get(depsgraph)
        if evaluated != scene:targets.append(evaluated)
    for target in targets:
        for key, value in descriptor['values'].items():
            old = target.get(key)
            if hasattr(value, '__len__'):
                changed = old is None or not hasattr(old,'__len__') or tuple(old) != tuple(value)
            else:changed = old is None or old != value
            if changed:
                target[key] = value
                STATS['environment_property_writes'] += 1


def _socket(node, identifier):
    return next((socket for socket in node.inputs if socket.identifier == identifier), None)


def eligible(material):
    """Structural discovery runs with the coordinator's binding rebuild, not every frame."""
    return bool(material.get(KEY) or any(
        node.name.startswith(PREFIX) or (node.type == 'GROUP' and any(
            '_AmbientIrradiance' in socket.name or '_SpecularRadiance' in socket.name for socket in node.inputs))
        for node in material.node_tree.nodes))


def bind(material, scene):
    descriptor = describe(scene)
    if descriptor is None:return False
    tree = material.node_tree
    if tree is None:return False
    try:
        records = json.loads(material.get(KEY, '[]'))
    except (ValueError, TypeError):
        return False
    if not isinstance(records,list) or any(not isinstance(r,dict) or not isinstance(r.get('node'),str)
            or not isinstance(r.get('targets'),list) for r in records):return False
    # Flat environment queries are semantic sockets in the generated shader.
    # Existing links carry authored normal/reflection queries and are preserved.
    flat = [(node, socket) for node in tree.nodes if node.type == 'GROUP'
            for socket in node.inputs if (not socket.is_linked or
                socket.links[0].from_node.name == PREFIX+'Flat Radiance') and
            ('_AmbientIrradiance' in socket.name or '_SpecularRadiance' in socket.name)]
    if not records and not flat:return False
    color = _attribute(tree, 'Color', descriptor['color'])
    strength = _attribute(tree, 'Strength', descriptor['strength']) if descriptor['strength'] else None
    for record in records:
        sampler = tree.nodes.get(record['node'])
        if sampler is None or sampler.type != 'TEX_ENVIRONMENT':continue
        if descriptor['image'] is not None:
            sampler.image = descriptor['image']
            incoming = sampler.inputs['Vector']
            mapping_name = PREFIX + 'Mapping ' + sampler.name
            mapping = tree.nodes.get(mapping_name)
            if mapping is None:
                mapping = tree.nodes.new('ShaderNodeMapping');mapping.name = mapping_name
                if incoming.is_linked:
                    original = incoming.links[0].from_node
                    if original.type == 'MAPPING':
                        original = original.inputs['Vector']
                        if original.is_linked:tree.links.new(original.links[0].from_socket,mapping.inputs['Vector'])
                    else:tree.links.new(incoming.links[0].from_socket,mapping.inputs['Vector'])
                tree.links.new(mapping.outputs['Vector'], incoming)
            upstream = descriptor['mapping']
            mapping.vector_type = upstream.vector_type if upstream else 'POINT'
            for name, default in (('Location',(0,0,0)),('Rotation',(0,0,0)),('Scale',(1,1,1))):
                socket = mapping.inputs[name]
                for link in list(socket.links):tree.links.remove(link)
                if upstream:
                    path = 'cmb53_environment_' + name.lower()
                    attr = _attribute(tree, 'Mapping ' + name, path)
                    tree.links.new(attr.outputs['Vector'], socket)
                else:socket.default_value=default
            source = sampler.outputs['Color']
        else:
            source = color.outputs['Color']
        scale_name = PREFIX + 'Radiance ' + sampler.name
        scale = tree.nodes.get(scale_name)
        if scale is None:scale=tree.nodes.new('ShaderNodeVectorMath');scale.name=scale_name
        scale.operation='SCALE';tree.links.new(source,scale.inputs[0])
        if strength:tree.links.new(strength.outputs['Fac'],scale.inputs['Scale'])
        else:
            for link in list(scale.inputs['Scale'].links):tree.links.remove(link)
            scale.inputs['Scale'].default_value=1.0
        for node_name, identifier in record['targets']:
            node=tree.nodes.get(node_name);target=_socket(node,identifier) if node else None
            if target:tree.links.new(scale.outputs['Vector'],target)
    if flat and descriptor['image'] is None:
        radiance=tree.nodes.get(PREFIX+'Flat Radiance')
        if radiance is None:radiance=tree.nodes.new('ShaderNodeVectorMath');radiance.name=PREFIX+'Flat Radiance'
        radiance.operation='SCALE';tree.links.new(color.outputs['Color'],radiance.inputs[0])
        if strength:tree.links.new(strength.outputs['Fac'],radiance.inputs['Scale'])
        else:radiance.inputs['Scale'].default_value=1.0
        for _,socket in flat:tree.links.new(radiance.outputs['Vector'],socket)
    STATS['material_rebinds'] += 1
    return True


def synchronize(scene, materials, force=False, depsgraph=None):
    global LAST_WARNING
    materials = tuple(materials)
    if not materials:
        LAST_WARNING = ''
        return 0
    descriptor = describe(scene, depsgraph)
    LAST_WARNING = '' if descriptor is not None else '环境同步支持 Background / Environment Texture / Mapping；当前 World 保留上次有效值'
    _publish(scene, descriptor, depsgraph)
    signature = descriptor['signature'] if descriptor else None
    key = scene.session_uid
    signature = (signature, tuple(m.session_uid for m in materials))
    if not force and _states.get(key) == signature:return 0
    count = sum(bool(bind(material,scene)) for material in materials)
    _states[key] = signature
    return count
