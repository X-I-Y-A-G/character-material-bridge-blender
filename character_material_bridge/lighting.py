"""CMB-owned scene lighting. No Importer runtime or global datablock remapping."""
import math
import uuid
from array import array
import bpy

API_REVISION = 527
ROWS, COLS = 4, 64
_cache = {}


def owned(obj):
    return obj.type == 'MESH' and obj.get('cmb_role', obj.get('jmb_role')) == 'result'


def targets(scene):
    return [o for o in scene.objects if owned(o)]


def image_refs(node):
    image = getattr(node, 'image', None)
    if image is not None:yield ('image', node, image)
    for socket in node.inputs:
        value = getattr(socket, 'default_value', None)
        if isinstance(value, bpy.types.Image):yield ('default_value', socket, value)


def is_light_table(image):
    name = image.name.casefold()
    return (image.get('cmb_data_role') == 'LIGHTS' or name.startswith('rurilighttable')
            or name.startswith('cmb light table')) and tuple(image.size) == (COLS, ROWS)


def _new_image(name, width, height, role, owner, pixels=None):
    from . import color_management
    image = bpy.data.images.new(name, width=width, height=height, float_buffer=True, alpha=True)
    color_management.set_data(image);image.alpha_mode = 'CHANNEL_PACKED'
    image['cmb_data_role'] = role;image['cmb_runtime_owner'] = owner
    if pixels is not None:image.pixels.foreach_set(pixels)
    image.update();image.pack()
    return image


def _materials(objects):
    return {s.material for o in objects for s in o.material_slots if s.material}


def _runtime_images(objects):
    from . import core
    objects = list(objects)
    images = {image for tree in core.walk_trees_for_objects(objects)
            for node in tree.nodes for _, _, image in image_refs(node)
            if image.get('cmb_data_role') in {'PARAMS', 'LIGHTS'}}
    for obj in objects:
        for modifier in obj.modifiers:
            if modifier.type != 'NODES':continue
            # Blender 5.2 geometry-only modifiers may have no IDProperty storage.
            try:keys=list(modifier.keys())
            except TypeError:keys=()
            for key in keys:
                value = modifier.get(key)
                if isinstance(value,bpy.types.Image) and value.get('cmb_data_role') in {'PARAMS','LIGHTS'}:
                    images.add(value)
    return images


def validate_scope(scene, objects):
    objects = list(objects)
    materials = _materials(objects)
    # users_scene itself scans every object in every Scene. Build membership
    # once instead of repeating that scan for every managed mesh.
    other_members = {o for s in bpy.data.scenes if s != scene for o in s.objects}
    for obj in objects:
        if obj.library or obj.data.library:raise RuntimeError('请先将移植对象本地化: ' + obj.name)
        if obj in other_members:
            raise RuntimeError('移植对象跨场景共享，请先独立化: ' + obj.name)
    if len(bpy.data.scenes) == 1:return
    images = _runtime_images(objects)
    table = scene.get('cmb_light_table')
    if isinstance(table, bpy.types.Image):images.add(table)
    for other in bpy.data.scenes:
        if other == scene:continue
        other_objects = [o for o in other.objects if o.type == 'MESH']
        if materials & _materials(other_objects):
            raise RuntimeError('移植材质跨场景共享，请先独立化: ' + other.name)
        other_images = _runtime_images(other_objects)
        other_table = other.get('cmb_light_table')
        if isinstance(other_table, bpy.types.Image):other_images.add(other_table)
        if images & other_images:
            raise RuntimeError('独立运行参数图或灯表跨场景共享，请先独立化: ' + other.name)


