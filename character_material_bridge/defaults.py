"""Material-use defaults; the extracted shader layout remains authoritative."""
import re
from collections import defaultdict
from array import array
import bpy

API_REVISION = 513
ROLE_KEY = 'cmb_default_profile'
ROLES = {'CLOTHING', 'BODY', 'FACE', 'OTHER', 'UNKNOWN'}
FACE_RGB = (1.0736485719680786, 1.0311452150344849, 1.07626211643219)
BODY_RGB = (0.979699969291687, 0.9942499995231628, 1.0476000308990479)
ROLE_ITEMS = [('AUTO', '自动识别', '优先使用保存的部位与源材质身份'),
              ('CLOTHING', '衣服', '包括透明衣服'), ('BODY', '身体皮肤', ''),
              ('FACE', '脸', ''), ('OTHER', '其他部位', '只设置 CP11.W=-0.1')]


def resolve(material, source_material='', source_object=''):
    from . import core
    if material is None:return 'UNKNOWN'
    override = getattr(material, 'cmb_default_role', 'AUTO')
    if override in ROLES - {'UNKNOWN'}:
        return override
    if material.get('cmb_default_conflict'):
        return 'UNKNOWN'
    saved = material.get(ROLE_KEY)
    if saved in ROLES:
        return saved
    part = core.material_uber_part(material)
    identities = [source_material, material.get('cmb_default_source_material', ''),
                  source_object, material.get('cmb_default_source_object', ''), material.name]
    return infer_role(part, identities)


def infer_role(part, identities):
    if part == 'Standard':
        return 'CLOTHING'
    if part == 'Face':
        for identity in identities:
            words = set(re.split(r'[^a-z0-9]+', str(identity).casefold()))
            face = bool(words & {'face', 'head'})
            body = bool(words & {'body', 'skin'})
            if face != body:
                return 'FACE' if face else 'BODY'
        return 'UNKNOWN'
    return 'OTHER' if part else 'UNKNOWN'


def bind(material, entry):
    from . import core
    role = entry.get('default_role')
    if role not in ROLES:
        role = infer_role(core.material_uber_part(material),
                          [entry.get('source_material',''), entry.get('source_object',''), material.name])
    if material.get('cmb_default_bound') and material.get(ROLE_KEY) != role:
        material['cmb_default_conflict'] = True
    material[ROLE_KEY] = role
    material['cmb_default_bound'] = True
    material['cmb_default_source_material'] = entry.get('source_material', '')
    material['cmb_default_source_object'] = entry.get('source_object', '')


def values(role, current):
    result = {}
    cp = current.get('_CharacterParams11')
    if cp is not None:
        cp = list(cp)
        cp[3] = -.15 if role == 'CLOTHING' else 0.0 if role in {'FACE', 'BODY'} else -.1
        if role != 'UNKNOWN':
            result['_CharacterParams11'] = cp
    if role in {'CLOTHING', 'BODY', 'FACE'}:
        if current.get('_CharacterParams9') is not None:
            result['_CharacterParams9'] = [0., 1. if role == 'FACE' else -1., 0., .4]
        if current.get('_CharacterParams8') is not None:
            result['_CharacterParams8'] = [3., 3., 3., 1.] if role == 'FACE' else [1., 1., 1., 1.]
    if role in {'BODY', 'FACE'} and current.get('_BaseColor') is not None:
        result['_BaseColor'] = list(FACE_RGB if role == 'FACE' else BODY_RGB) + [current['_BaseColor'][3]]
    return result


def current_values(material):
    from . import core
    snap = core.read_material_column(material)
    if snap is None:
        return None
    part, col, width, pixels = snap
    return part, col, {name: core.read_param(pixels, width, col, part, name) for name in
                       ('_CharacterParams11', '_CharacterParams9', '_CharacterParams8', '_BaseColor')}


def default_value(material, part, name, current=None):
    from . import core
    if name not in {'_CharacterParams11', '_CharacterParams9', '_CharacterParams8', '_BaseColor'}:
        return core.default_value(part, name)
    role = resolve(material)
    if role == 'UNKNOWN':
        return None
    if current is None:
        snap = current_values(material)
        current = snap[2].get(name) if snap else core.default_value(part, name)
    return values(role, {name: current}).get(name, core.default_value(part, name))


