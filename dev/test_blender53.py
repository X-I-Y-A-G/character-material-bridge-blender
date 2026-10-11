"""Native Blender 5.3 transport, animation, persistence and performance acceptance."""
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

import bpy
from _bpy_restrict_state import RestrictBlend

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import character_material_bridge as addon
from character_material_bridge import core, animation, lighting, runtime, native53, rig_runtime

OUT = Path(os.environ.get('CMB_TEST_OUT', ROOT/'test_output/blender53/native'))
OUT.mkdir(parents=True, exist_ok=True)
checks = []


def check(name, condition, detail=None):
    assert condition, (name, detail)
    checks.append({'name': name, 'status': 'PASS', 'detail': detail})
    print('PASS', name, flush=True)


def near(actual, expected, epsilon=1e-5):
    if isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(near(a, b, epsilon) for a, b in zip(actual, expected))
    return abs(float(actual)-float(expected)) < epsilon


def material_fixture(name='CMB Native Fixture', count=1):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material['ruri_uber_part'] = 'Standard'
    material['ruri_uber_stamp'] = 'new-native-schema'
    material[native53.MARKER] = 1
    tree = material.node_tree
    tree.nodes.clear()
    group = bpy.data.node_groups.new('CMB 5.3 Numeric Uniforms', 'ShaderNodeTree')
    for name, kind, default in [('_BaseColor','NodeSocketVector',(0.2,0.3,0.4)),
                                ('_BaseColor_w','NodeSocketFloat',0.6),
                                ('_BumpScale','NodeSocketFloat',1.0),
                                ('_CharacterParams11','NodeSocketVector',(1,2,3)),
                                ('_CharacterParams11_w','NodeSocketFloat',0.5),
                                ('reserved','NodeSocketVector',(7,8,9))]:
        socket = group.interface.new_socket(name=name,in_out='INPUT',socket_type=kind)
        socket.default_value = default
    group.interface.new_socket(name='Color',in_out='OUTPUT',socket_type='NodeSocketColor')
    inputs = group.nodes.new('NodeGroupInput')
    outputs = group.nodes.new('NodeGroupOutput')
    group.links.new(inputs.outputs['_BaseColor'],outputs.inputs['Color'])
    nodes = []
    for _ in range(count):
        node = tree.nodes.new('ShaderNodeGroup');node.node_tree = group;nodes.append(node)
    emission = tree.nodes.new('ShaderNodeEmission')
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    tree.links.new(nodes[0].outputs['Color'],emission.inputs['Color'])
    tree.links.new(emission.outputs[0],output.inputs['Surface'])
    material[native53.DEFAULTS] = json.dumps(native53.values(material))
    return material,nodes


with RestrictBlend():
    addon.register()
check('restricted_registration', hasattr(bpy.types.Scene,'cmb_anim_channels'))
scene = bpy.context.scene
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
mat, nodes = material_fixture(count=3)
bpy.ops.mesh.primitive_plane_add()
obj = bpy.context.object;obj.name='CMB Native Result';obj['cmb_role']='result';obj.data.materials.append(mat)
check('numeric_native_detection',native53.is_native(mat) and core.stamp_matches(mat))
before = native53.STATS['node_batches']
core.write_material_params(mat,[('Standard','_BaseColor',[0.6,0.5,0.4,0.3]),('Standard','_BumpScale',2.5)])
check('bulk_values_rgba',near(native53.read(mat,'_BaseColor'),[0.6,0.5,0.4,0.3]))
check('one_batch_per_group',native53.STATS['node_batches']-before==3)
check('unrelated_socket_preserved',all(near(n.inputs['reserved'].default_value,[7,8,9]) for n in nodes))
old = native53.values(mat)
try:
    native53.write(mat,[('Standard','_BumpScale',7),('Standard','_BaseColor',[1,2,math.nan,4])])
except RuntimeError:
    pass
else:
    raise AssertionError('Nonfinite edit accepted')