def initialize(scene, objects=None, transaction=None):
    """Explicit migration / transfer transaction. Never invoked by register/load."""
    from . import core, animation, rig_runtime
    objects = list(targets(scene) if objects is None else objects)
    if not objects:return {'objects':0, 'images':0, 'warnings':['场景没有材质桥移植结果']}
    validate_scope(scene, objects)
    materials = _materials(objects)
    params = {core.mat_param_image(m) for m in materials};params.discard(None)
    owner = scene.get('cmb_runtime_id') or uuid.uuid4().hex
    # A single old physical animation binding cannot be split between source and
    # result without deciding which material those existing curves belong to.
    foreign = _materials([o for o in scene.objects if o.type == 'MESH' and not owned(o)])
    for m in foreign:
        image, col = core.mat_param_image(m), core.material_param_col(m)
        if image in params and any(c.image == image and c.col == col for c in getattr(scene,'cmb_anim_channels', ())):
            raise RuntimeError('参数动画绑定同时被源材质和移植结果使用，请先分开参数图: ' + m.name)
    before = core.id_blocks()
    changes, channel_changes, prop_changes, wire_backups, binding_backups = [], [], [], [], []
    face_rollbacks = []
    # Binding callbacks can assign an identity to existing armature data. Those
    # properties are outside id_blocks() and must be included in rollback.
    binding_arms = {rig_runtime.armature_of(obj) for obj in objects}
    binding_arms.update(obj.get('cmb_rig_armature') for obj in objects)
    binding_arms.update(getattr(getattr(obj,'cmb_rig_binding',None),'armature',None) for obj in objects)
    bone_backups = [(bone, bone.get(rig_runtime.BONE_ID))
                    for arm in binding_arms if isinstance(arm,bpy.types.Object) and arm.type=='ARMATURE'
                    for bone in arm.data.bones]
    lock_before = scene.render.use_lock_interface
    scene_before = {k:scene.get(k) for k in ('cmb_runtime_id','cmb_runtime_ready','cmb_light_table')}
    def rollback():
        scene.render.use_lock_interface = lock_before
        # The transfer's outer transaction may fail after this function has
        # succeeded. Restore references before that caller removes new IDs.
        for restore in reversed(face_rollbacks):restore()
        for holder, attr, value in reversed(changes):setattr(holder,attr,value)
        for tree,nodes,attributes,links in reversed(wire_backups):
            for node in set(tree.nodes)-nodes:tree.nodes.remove(node)
            for node,kind,name,label in attributes:
                node.attribute_type=kind;node.attribute_name=name;node.label=label
            tree.links.clear()
            for frm,out,to,inp in links:tree.links.new(tree.nodes[frm].outputs[out],tree.nodes[to].inputs[inp])
        for channel, image in channel_changes:channel.image=image
        for settings,arm,bone in binding_backups:
            settings.armature=arm;settings.bone=bone
        for bone,value in bone_backups:
            if value is None:bone.pop(rig_runtime.BONE_ID,None)
            else:bone[rig_runtime.BONE_ID]=value
        for holder,key,value in reversed(prop_changes):
            if value is None:holder.pop(key,None)
            else:holder[key]=value
        for key,value in scene_before.items():
            if value is None:scene.pop(key,None)
            else:scene[key]=value
        animation.invalidate();_cache.clear()
        from . import runtime
        runtime.invalidate(scene)
    try:
        # DATA-linked material slots belong to the Mesh, not the Object. Make
        # the target mesh local before replacing a shared material slot.
        mesh_copies = {}
        selected = set(objects)
        shared_meshes = {other.data for other in bpy.data.objects
                         if other.type == 'MESH' and other not in selected}
        for obj in objects:
            if obj.data in shared_meshes and any(slot.link == 'DATA' and slot.material
                    and (slot.material in foreign or slot.material.library) for slot in obj.material_slots):
                original = obj.data
                if original not in mesh_copies:mesh_copies[original] = original.copy()
                changes.append((obj, 'data', original));obj.data = mesh_copies[original]
        # Localize a material also used by non-CMB geometry in this scene.
        copied_materials = {}
        for obj in objects:
            for slot in obj.material_slots:
                mat = slot.material
                if mat and (mat in foreign or mat.library):
                    copy = copied_materials.get(mat)
                    if copy is None:
                        copy = mat.copy();copy.use_fake_user = False;copied_materials[mat] = copy
                    changes.append((slot, 'material', mat));slot.material = copy
        materials = _materials(objects)
        mapping = {}
        for image in params:
            if image.library is None and image.get('cmb_data_role') == 'PARAMS' and image.get('cmb_runtime_owner') == owner:
                continue
            pixels = array('f',[0.0]) * len(image.pixels);image.pixels.foreach_get(pixels)
            mapping[image] = _new_image('CMB Uber Params ' + uuid.uuid4().hex, *image.size, 'PARAMS', owner, pixels)
        table = scene.get('cmb_light_table')
        if not isinstance(table,bpy.types.Image) or not is_light_table(table) or table.get('cmb_runtime_owner') != owner:
            table = _new_image('CMB Light Table ' + owner, COLS, ROWS, 'LIGHTS', owner)
        trees = list(core.walk_trees_for_objects(objects))
        for tree in trees:
            for node in tree.nodes:
                for _, _, image in image_refs(node):
                    if is_light_table(image) and image != table:mapping[image] = table
        has_cache, copies = {}, {}
        def mutable(tree, seen=None):
            if tree in has_cache:return has_cache[tree]
            seen = set() if seen is None else seen
            if tree in seen:return False
            seen = seen | {tree}
            answer = any(image in mapping for n in tree.nodes for _,_,image in image_refs(n)) or rig_runtime.needs_wiring(tree)
            answer = answer or any(n.type == 'GROUP' and n.node_tree and mutable(n.node_tree, seen) for n in tree.nodes)
            has_cache[tree] = answer;return answer
        def rewrite(tree):
            for node in tree.nodes:
                for attr, holder, image in list(image_refs(node)):
                    if image in mapping:
                        changes.append((holder,attr,image));setattr(holder,attr,mapping[image])
                if node.type == 'GROUP' and node.node_tree and mutable(node.node_tree):
                    original = node.node_tree
                    copy = copies.get(original)
                    if copy is None:
                        copy = original.copy();copy.use_fake_user=False;copy.name='CMB RT '+original.name
                        copies[original] = copy;rewrite(copy)
                    changes.append((node,'node_tree',original));node.node_tree = copy
            if rig_runtime.needs_wiring(tree):
                nodes=set(tree.nodes)
                attributes=[(n,n.attribute_type,n.attribute_name,n.label) for n in nodes if n.type=='ATTRIBUTE']
                links=[(l.from_node.name,list(l.from_node.outputs).index(l.from_socket),l.to_node.name,list(l.to_node.inputs).index(l.to_socket)) for l in tree.links]
                wire_backups.append((tree,nodes,attributes,links))
                rig_runtime.wire_basis(tree)
        for mat in materials:
            if mat.node_tree:rewrite(mat.node_tree)
            if 'ruri_uber_stack' in mat:
                prop_changes.append((mat,'ruri_uber_stack',mat.get('ruri_uber_stack')))
                del mat['ruri_uber_stack']
            prop_changes.append((mat,'cmb_runtime_owner',mat.get('cmb_runtime_owner')))
            mat['cmb_runtime_owner'] = owner
        for obj in objects:
            for mod in obj.modifiers:
                if mod.type == 'NODES' and mod.node_group and mutable(mod.node_group):
                    original = mod.node_group
                    copy = copies.get(original)
                    if copy is None:
                        copy = original.copy();copy.use_fake_user=False;copy.name='CMB RT '+original.name
                        copies[original] = copy;rewrite(copy)
                    changes.append((mod,'node_group',original));mod.node_group=copy
                try:keys=list(mod.keys())
                except TypeError:keys=[]
                for key in keys:
                    value=mod.get(key)
                    if isinstance(value,bpy.types.Image) and value in mapping:
                        prop_changes.append((mod,key,value));mod[key]=mapping[value]
        for channel in getattr(scene,'cmb_anim_channels', ()):
            if channel.image in mapping:
                channel_changes.append((channel,channel.image));channel.image = mapping[channel.image]
        warnings=[]
        for obj in objects:
            prop_changes.append((obj,'cmb_runtime_owner',obj.get('cmb_runtime_owner')))
            obj['cmb_runtime_owner']=owner
            prop_changes.append((obj,'cmb_basis_enabled',obj.get('cmb_basis_enabled')))
            obj['cmb_basis_enabled']=rig_runtime.object_needs_basis(obj)
            if obj['cmb_basis_enabled']:
                settings=getattr(obj,'cmb_rig_binding',None)
                if settings:binding_backups.append((settings,settings.armature,settings.bone))
                for key in ('cmb_rig_armature','cmb_rig_bone_name','cmb_rig_bone_id'):
                    prop_changes.append((obj,key,obj.get(key)))
                warnings.extend(rig_runtime.initialize(obj))
        scene['cmb_runtime_id']=owner;scene['cmb_light_table']=table;scene['cmb_runtime_ready']=True
        from . import face_outline, hair_outline
        face_report = face_outline.apply(objects, transaction=face_rollbacks)
        warnings.extend(face_report['warnings'])
        hair_report = hair_outline.apply(objects, transaction=face_rollbacks)
        warnings.extend(hair_report['warnings'])
        animation.invalidate();_cache.clear()
        from . import runtime
        runtime.invalidate(scene)
        runtime.prepare_render(scene)
        if transaction is not None:transaction.append(rollback)
        return {'objects':len(objects),'images':len(mapping),'warnings':warnings,
                'face_outline':face_report,'hair_outline':hair_report}
    except Exception:
        rollback()
        bpy.data.batch_remove(ids=core.id_blocks()-before)
        raise