def differs(material, part, name, value, epsilon=1e-4):
    default = default_value(material, part, name, value)
    if default is None or value is None:
        return False
    if isinstance(value, (tuple, list)):
        return any(abs(float(a)-float(b)) > epsilon for a, b in zip(value, default))
    return abs(float(value)-float(default)) > epsilon


def apply(objects, scene=None, undo_checkpoint=False):
    from . import core, animation
    scene = scene or bpy.context.scene
    materials = {slot.material for obj in objects if obj.type == 'MESH'
                 for slot in obj.material_slots if slot.material}
    bindings = defaultdict(list)
    report = {'materials': 0, 'columns': 0, 'warnings': [], 'skipped': []}
    for mat in materials:
        snap = current_values(mat)
        if snap is None or not core.stamp_matches(mat):
            report['skipped'].append(mat.name + ': 无匹配参数布局')
            continue
        part, col, current = snap
        bindings[(core.mat_param_image(mat), col)].append((mat, resolve(mat), current, part))
    # Include other users of each binding so a manual selection cannot silently
    # overwrite another material using that same column with a different role.
    for mat in bpy.data.materials:
        if mat in materials:
            continue
        key = (core.mat_param_image(mat), core.material_param_col(mat))
        if key in bindings:
            bindings[key].append((mat, resolve(mat), None, core.material_uber_part(mat)))
    uploads = defaultdict(list)
    planned = []
    for (image, col), rows in bindings.items():
        roles = {r[1] for r in rows}
        part = rows[0][3]
        if len(roles) != 1 or 'UNKNOWN' in roles or len({r[3] for r in rows}) != 1:
            report['warnings'].append('默认部位不明或共享列冲突，已跳过: ' + ', '.join(r[0].name for r in rows))
            continue
        updates = [(part, name, value) for name, value in values(rows[0][1], rows[0][2]).items()]
        if not updates:
            continue
        uploads[image].append((col, updates))
        planned.append((image, col, updates, rows))
    # Image pixel buffers are not reliable native undo storage. Keep the original
    # values in persistent RNA channels before the operator creates its checkpoint.
    # Transfer calls do not push an undo step; their transaction owns that history.
    initialized = False
    if hasattr(scene, 'cmb_anim_channels'):
        for image, col, updates, rows in planned:
            for part, name, _ in updates:
                if animation.find(scene, image, col, part, name) is None:
                    animation.ensure(scene, image, col, part, name, rows[0][2][name])
                    initialized = True
    if initialized and undo_checkpoint and bpy.context.preferences.edit.use_global_undo:
        bpy.ops.ed.undo_push(message='应用部位默认值前')
    snapshots = {}
    try:
        for image, columns in uploads.items():
            pixels = array('f', [0.0]) * len(image.pixels)
            image.pixels.foreach_get(pixels)
            snapshots[image] = pixels, image.alpha_mode
            result = core.write_params_batch(image, columns, pack=True)
            if result['unknown']:
                raise RuntimeError('默认参数布局不匹配: ' + str(result['unknown']))
    except Exception:
        for image, (pixels, alpha) in snapshots.items():
            image.pixels.foreach_set(pixels); image.alpha_mode = alpha
            image.update(); image.pack()
        raise
    for image, col, updates, rows in planned:
        for other in bpy.data.scenes:
            animation.adopt_updates(other, image, col, updates)
        for mat, role, _, _ in rows:
            mat[ROLE_KEY] = role
            mat['cmb_default_revision'] = API_REVISION
        report['columns'] += 1
        report['materials'] += sum(r[0] in materials for r in rows)
    return report


def binding_conflict(material):
    from . import core
    image, col = core.mat_param_image(material), core.material_param_col(material)
    kinds = {(resolve(m), core.material_uber_part(m)) for m in bpy.data.materials
             if core.material_param_col(m) == col and core.mat_param_image(m) == image}
    return len(kinds) > 1


def role_updated(material, context):
    # Keep the explicit selector separate from the inferred saved role.
    if context and hasattr(context.scene, 'cmb_params'):
        context.scene.cmb_params.note = '部位已改变，请刷新参数列表；应用默认值后写入材质'


def register():
    bpy.types.Material.cmb_default_role = bpy.props.EnumProperty(
        name='默认参数部位', items=ROLE_ITEMS, default='AUTO', update=role_updated)


def unregister():
    if hasattr(bpy.types.Material, 'cmb_default_role'):
        del bpy.types.Material.cmb_default_role
