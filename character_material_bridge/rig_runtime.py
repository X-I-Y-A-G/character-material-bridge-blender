"""Evaluated per-object head rotation uniforms for transferred Ruri shaders."""
import uuid
import bpy
from mathutils import Matrix, Vector

API_REVISION=527
BASIS=('cmb_face_basis0','cmb_face_basis1','cmb_face_basis2')
SOCKETS=('_RuriRigBasis0','_RuriRigBasis1','_RuriRigBasis2')
BONE_ID='cmb_basis_bone_id'
SWAP=Matrix(((1,0,0),(0,0,1),(0,1,0)))


def needs_basis(tree):
    if tree is None:return False
    return any((n.type=='GROUP' and all(n.inputs.get(k) is not None for k in SOCKETS))
               or (n.type=='ATTRIBUTE' and 'ruri_face_basis' in n.attribute_name)
               or (n.type=='ATTRIBUTE' and 'cmb_face_basis' in n.attribute_name) for n in tree.nodes)


def needs_wiring(tree):
    """Reading a basis does not mean an already-correct tree needs copying."""
    if tree.bl_idname != 'ShaderNodeTree':return False
    for node in tree.nodes:
        if node.type == 'ATTRIBUTE' and 'ruri_face_basis' in node.attribute_name:
            return True
        if node.type == 'ATTRIBUTE' and 'cmb_face_basis' in node.attribute_name and node.attribute_type != 'OBJECT':
            return True
        if node.type != 'GROUP' or not all(node.inputs.get(k) is not None for k in SOCKETS):
            continue
        for key, name in zip(BASIS, SOCKETS):
            links = node.inputs[name].links
            if len(links) != 1:return True
            link = links[0]
            if (link.from_node.type != 'ATTRIBUTE' or link.from_node.attribute_type != 'OBJECT'
                    or link.from_node.attribute_name != '["'+key+'"]'
                    or link.from_socket != link.from_node.outputs['Vector']):
                return True
    return False


def wire_basis(tree):
    if tree.bl_idname!='ShaderNodeTree' or not needs_basis(tree):return
    for n in tree.nodes:
        if n.type=='ATTRIBUTE':
            for i,key in enumerate(BASIS):
                if 'ruri_face_basis'+str(i) in n.attribute_name or 'cmb_face_basis'+str(i) in n.attribute_name:
                    n.attribute_type='OBJECT';n.attribute_name='["'+key+'"]';n.label='CMB Rig Basis '+str(i)
    groups=[n for n in tree.nodes if n.type=='GROUP' and all(n.inputs.get(k) is not None for k in SOCKETS)]
    for i,key in enumerate(BASIS):
        if not groups:break
        node=next((n for n in tree.nodes if n.type=='ATTRIBUTE' and n.attribute_name=='["'+key+'"]'),None)
        if node is None:node=tree.nodes.new('ShaderNodeAttribute')
        node.attribute_type='OBJECT';node.attribute_name='["'+key+'"]';node.label='CMB Rig Basis '+str(i)
        for group in groups:tree.links.new(node.outputs['Vector'],group.inputs[SOCKETS[i]])


def object_needs_basis(obj):
    from . import core
    return any(needs_basis(t) for m in obj.data.materials if m for t in core.walk_trees(m.node_tree))


def armature_of(obj):
    arms={m.object for m in obj.modifiers if m.type=='ARMATURE' and m.object and m.object.type=='ARMATURE'}
    if len(arms)==1:return next(iter(arms))
    if not arms and obj.parent and obj.parent.type=='ARMATURE':return obj.parent
    return None


def head_of(arm):
    def japanese(pose):
        md=getattr(pose,'mmd_bone',None)
        raw=pose.get('mmd_bone')
        return getattr(md,'name_j','') or (raw.get('name_j','') if hasattr(raw,'get') else '')
    candidates=[p.bone for p in arm.pose.bones if japanese(p)=='頭']
    if len(candidates)==1:return candidates[0]
    if candidates:return None
    candidates=[b for b in arm.data.bones if b.name.casefold() in {'頭','头','head','bip001_head'}]
    return candidates[0] if len(candidates)==1 else None


def binding_updated(settings, context):
    obj=settings.id_data
    arm=settings.armature;bone=arm.data.bones.get(settings.bone) if arm else None
    if arm is not None:obj['cmb_rig_armature']=arm
    else:obj.pop('cmb_rig_armature',None)
    obj['cmb_rig_bone_name']=settings.bone
    if bone:
        if not bone.get(BONE_ID):bone[BONE_ID]=uuid.uuid4().hex
        obj['cmb_rig_bone_id']=bone[BONE_ID]
    else:obj.pop('cmb_rig_bone_id',None)
    from . import runtime
    runtime.invalidate()