def _visible_lights(scene, depsgraph, for_render, candidates=None):
    layer = depsgraph.view_layer if depsgraph else bpy.context.view_layer
    allowed=set()
    candidates=list(candidates) if candidates is not None else [o for o in scene.objects if o.type=='LIGHT']
    def walk(collection, parent_hidden=False):
        hidden=parent_hidden or (collection.collection.hide_render if for_render else collection.collection.hide_viewport) or collection.exclude
        if not for_render:hidden=hidden or collection.hide_viewport
        if not hidden:allowed.add(collection.collection)
        for child in collection.children:walk(child,hidden)
    walk(layer.layer_collection)
    lights=[]
    for obj in candidates:
        if obj.type!='LIGHT' or not any(c in allowed for c in obj.users_collection):continue
        if for_render:
            if obj.hide_render:continue
        elif not obj.visible_get(view_layer=layer):continue
        lights.append(obj)
    return sorted(lights,key=lambda o:o.name)


def pack_light(obj, depsgraph=None):
    evaluated=obj.evaluated_get(depsgraph) if depsgraph else obj
    matrix=evaluated.matrix_world;data=evaluated.data;kind=data.type
    axis=matrix.to_3x3().col[2].normalized()
    flag=0. if kind=='SUN' else 2. if kind=='SPOT' else 1.
    color=[float(v)*float(data.energy) for v in data.color]
    cone=[math.cos(data.spot_size*.5),math.cos(data.spot_size*(1-data.spot_blend)*.5)] if kind=='SPOT' else [-1.,1.]
    return [list(matrix.translation)+[flag],color+[float(data.diffuse_factor)],
            list(axis)+[math.cos(data.angle*.5) if kind=='SUN' else 1.],cone+[0.,0.]]


