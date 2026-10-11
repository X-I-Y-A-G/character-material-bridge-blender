"""Deterministic CPU transport comparison; does not estimate viewport FPS."""
from array import array
import json
import os
from pathlib import Path
import statistics
import sys
import time
import bpy

ROOT=Path(__file__).resolve().parents[1]
args=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []
source=Path(args[0]) if args else ROOT
sys.path.insert(0,str(source))
from character_material_bridge import core,runtime
try:
    from character_material_bridge import native53
except ImportError:
    native53=None
OUT=Path(os.environ.get('CMB_TEST_OUT',ROOT/'test_output/blender53/benchmark'))
OUT.mkdir(parents=True,exist_ok=True)

def measure(fn,count=50):
    samples=[]
    for index in range(count+5):
        start=time.perf_counter();fn(index);elapsed=time.perf_counter()-start
        if index>=5:samples.append(elapsed*1000)
    return {'median_ms':statistics.median(samples),'p95_ms':sorted(samples)[int(len(samples)*.95)-1],'iterations':count}

image=bpy.data.images.new('Benchmark Uber Params',width=512,height=512,float_buffer=True)
image.alpha_mode='CHANNEL_PACKED';image.colorspace_settings.name='Non-Color'
image.pixels.foreach_set(array('f',[0])*len(image.pixels));image.update();image.pack()
packed=measure(lambda i:core.write_params_batch(image,[(1,[('Standard','_BumpScale',i/100),
                       ('Standard','_BaseColor',[.2,.3,.4,.5])])],pack=True))
result={'blender':bpy.app.version_string,'source':str(source),'packed_manual':packed,
        'configuration':{'legacy_image':[512,512,4],'edited_parameters':2,'native_group_instances':8,
                         'camera_objects':128,'metric':'Python/RNA CPU time; not rendered FPS'}}
result['packed_realtime']=measure(lambda i:core.write_params_batch(image,[(1,[('Standard','_BumpScale',i/100),
                             ('Standard','_BaseColor',[.2,.3,.4,.5])])],pack=False))
if native53:
    m=bpy.data.materials.new('CMB Benchmark Native');m.use_nodes=True;m[native53.MARKER]=1
    group=bpy.data.node_groups.new('CMB Benchmark Group','ShaderNodeTree')
    for name,kind in [('_BumpScale','NodeSocketFloat'),('_BaseColor','NodeSocketVector'),('_BaseColor_w','NodeSocketFloat')]:
        group.interface.new_socket(name=name,in_out='INPUT',socket_type=kind)
    for _ in range(8):
        node=m.node_tree.nodes.new('ShaderNodeGroup');node.node_tree=group
    result['native_manual']=measure(lambda i:native53.write(m,[('Standard','_BumpScale',i/100),
                                                            ('Standard','_BaseColor',[.2,.3,.4,.5])]))
    result['native_counters']=dict(native53.STATS)

scene=bpy.context.scene
camera=bpy.data.objects.new('Camera Benchmark',bpy.data.cameras.new('Camera Benchmark'))
scene.collection.objects.link(camera);scene.camera=camera;camera.location=(2,3,4)
objects=[]
for index in range(128):
    obj=bpy.data.objects.new('CMB Camera '+str(index),bpy.data.meshes.new('CMB Mesh'))
    scene.collection.objects.link(obj);obj.location=(index*.1,0,0);objects.append(obj)
graph=bpy.context.evaluated_depsgraph_get()
values=[]
def frame(_index):
    values.clear()
    shared=runtime.camera_frame(scene,camera,graph) if hasattr(runtime,'camera_frame') else None
    for obj in objects:
        values.append(runtime.camera_values(scene,camera,obj,graph,shared) if shared is not None
                      else runtime.camera_values(scene,camera,obj,graph))
result['camera_frame_128']=measure(frame)
result['camera_numeric_samples']=[{k:list(v) if hasattr(v,'__len__') else v for k,v in row.items()} for row in (values[0],values[-1])]
(OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k not in {'camera_numeric_samples','source'}},indent=2))