def initialize(obj):
    arm=obj.get('cmb_rig_armature') or armature_of(obj)
    if not isinstance(arm,bpy.types.Object) or arm.type!='ARMATURE':arm=None
    if arm and (arm.library or arm.data.library):
        return [obj.name+'：骨架为链接数据，请先本地化再绑定头骨']
    existing=obj.get('cmb_rig_bone_id')
    bone=next((b for b in arm.data.bones if b.get(BONE_ID)==existing),None) if arm and existing else None
    if bone is None and arm:bone=head_of(arm)
    settings=getattr(obj,'cmb_rig_binding',None)
    if settings:
        settings.armature=arm;settings.bone=bone.name if bone else ''
    else:
        if arm:obj['cmb_rig_armature']=arm
        if bone:
            if not bone.get(BONE_ID):bone[BONE_ID]=uuid.uuid4().hex
            obj['cmb_rig_bone_name']=bone.name;obj['cmb_rig_bone_id']=bone[BONE_ID]
    if not bone:return [obj.name+'：头骨未确定，请在独立同步面板指定；暂用单位基座']
    return []


def basis_values(obj, arm, pose, depsgraph, delta=None):
    target=obj.evaluated_get(depsgraph)
    evaluated=arm.evaluated_get(depsgraph)
    if delta is None:delta=pose.matrix.to_3x3() @ pose.bone.matrix_local.to_3x3().inverted()
    relative=(target.matrix_world.inverted() @ evaluated.matrix_world).to_3x3()
    delta=relative @ delta @ relative.inverted()
    unity=SWAP @ delta @ SWAP
    return [tuple(unity.col[i].normalized()) for i in range(3)]


def bindings_for(objects):
    """Resolve each armature's UUID table once after a binding change."""
    result = []
    by_arm = {}
    for obj in objects:
        if obj.get('cmb_basis_enabled') is False:continue
        if obj.get('cmb_basis_enabled') is None and not obj.get('cmb_rig_bone_name') and not object_needs_basis(obj):continue
        arm=obj.get('cmb_rig_armature');uid=obj.get('cmb_rig_bone_id')
        if not isinstance(arm,bpy.types.Object) or arm.type!='ARMATURE':arm=None
        if arm and arm not in by_arm:
            by_arm[arm]={b.get(BONE_ID):b for b in arm.data.bones if b.get(BONE_ID)}
        bone=by_arm[arm].get(uid) if arm and uid else None
        result.append((obj,arm,bone))
    return result


def sync_scene(scene,depsgraph=None,objects=None,bindings=None):
    from . import lighting
    report={'objects':0,'evaluated_writes':0,'warnings':[]}
    if not scene.get('cmb_runtime_ready'):return report
    from . import runtime
    depsgraph = runtime.scene_depsgraph(scene, depsgraph)
    if bindings is None:bindings=bindings_for(objects if objects is not None else lighting.targets(scene))
    deltas={}
    for obj,arm,bone in bindings:
        if obj.get('cmb_runtime_owner')!=scene.get('cmb_runtime_id'):continue
        values=[(1.,0.,0.),(0.,1.,0.),(0.,0.,1.)]
        if bone:
            pose=arm.evaluated_get(depsgraph).pose.bones.get(bone.name)
            if pose is None:
                report['warnings'].append(obj.name+'：求值头骨缺失，保留上次有效值');continue
            try:
                key=(arm.as_pointer(),bone.as_pointer())
                if key not in deltas:deltas[key]=pose.matrix.to_3x3() @ pose.bone.matrix_local.to_3x3().inverted()
                values=basis_values(obj,arm,pose,depsgraph,deltas[key])
            except (ValueError,ZeroDivisionError):
                report['warnings'].append(obj.name+'：基座矩阵不可逆，保留上次有效值');continue
            if obj.get('cmb_rig_bone_name')!=bone.name:obj['cmb_rig_bone_name']=bone.name
            settings=getattr(obj,'cmb_rig_binding',None)
            if settings and settings.bone!=bone.name:settings.bone=bone.name
        else:report['warnings'].append(obj.name+'：头骨绑定缺失，使用单位基座')
        changed=False
        evaluated=obj.evaluated_get(depsgraph)
        for key,value in zip(BASIS,values):
            old=obj.get(key)
            if old is None or len(old)!=len(value) or max(abs(a-b) for a,b in zip(old,value))>1e-6:
                obj[key]=value;changed=True
            # The render/viewport reads the evaluated object from this same frame.
            old=evaluated.get(key)
            if old is None or len(old)!=len(value) or max(abs(a-b) for a,b in zip(old,value))>1e-6:
                evaluated[key]=value
                report['evaluated_writes']+=1
        report['objects']+=changed
    return report
