"""EEVEE image checks for native light and World parameter propagation."""
import json
import math
import os
from pathlib import Path
import sys

import bpy
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import character_material_bridge as addon
from character_material_bridge import lighting,runtime,native53,environment53
OUT=Path(os.environ.get('CMB_TEST_OUT',ROOT/'test_output/blender53/render'))
OUT.mkdir(parents=True,exist_ok=True)
addon.register()
scene=bpy.context.scene
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
scene.render.engine='BLENDER_EEVEE'
scene.render.resolution_x=64;scene.render.resolution_y=64;scene.render.resolution_percentage=100
scene.render.image_settings.file_format='OPEN_EXR';scene.render.image_settings.color_depth='32'
scene.render.film_transparent=True
bpy.ops.object.camera_add(location=(0,0,3));scene.camera=bpy.context.object
scene.camera.data.type='ORTHO';scene.camera.data.ortho_scale=2.5
bpy.ops.mesh.primitive_plane_add(size=2)
obj=bpy.context.object;obj['cmb_role']='result'
world=bpy.data.worlds.new('CMB World QA');world.use_nodes=True;scene.world=world
background=next(n for n in world.node_tree.nodes if n.type=='BACKGROUND')
background.inputs['Color'].default_value=(.2,.3,.4,1);background.inputs['Strength'].default_value=.5

checks=[]
def check(name,okay,detail=None):
    assert okay,(name,detail)
    checks.append({'name':name,'status':'PASS','detail':detail})
    print('PASS',name,flush=True)
def render(name):
    path=OUT/(name+'.exr');scene.render.filepath=str(path)
    bpy.ops.render.render(write_still=True)
    image=bpy.data.images.load(str(path),check_existing=False)
    index=(32*64+32)*4
    pixel=list(image.pixels[index:index+4]);bpy.data.images.remove(image)
    return pixel
def close(a,b,tolerance=2e-3):return max(abs(x-y) for x,y in zip(a,b))<tolerance
def mat(name):
    m=bpy.data.materials.new(name);m.use_nodes=True;m[native53.MARKER]=1;m['ruri_uber_part']='Standard'
    m.node_tree.nodes.clear();obj.data.materials.clear();obj.data.materials.append(m)
    return m,m.node_tree
def emission(tree,color):
    emit=tree.nodes.new('ShaderNodeEmission');output=tree.nodes.new('ShaderNodeOutputMaterial')
    tree.links.new(color,emit.inputs['Color']);tree.links.new(emit.outputs[0],output.inputs['Surface'])

m,tree=mat('CMB Native Flat World')
group=bpy.data.node_groups.new('CMB World Capability','ShaderNodeTree')
group.interface.new_socket(name='C1_AmbientIrradiance',in_out='INPUT',socket_type='NodeSocketVector')
group.interface.new_socket(name='Color',in_out='OUTPUT',socket_type='NodeSocketColor')
gi=group.nodes.new('NodeGroupInput');go=group.nodes.new('NodeGroupOutput')
group.links.new(gi.outputs[0],go.inputs[0])
node=tree.nodes.new('ShaderNodeGroup');node.node_tree=group;emission(tree,node.outputs[0])
lighting.initialize(scene)
runtime.synchronize(scene,force=True)
first=render('world_initial')
check('native_world_rna_color_strength',close(first[:3],[.1,.15,.2]),first)
rebinds=environment53.STATS['material_rebinds']
background.inputs['Color'].default_value=(.8,.2,.1,1);background.inputs['Strength'].default_value=.75
second=render('world_edited')
check('native_world_live_parameter_edit',close(second[:3],[.6,.15,.075]),second)
check('world_value_edit_no_python_rebind',environment53.STATS['material_rebinds']==rebinds)
background.name='Renamed World Background'
runtime.synchronize(scene,force=True)
check('world_node_rename',close(render('world_renamed'),second))

settings=scene.cmb_runtime_settings
check('world_control_default_preserves_existing_files',settings.use_world_environment)
world_before=(tuple(background.inputs['Color'].default_value),background.inputs['Strength'].default_value)
settings.environment_color=(.2,.6,.8);settings.environment_strength=.25
settings.use_world_environment=False
check('independent_rgb_strength',close(render('manual_rgb')[:3],[.05,.15,.2]))
check('independent_control_preserves_world',world_before==(
    tuple(background.inputs['Color'].default_value),background.inputs['Strength'].default_value))
rebinds=environment53.STATS['material_rebinds']
background.inputs['Color'].default_value=(.9,.8,.7,1);background.inputs['Strength'].default_value=3
check('world_edit_ignored_when_disabled',close(render('manual_ignores_world')[:3],[.05,.15,.2]))
check('ignored_world_edit_no_rebind',environment53.STATS['material_rebinds']==rebinds)
settings.preview_sync=False
syncs=runtime.STATS['parameter_syncs']
settings.environment_color=(.4,.1,.2);settings.environment_strength=2
check('manual_edit_immediate_while_preview_paused',
      close(scene[environment53.COLOR][:3],[.4,.1,.2]) and scene[environment53.STRENGTH]==2)