check('invalid_batch_is_atomic',old==native53.values(mat))
link_value=mat.node_tree.nodes.new('ShaderNodeValue')
link=mat.node_tree.links.new(link_value.outputs[0],nodes[-1].inputs['_BumpScale'])
try:
    native53.write(mat,[('Standard','_BumpScale',8)])
except RuntimeError:
    pass
else:
    raise AssertionError('Linked socket overwritten')
check('linked_input_is_protected',near(native53.read(mat,'_BumpScale'),2.5))
mat.node_tree.links.remove(link)

lighting.initialize(scene)
check('native_scene_needs_no_light_table',scene.get('cmb_native_lighting_only') and scene.get('cmb_light_table') is None)
check('native_light_sync_zero_upload',lighting.sync_scene(scene)['uploads']==0)
# A native surface may still have an old vertex-stage light-table reader.
old_table=bpy.data.images.new('RuriLightTable Hybrid QA',width=64,height=4,float_buffer=True)
geometry=bpy.data.node_groups.new('CMB Legacy Vertex Lighting','GeometryNodeTree')
reader=geometry.nodes.new('GeometryNodeImageTexture');reader.inputs['Image'].default_value=old_table
modifier=obj.modifiers.new('CMB Legacy Vertex Lighting','NODES');modifier.node_group=geometry
lighting.initialize(scene)
check('native_surface_legacy_vertex_light_table',not scene.get('cmb_native_lighting_only') and
      modifier.node_group.nodes[reader.name].inputs['Image'].default_value==scene['cmb_light_table'])
obj.modifiers.remove(modifier);lighting.initialize(scene)
addon.rebuild_param_rows(scene.cmb_params,bpy.context)
check('native_parameter_ui',len(scene.cmb_params.rows)==3 and addon.parameter_target_is_current(scene.cmb_params,bpy.context),len(scene.cmb_params.rows))
channel=animation.find(scene,None,0,'Standard','_BaseColor',material=mat)
check('native_animation_binding',channel is not None)
channel.cval=(0.1,0.2,0.3,0.4);channel.keyframe_insert('cval',frame=1)
channel.cval=(0.9,0.8,0.7,0.6);channel.keyframe_insert('cval',frame=11)
for curve in animation.action_curves(scene):
    for point in curve.keyframe_points:point.interpolation='LINEAR'
scene.frame_set(6)
animation.sync_scene(scene,bpy.context.evaluated_depsgraph_get(),force=True)
check('native_evaluated_animation',near(native53.read(mat,'_BaseColor'),[0.5,0.5,0.5,0.5]),native53.read(mat,'_BaseColor'))
scene.cmb_runtime_settings.preview_sync=False
frozen=native53.read(mat,'_BaseColor');scene.frame_set(8)
check('preview_pause_keeps_uniforms',near(native53.read(mat,'_BaseColor'),frozen))
channel=animation.find(scene,None,0,'Standard','_BumpScale',material=mat)
channel.fval=4
check('manual_edit_while_paused',near(native53.read(mat,'_BumpScale'),4))
check('paused_other_channel_unchanged',near(native53.read(mat,'_BaseColor'),frozen))
scene.cmb_runtime_settings.preview_sync=True
scene.frame_set(6)
preset=core.build_params_preset([obj])
check('native_preset_snapshot',next(iter(preset['mats'].values()))['transport']=='native53')
core.write_material_params(mat,[('Standard','_BumpScale',9)])
report=core.apply_params_preset(preset)
check('native_preset_restore',report['applied']==1 and near(native53.read(mat,'_BumpScale'),4),report)
builder=core.LibraryBuilder();copy=builder.clone(mat)
check('native_clone_values',native53.values(copy)==native53.values(mat))
check('native_clone_no_importer_owner',native53.is_native(copy) and 'ruri_uber_stack' not in copy)

