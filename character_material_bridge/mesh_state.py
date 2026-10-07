"""Final-package reference meshes and strict same-mesh shading restoration.

Reference geometry is explicit package content. Never replace the recipient's
mesh, rig, weights or shape keys; restore shading only after correspondence checks.
"""
import hashlib
import math
import struct
import sys
from array import array

API_REVISION = 524
VERSION = 1
KEY = 'cmb_reference_mesh_version'
SIGNATURE = 'cmb_reference_geometry_signature'
META_KEYS = ('cmb_outline_encoding', 'cmb_outline_generator', 'cmb_shading_basis_source')


def values(collection, field, width, kind='f'):
    result = array(kind, [0]) * (len(collection) * width)
    collection.foreach_get(field, result)
    return result


def geometry_signature(mesh):
    """Local base geometry plus ordered topology; does not depend on shading or names."""
    digest = hashlib.sha256()
    digest.update(struct.pack('<4Q', len(mesh.vertices), len(mesh.edges),
                              len(mesh.loops), len(mesh.polygons)))
    for collection, field, width, kind in (
            (mesh.vertices, 'co', 3, 'f'), (mesh.edges, 'vertices', 2, 'I'),
            (mesh.loops, 'vertex_index', 1, 'I'), (mesh.polygons, 'loop_total', 1, 'I')):
        data = values(collection, field, width, kind)
        if kind == 'f':
            if not all(math.isfinite(v) for v in data):
                raise RuntimeError('参考网格含无效顶点坐标')
            data = array('f', (round(v, 6) for v in data))
        if sys.byteorder != 'little':
            data.byteswap()
        digest.update(data.tobytes())
    return digest.hexdigest()


def make_reference(source, carrier):
    """Copy base mesh and shading; strip source rig weights, shape keys and ID props."""
    signature = geometry_signature(source.data)
    carrier.data = source.data.copy()
    carrier.data.name = 'CMB_REFERENCE_' + carrier.name
    carrier.shape_key_clear()
    for group in source.vertex_groups:
        carrier.vertex_groups.new(name=group.name)
    carrier.vertex_groups.clear()
    if any(v.groups for v in carrier.data.vertices):
        raise RuntimeError('参考网格权重清理失败: ' + source.name)
    for key in list(carrier.data.keys()):
        del carrier.data[key]
    carrier.data.materials.clear()
    if geometry_signature(carrier.data) != signature:
        raise RuntimeError('参考网格清理改变了基础几何: ' + source.name)
    carrier[KEY] = VERSION
    carrier[SIGNATURE] = signature
    for key in META_KEYS:
        if source.get(key) is not None:
            carrier[key] = str(source[key])
    return {'version': VERSION, 'signature': signature,
            'vertices': len(carrier.data.vertices), 'corners': len(carrier.data.loops)}


def restore_reference(source, target, metadata):
    mesh, reference = target.data, source.data
    if (metadata.get('version') != VERSION or source.get(KEY) != VERSION
            or not isinstance(metadata.get('signature'), str)):
        raise RuntimeError('最终包参考网格版本或记录无效: ' + target.name)
    signature = geometry_signature(reference)
    if signature != metadata['signature'] or signature != source.get(SIGNATURE):
        raise RuntimeError('最终包参考网格校验失败: ' + target.name)
    if geometry_signature(mesh) != signature:
        raise RuntimeError(target.name + ': 网格位置/拓扑顺序与最终包参考网格不同，无法安全还原校正数据。'
                           '请使用相同模型；若要移植到不同模型，可关闭“恢复最终包网格校正数据”后按目标重建')
    if mesh.users > 1:
        target.data = mesh.copy()
        mesh = target.data
    # Capture before any edits; Blender RNA collections may be invalidated by new layers.
    normals = values(reference.corner_normals, 'vector', 3)
    smooth = values(reference.polygons, 'use_smooth', 1, 'b')
    layers = []
    for uv in reference.uv_layers:
        layers.append((uv.name, 'FLOAT2', 'CORNER', 'vector',
                       values(uv.data, 'uv', 2)))
    for color in reference.color_attributes:
        layers.append((color.name, color.data_type, color.domain, 'color',
                       values(color.data, 'color', 4)))
    for name, field, width in (('ruri_tangent', 'vector', 3), ('ruri_tangent_sign', 'value', 1)):
        attr = reference.attributes.get(name)
        if attr:
            layers.append((name, attr.data_type, attr.domain, field, values(attr.data, field, width)))
    # Prebuilt MMD fur uses the same managed UV coordinate as generated shells,
    # stored per corner instead of being emitted by a generator modifier.
    fur_uv = reference.attributes.get('CMB_FurUV1')
    if fur_uv and fur_uv.name not in reference.uv_layers:
        if fur_uv.data_type != 'FLOAT_VECTOR' or fur_uv.domain not in {'CORNER', 'POINT'}:
            raise RuntimeError('最终包毛绒层属性格式无效: ' + target.name)
        data = values(fur_uv.data, 'vector', 3)
        if not all(math.isfinite(v) for v in data):
            raise RuntimeError('最终包毛绒层属性包含无效数值: ' + target.name)
        layers.append((fur_uv.name, fur_uv.data_type, fur_uv.domain, 'vector', data))
    tangent_names = {name for name, *_ in layers if name.startswith('ruri_tangent')}
    if tangent_names and tangent_names != {'ruri_tangent', 'ruri_tangent_sign'}:
        raise RuntimeError('最终包切线/符号数据不成对: ' + target.name)
    if not all(math.isfinite(v) for v in normals):
        raise RuntimeError('最终包表面法线数据无效: ' + target.name)
    for name, dtype, domain, field, data in layers:
        attr = mesh.attributes.get(name)
        if attr and (attr.data_type != dtype or attr.domain != domain):
            mesh.attributes.remove(attr)
            attr = None
        if attr is None:
            attr = mesh.attributes.new(name, dtype, domain)
        attr.data.foreach_set(field, data)
    edge = reference.attributes.get('sharp_edge')
    target_edge = mesh.attributes.get('sharp_edge')
    if edge:
        if target_edge is None:
            target_edge = mesh.attributes.new('sharp_edge', 'BOOLEAN', 'EDGE')
        target_edge.data.foreach_set('value', values(edge.data, 'value', 1, 'b'))
    elif target_edge:
        mesh.attributes.remove(target_edge)
    mesh.polygons.foreach_set('use_smooth', smooth)
    mesh.normals_split_custom_set(tuple(zip(*(iter(normals),) * 3)))
    if reference.uv_layers.active:
        mesh.uv_layers.active_index = list(mesh.uv_layers.keys()).index(reference.uv_layers.active.name)
    render = next((uv.name for uv in reference.uv_layers if uv.active_render), None)
    if render:
        mesh.uv_layers[render].active_render = True
    # A Vertex Color node without an explicit layer name reads the render color
    # attribute. Restoring the values alone leaves an unrelated target layer
    # active, silently changing the final package's shading.
    colors = mesh.color_attributes
    if reference.color_attributes.default_color_name in colors:
        colors.render_color_index = list(colors.keys()).index(
            reference.color_attributes.default_color_name)
    if reference.color_attributes.active_color_name in colors:
        colors.active_color = colors[reference.color_attributes.active_color_name]
    for key in META_KEYS:
        if source.get(key) is not None:
            target[key] = str(source[key])
    mesh.update()
    return {'object': target.name, 'vertices': len(mesh.vertices), 'corners': len(mesh.loops),
            'tangents': bool(tangent_names), 'layers': [row[0] for row in layers],
            'surface_normals': True, 'reference_mesh_restored': True}