def sync_scene(scene, depsgraph=None, for_render=False, force=False, candidates=None):
    report={'uploads':0,'warnings':[]}
    if not scene.get('cmb_runtime_ready'):return report
    table=scene.get('cmb_light_table');owner=scene.get('cmb_runtime_id')
    if not isinstance(table,bpy.types.Image) or table.get('cmb_runtime_owner')!=owner:
        report['warnings'].append('独立灯表缺失，请初始化／修复独立同步');return report
    from . import runtime
    depsgraph = runtime.scene_depsgraph(scene, depsgraph)
    lights=_visible_lights(scene,depsgraph,for_render,candidates)
    explicit=getattr(getattr(scene,'cmb_runtime_settings',None),'main_sun',None)
    main=explicit if explicit in lights and explicit.data.type=='SUN' else None
    if explicit and main is None:report['warnings'].append('指定主日光无效或不可见，已回退自动选择')
    if main is None:main=next((o for o in lights if o.data.type=='SUN'),None)
    if main is None:report['warnings'].append('无主日光：沿用固定方向兜底，请添加 Sun')
    others=[o for o in lights if o!=main]
    if len(others)>COLS-1:report['warnings'].append('附加光超过 63 盏，已按名称排序截断')
    others=others[:COLS-1]
    pixels=array('f',[0.0])*(COLS*ROWS*4)
    for col,obj in enumerate(([main]+others) if main else [None]+others):
        if obj is None:continue
        for row,values in enumerate(pack_light(obj,depsgraph)):
            start=(row*COLS+col)*4;pixels[start:start+4]=array('f',values)
    pixels[(3*COLS)*4+2]=float(main is not None);pixels[(3*COLS)*4+3]=float(len(others))
    signature=(depsgraph.view_layer.name,for_render,pixels.tobytes())
    key=table.as_pointer()
    if _cache.get(key)!=signature or force:
        # Forced evaluation (render/save/export) is not an unconditional upload.
        old=array('f',[0.0])*len(pixels);table.pixels.foreach_get(old)
        if old==pixels:
            _cache[key]=signature
            return report
        table.pixels.foreach_set(pixels);table.update();table.update_tag()
        # image.update() invalidates its GPU copy without worker-side gl_free.
        _cache[key]=signature;report['uploads']=1
    return report


def flush(scene):
    table=scene.get('cmb_light_table')
    if isinstance(table,bpy.types.Image) and (table.is_dirty or not table.packed_file):table.pack()


def clear():
    _cache.clear()