check('environment_edit_does_not_flush_other_parameters',runtime.STATS['parameter_syncs']==syncs)
check('manual_rgb_edit_no_rebind',environment53.STATS['material_rebinds']==rebinds)
check('manual_rgb_edit_render',close(render('manual_edited')[:3],[.8,.2,.4]))
settings.environment_strength=0
check('zero_environment_strength',close(render('manual_zero')[:3],[0,0,0]))
settings.environment_strength=2
scene.world=None
check('manual_control_without_world',close(render('manual_no_world')[:3],[.8,.2,.4]))
scene.world=world
mix=world.node_tree.nodes.new('ShaderNodeMixShader')
world_output=next(n for n in world.node_tree.nodes if n.type=='OUTPUT_WORLD')
world.node_tree.links.new(mix.outputs[0],world_output.inputs['Surface'])
check('manual_control_with_procedural_world',close(render('manual_procedural_world')[:3],[.8,.2,.4])
      and not environment53.LAST_WARNING)
world.node_tree.nodes.remove(mix)
world.node_tree.links.new(background.outputs[0],world_output.inputs['Surface'])
settings.use_world_environment=True
check('world_control_reenabled_while_paused',close(render('world_reenabled')[:3],[2.7,2.4,2.1]))
check('manual_settings_retained_on_toggle',close(settings.environment_color,[.4,.1,.2]) and settings.environment_strength==2)
settings.preview_sync=True

image=bpy.data.images.new('CMB World HDR QA',width=16,height=8,float_buffer=True)
image.colorspace_settings.name='Non-Color';image.alpha_mode='CHANNEL_PACKED'
pixels=[]
for y in range(8):
    for x in range(16):pixels.extend((x/16,y/8,.2,1))
image.pixels.foreach_set(pixels);image.update();image.pack()
world_tex=world.node_tree.nodes.new('ShaderNodeTexEnvironment');world_tex.image=image
world_mapping=world.node_tree.nodes.new('ShaderNodeMapping')
world_mapping.inputs['Rotation'].default_value=(0,.25,0)
world.node_tree.links.new(world_mapping.outputs['Vector'],world_tex.inputs['Vector'])
world.node_tree.links.new(world_tex.outputs['Color'],background.inputs['Color'])
background.inputs['Strength'].default_value=.4
m,tree=mat('CMB Native HDR World')
geometry=tree.nodes.new('ShaderNodeNewGeometry')
mapping=tree.nodes.new('ShaderNodeMapping');mapping.inputs['Rotation'].default_value=(0,.25,0)
tex=tree.nodes.new('ShaderNodeTexEnvironment');tex.image=image
tree.links.new(geometry.outputs['Normal'],mapping.inputs['Vector']);tree.links.new(mapping.outputs['Vector'],tex.inputs['Vector'])
scale=tree.nodes.new('ShaderNodeVectorMath');scale.operation='SCALE';scale.inputs['Scale'].default_value=.4
tree.links.new(tex.outputs['Color'],scale.inputs[0]);emission(tree,scale.outputs[0])
baseline=render('hdri_authored')
settings.use_world_environment=False
environment53.capture(m,m,scene)
check('world_sampler_provenance_while_manual',bool(m.get(environment53.KEY)))
runtime.invalidate(scene);runtime.synchronize(scene,force=True)
check('hdri_native_binding_created',bool(tree.nodes.get(environment53.PREFIX+'Radiance '+tex.name)))
check('hdri_replaced_by_independent_rgb',close(render('hdri_manual')[:3],[.8,.2,.4]))
settings.use_world_environment=True
bound=render('hdri_native')
check('hdri_binding_preserves_image',close(bound,baseline),{'authored':baseline,'native':bound})
rebinds=environment53.STATS['material_rebinds']
world_mapping.inputs['Rotation'].default_value=(0,-.7,0);background.inputs['Strength'].default_value=.8
changed=render('hdri_edited')
check('hdri_rotation_strength_live',not close(changed,bound),{'before':bound,'after':changed})
check('hdri_value_edit_no_python_rebind',environment53.STATS['material_rebinds']==rebinds)
node_count=len(tree.nodes)
for _ in range(3):
    settings.use_world_environment=False
    settings.use_world_environment=True
check('repeated_toggle_keeps_hdri_graph',len(tree.nodes)==node_count and close(render('hdri_retoggled'),changed))
hdr_material=m