# New shader bases are world-space rotations. A rotated target mesh must not
# introduce an extra inverse object transform; old packed shaders keep that path.
bpy.ops.object.armature_add()
arm=bpy.context.object;arm.data.bones[0].name='頭';arm.rotation_euler=(.1,.2,.3)
arm.pose.bones[0].rotation_mode='XYZ';arm.pose.bones[0].rotation_euler=(.2,.4,.1)
basis_material=bpy.data.materials.new('CMB World Basis');basis_material.use_nodes=True
basis_material[native53.MARKER]=1;basis_material['ruri_uber_part']='Standard'
basis_group=bpy.data.node_groups.new('CMB World Basis Inputs','ShaderNodeTree')
for socket_name in rig_runtime.SOCKETS:
    basis_group.interface.new_socket(name=socket_name,in_out='INPUT',socket_type='NodeSocketVector')
query=basis_material.node_tree.nodes.new('ShaderNodeGroup');query.node_tree=basis_group
targets=[]
for angle in (.7,-1.2):
    target=bpy.data.objects.new('CMB Rotated Basis Target',bpy.data.meshes.new('CMB Basis Mesh'))
    scene.collection.objects.link(target);target['cmb_role']='result';target.rotation_euler.z=angle
    target.data.materials.append(basis_material)
    modifier=target.modifiers.new('Armature','ARMATURE');modifier.object=arm
    targets.append(target)
lighting.initialize(scene)
graph=bpy.context.evaluated_depsgraph_get()
rig_runtime.sync_scene(scene,graph)
evaluated_arm=arm.evaluated_get(graph);pose=evaluated_arm.pose.bones[0]
world=evaluated_arm.matrix_world.to_3x3()
expected=rig_runtime.SWAP @ world @ pose.matrix.to_3x3() @ pose.bone.matrix_local.to_3x3().inverted() @ world.inverted() @ rig_runtime.SWAP
expected=[list(expected.col[index].normalized()) for index in range(3)]
check('native_world_rig_basis',all(near(target[key],expected[index])
    for target in targets for index,key in enumerate(rig_runtime.WORLD_BASIS)))
check('native_basis_independent_of_mesh_rotation',all(near(targets[0][key],list(targets[1][key])) for key in rig_runtime.WORLD_BASIS))
legacy=rig_runtime.basis_values(targets[0],arm,pose,graph)
check('world_vs_legacy_basis_discriminator',any(not near(legacy[index],expected[index]) for index in range(3)))
check('native_basis_attribute_wiring',all(query.inputs[name].links[0].from_node.attribute_name=='["'+key+'"]'
    for name,key in zip(rig_runtime.SOCKETS,rig_runtime.WORLD_BASIS)))

path=OUT/'native_roundtrip.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(path))
name=mat.name
bpy.ops.wm.open_mainfile(filepath=str(path))
mat=bpy.data.materials[name]
check('native_save_reload',near(native53.read(mat,'_BumpScale'),4))
check('native_saved_channels',any(c.material==mat for c in bpy.context.scene.cmb_anim_channels))

# Warm, repeated measurements report CPU transport cost, not viewport FPS.
mat,nodes=material_fixture('CMB Native Benchmark',count=8)
updates=[('Standard','_BaseColor',[.1,.2,.3,.4]),('Standard','_BumpScale',1.0),('Standard','_CharacterParams11',[1,2,3,4])]
native53.write(mat,updates)
samples=[]
for iteration in range(80):
    updates[1]=('Standard','_BumpScale',float(iteration+1))
    start=time.perf_counter();native53.write(mat,updates);samples.append(time.perf_counter()-start)
results={'status':'PASS','blender':bpy.app.version_string,'checks':checks,
         'native_edit_median_ms':statistics.median(samples)*1000,'native_stats':dict(native53.STATS),
         'measurement':'Python/RNA update cost; not rendering FPS'}
(OUT/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print('BLENDER53_PASS',len(checks),json.dumps({'native_edit_median_ms':results['native_edit_median_ms']}),flush=True)
