"""Install and exercise either release ZIP in the runner's temporary profile."""
import importlib
import json
import os
from pathlib import Path
import sys
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
assert tuple(addon.bl_info['version'])==(0,6,0)
assert tuple(addon.bl_info['blender'])==(5,3,0)
assert Path(addon.__file__).is_relative_to(Path(os.environ['BLENDER_USER_SCRIPTS']))
mat=bpy.data.materials.new('CMB Installed Native');mat.use_nodes=True;mat[native.MARKER]=1
mat['ruri_uber_part']='Standard'
group=bpy.data.node_groups.new('CMB Installed Group','ShaderNodeTree')
group.interface.new_socket(name='_BumpScale',in_out='INPUT',socket_type='NodeSocketFloat')
node=mat.node_tree.nodes.new('ShaderNodeGroup');node.node_tree=group
native.write(mat,[('Standard','_BumpScale',2.0)])
assert native.read(mat,'_BumpScale')==2.0
channel=addon.animation.ensure(bpy.context.scene,None,0,'Standard','_BumpScale',2.0,material=mat)
channel.fval=3.0
assert native.read(mat,'_BumpScale')==3.0
if module_name.endswith('_user'):
    assert not hasattr(addon,'CMB_OT_export')
    assert not hasattr(addon.core,'export_package')
addon_utils.disable(module_name,default_set=True)
addon_utils.enable(module_name,default_set=True)
assert addon_utils.check(module_name)==(True,True)
result={'status':'PASS','module':module_name,'installed_zip':archive.name,'native_edit':True,
        'native_animation_channel':True,'disable_reenable':True,'blender':bpy.app.version_string}
(Path(os.environ['CMB_TEST_OUT'])/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print('ZIP_INSTALL_PASS',json.dumps(result))