# Scene settings use Blender's evaluated animation values for frames and renders.
settings.use_world_environment=False
settings.environment_color=(.2,.4,.6);settings.environment_strength=1
settings.keyframe_insert('environment_color',frame=1);settings.keyframe_insert('environment_strength',frame=1)
settings.environment_color=(.6,.2,.2);settings.environment_strength=3
settings.keyframe_insert('environment_color',frame=11);settings.keyframe_insert('environment_strength',frame=11)
scene.frame_set(6)
animated=render('manual_animated')
check('manual_environment_animation',close(animated[:3],[.8,.6,.8]),
      {'pixel':animated,'color':list(settings.environment_color),'strength':settings.environment_strength,
       'published_color':list(scene[environment53.COLOR]),'published_strength':scene[environment53.STRENGTH]})
scene.animation_data_clear()
settings.environment_color=(.3,.5,.7);settings.environment_strength=.5
check('manual_settings_after_animation',close(render('manual_after_animation')[:3],[.15,.25,.35]))

m,tree=mat('CMB Native Sun')
info=tree.nodes.new('ShaderNodeLightInfo');evaluation=tree.nodes.new('ShaderNodeLightEvaluation')
power=tree.nodes.new('ShaderNodeMath');power.operation='MULTIPLY'
tree.links.new(info.outputs['Power'],power.inputs[0]);power.inputs[1].default_value=math.pi
factor=tree.nodes.new('ShaderNodeMath');factor.operation='MULTIPLY'
tree.links.new(power.outputs[0],factor.inputs[0]);tree.links.new(evaluation.outputs['Factor'],factor.inputs[1])
scale=tree.nodes.new('ShaderNodeVectorMath');scale.operation='SCALE'
tree.links.new(info.outputs['Color'],scale.inputs[0]);tree.links.new(factor.outputs[0],scale.inputs['Scale'])
accum=tree.nodes.new('ShaderNodeLightAccumulation')
tree.links.new(scale.outputs[0],accum.inputs['Diffuse Light']);accum.inputs['Diffuse Color'].default_value=(1,1,1,1)
output=tree.nodes.new('ShaderNodeOutputMaterial');tree.links.new(accum.outputs[0],output.inputs['Surface'])
light_data=bpy.data.lights.new('CMB Sun','SUN');light_data.energy=1;light_data.color=(.8,.4,.2)
sun=bpy.data.objects.new('CMB Sun',light_data);scene.collection.objects.link(sun)
runtime.invalidate(scene);runtime.synchronize(scene,force=True)
calls=runtime.STATS['light_syncs']
initial=render('sun_initial');light_data.energy=2
bright=render('sun_energy');sun.rotation_euler.y=math.radians(60)
rotated=render('sun_rotated')
check('native_sun_energy',initial[0]>.01 and close(bright[:3],[v*2 for v in initial[:3]],.02),{'initial':initial,'bright':bright})
check('native_sun_rotation',0<rotated[0]<bright[0]*.8,{'bright':bright,'rotated':rotated})
check('sun_changes_without_script_light_table',runtime.STATS['light_syncs']==calls and scene.get('cmb_light_table') is None)

# Save and reopen only the isolated fixture; the user's session is not touched.
obj.data.materials.clear();obj.data.materials.append(hdr_material)
runtime.invalidate(scene)
settings.preview_sync=False
bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'independent_environment.blend'))
bpy.ops.wm.open_mainfile(filepath=str(OUT/'independent_environment.blend'))
scene=bpy.context.scene;settings=scene.cmb_runtime_settings
check('environment_settings_persist',not settings.use_world_environment and
      close(settings.environment_color,[.3,.5,.7]) and abs(settings.environment_strength-.5)<1e-6)
check('manual_environment_reload_image',close(render('manual_reloaded')[:3],[.15,.25,.35]))
settings.use_world_environment=True
check('hdri_restored_after_reload',close(render('hdri_reloaded'),changed))

settings.use_world_environment=False
bpy.context.preferences.edit.use_global_undo=True
bpy.ops.ed.undo_push(message='Independent environment')
settings.environment_color=(.8,.6,.4);settings.environment_strength=2
settings.use_world_environment=True
bpy.ops.ed.undo_push(message='World environment')
assert bpy.ops.ed.undo()=={'FINISHED'}
scene=bpy.context.scene;settings=scene.cmb_runtime_settings
check('environment_undo_while_paused',not settings.use_world_environment and
      close(scene[environment53.COLOR][:3],[.3,.5,.7]) and scene[environment53.STRENGTH]==.5 and
      close(render('manual_undo')[:3],[.15,.25,.35]))
assert bpy.ops.ed.redo()=={'FINISHED'}
scene=bpy.context.scene;settings=scene.cmb_runtime_settings
check('environment_redo_while_paused',settings.use_world_environment and
      close(settings.environment_color,[.8,.6,.4]) and settings.environment_strength==2 and
      close(render('world_redo'),changed))
result={'status':'PASS','blender':bpy.app.version_string,'checks':checks,'environment_stats':dict(environment53.STATS),
        'boundary':'Synthetic image correctness; no claim of game visual equivalence or viewport FPS'}
(OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print('BLENDER53_RENDER_PASS',len(checks),flush=True)
