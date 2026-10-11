"""Install and exercise either release ZIP in the runner's temporary profile."""
import importlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import bpy
import addon_utils

args=sys.argv[sys.argv.index('--')+1:]
archive=Path(args[0]).resolve()
module_name='character_material_bridge_user' if '_user-' in archive.name else 'character_material_bridge'
assert bpy.ops.preferences.addon_install(filepath=str(archive),overwrite=False)=={'FINISHED'}
addon_utils.enable(module_name,default_set=True)
assert addon_utils.check(module_name)==(True,True)
addon=importlib.import_module(module_name)
native=importlib.import_module(module_name+'.native53')
environment=importlib.import_module(module_name+'.environment53')
assert tuple(addon.bl_info['version'])==(0,6,1)
assert tuple(addon.bl_info['blender'])==(5,3,0)
assert Path(addon.__file__).is_relative_to(Path(os.environ['BLENDER_USER_SCRIPTS']))
mat=bpy.data.materials.new('CMB Installed Native');mat.use_nodes=True;mat[native.MARKER]=1
mat['ruri_uber_part']='Standard'
group=bpy.data.node_groups.new('CMB Installed Group','ShaderNodeTree')
group.interface.new_socket(name='_BumpScale',in_out='INPUT',socket_type='NodeSocketFloat')
group.interface.new_socket(name='C1_AmbientIrradiance',in_out='INPUT',socket_type='NodeSocketVector')
node=mat.node_tree.nodes.new('ShaderNodeGroup');node.node_tree=group
native.write(mat,[('Standard','_BumpScale',2.0)])
assert native.read(mat,'_BumpScale')==2.0
channel=addon.animation.ensure(bpy.context.scene,None,0,'Standard','_BumpScale',2.0,material=mat)
channel.fval=3.0
assert native.read(mat,'_BumpScale')==3.0
scene=bpy.context.scene
obj=bpy.context.object;obj['cmb_role']='result';obj.data.materials.append(mat)
addon.lighting.initialize(scene)
settings=scene.cmb_runtime_settings
assert settings.use_world_environment

class LayoutProbe:
    """Exercise the real panel branches and validate their RNA property names."""
    def __init__(self):self.properties=[]
    def prop(self,owner,name,**kwargs):
        assert name in owner.bl_rna.properties
        self.properties.append(name)
    def label(self,**kwargs):
        if 'icon' in kwargs:
            assert kwargs['icon'] in bpy.types.UILayout.bl_rna.functions['label'].parameters['icon'].enum_items
    def box(self):return self
    def column(self):return self
    def operator(self,*args,**kwargs):return SimpleNamespace()

def visible_controls():
    layout=LayoutProbe()
    addon.CMB_PT_runtime.draw(SimpleNamespace(layout=layout),SimpleNamespace(scene=scene,object=obj))
    return layout.properties

assert 'use_world_environment' in visible_controls() and 'environment_color' not in visible_controls()
settings.use_world_environment=False
assert {'environment_color','environment_strength'}.issubset(visible_controls())
assert settings.bl_rna.properties['environment_color'].array_length==3
assert settings.bl_rna.properties['environment_color'].subtype=='COLOR'
settings.environment_color=(.2,.4,.6);settings.environment_strength=2
assert abs(scene[environment.COLOR][1]-.4)<1e-6 and scene[environment.STRENGTH]==2
if module_name.endswith('_user'):
    assert not hasattr(addon,'CMB_OT_export')
    assert not hasattr(addon.core,'export_package')
addon_utils.disable(module_name,default_set=True)
addon_utils.enable(module_name,default_set=True)
assert addon_utils.check(module_name)==(True,True)
assert not scene.cmb_runtime_settings.use_world_environment
assert scene.cmb_runtime_settings.environment_strength==2
result={'status':'PASS','module':module_name,'installed_zip':archive.name,'native_edit':True,
        'native_animation_channel':True,'environment_controls':True,'conditional_panel':True,
        'disable_reenable':True,'blender':bpy.app.version_string}
(Path(os.environ['CMB_TEST_OUT'])/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print('ZIP_INSTALL_PASS',json.dumps(result))
