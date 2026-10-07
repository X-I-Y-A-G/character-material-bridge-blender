"""Scene-owned installation of the approved native compositor asset."""
from pathlib import Path
import bpy

API_REVISION = 513
IMPLEMENTATION_REVISION = 526
NAME = '终末地通用后处理'
SCENE_TREE = 'Ruri Endfield Post Scene'
BACKUP = 'cmb_post_backup'
ASSET = Path(__file__).parent/'assets'/'endfield_post.blend'


def installed(scene):
    tree = getattr(scene, 'compositing_node_group', None)
    return bool(tree and tree.get('cmb_post_scene') == API_REVISION)


def stage_node(scene):
    if not installed(scene):return None
    return next((n for n in scene.compositing_node_group.nodes
                 if n.type == 'GROUP' and n.node_tree and n.node_tree.get('cmb_post_asset') == API_REVISION), None)


def unbound_render_inputs(scene):
    """Find the plugin input after Scene copies, deleted layers, or old installs."""
    stage = stage_node(scene)
    if stage is None:return []
    socket = stage.inputs.get('Image')
    return [link.from_node for link in socket.links
            if link.from_node.type == 'R_LAYERS' and (
                link.from_node.scene != scene or link.from_node.layer not in scene.view_layers)] if socket else []


def bind_render_input(node, scene):
    node.scene = scene
    if node.layer not in scene.view_layers:
        active = bpy.context.view_layer if bpy.context.scene == scene else None
        layer = active or next((layer for layer in scene.view_layers if layer.use), scene.view_layers[0])
        node.layer = layer.name


def _normalize_luts(pipeline):
    from . import core, color_management
    images = {node.image for tree in core.walk_trees(pipeline) for node in tree.nodes
              if isinstance(getattr(node, 'image', None), bpy.types.Image)
              and (node.image.get('cmb_post_lut') == 1
                   or node.image.name.startswith('RD2001 · 原始RGBA16F LUT32'))}
    for image in images:
        if not image.colorspace_settings.is_data:color_management.set_data(image)


def snapshot(scene):
    old = scene.compositing_node_group
    data = {'use_compositing':scene.render.use_compositing,
            'view_transform':scene.view_settings.view_transform, 'look':scene.view_settings.look,
            'exposure':scene.view_settings.exposure, 'gamma':scene.view_settings.gamma,
            'dither':scene.render.dither_intensity, 'precision':scene.render.compositor_precision}
    if old is not None:data['tree'] = old; data['tree_name'] = old.name
    return data


def restore_settings(scene, data):
    scene.compositing_node_group = data.get('tree')
    scene.render.use_compositing = data['use_compositing']
    scene.view_settings.view_transform = data['view_transform']; scene.view_settings.look = data['look']
    scene.view_settings.exposure = data['exposure']; scene.view_settings.gamma = data['gamma']
    scene.render.dither_intensity = data['dither']; scene.render.compositor_precision = data['precision']


def install(scene, force=False, automatic=False):
    if bpy.app.version < (5, 2, 0) or not hasattr(scene, 'compositing_node_group'):
        return {'status':'unsupported','message':'终末地后处理需要 Blender 5.2；材质移植仍可使用'}
    from . import color_management
    compatibility = color_management.postprocess_compatibility(scene)
    if compatibility['status'] != 'compatible':return compatibility
    if installed(scene) and not force:
        missing = unbound_render_inputs(scene)
        if missing and any(s != scene and s.compositing_node_group == scene.compositing_node_group
                           for s in bpy.data.scenes):
            return {'status':'blocked','message':'后处理树被多个场景共用，无法安全绑定渲染场景，请先使合成树独立'}
        for node in missing:bind_render_input(node, scene)
        stage = stage_node(scene)
        if stage is not None:_normalize_luts(stage.node_tree)
        report = {'status':'already_installed'}
        if missing:report['message'] = '已将渲染层绑定到当前场景，保留已有后处理调节'
        return report
    if automatic and scene.get('cmb_post_disabled'):
        return {'status':'disabled','message':'当前场景已手动停用终末地后处理'}
    current = scene.compositing_node_group
    if any(s != scene and installed(s) for s in bpy.data.scenes):
        return {'status':'blocked','message':'其他场景已占用 Importer 后处理兼容入口，保留当前场景合成'}
    reserved = bpy.data.node_groups.get(SCENE_TREE)
    if reserved is not None and (reserved != current or any(
            s != scene and s.compositing_node_group == reserved for s in bpy.data.scenes)):
        return {'status':'blocked','message':'Ruri 后处理树存在名称或多场景冲突，保留当前场景合成'}
    public = bpy.data.node_groups.get(NAME)
    if public is not None and public.get('cmb_post_asset') != API_REVISION:
        return {'status':'blocked','message':'已有同名自定义节点组「终末地通用后处理」，未覆盖'}
    if not ASSET.is_file():
        return {'status':'blocked','message':'缺少内置后处理资产，请重新安装完整插件包'}
    before = snapshot(scene)
    old_backup = scene.get(BACKUP)
    had_backup = old_backup is not None
    old_names = []
    groups_before = set(bpy.data.node_groups); images_before = set(bpy.data.images)
    try:
        if reserved is not None:
            old_names.append((reserved, reserved.name)); reserved.name = SCENE_TREE + ' · CMB备份'
        if public is not None:
            old_names.append((public, public.name)); public.name = NAME + ' · 参数备份'
        with bpy.data.libraries.load(str(ASSET), link=False) as (source, dest):
            if NAME not in source.node_groups:raise RuntimeError('内置节点资产缺少主节点组')
            dest.node_groups = [NAME]
        pipeline = dest.node_groups[0]; pipeline.name = NAME
        for image in set(bpy.data.images) - images_before:
            color_management.set_data(image)
            image['cmb_post_lut'] = 1
        tree = bpy.data.node_groups.new(SCENE_TREE, 'CompositorNodeTree')
        tree['cmb_post_scene'] = API_REVISION
        tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
        source = tree.nodes.new('CompositorNodeRLayers'); bind_render_input(source, scene); source.location = (-350, 150)
        stage = tree.nodes.new('CompositorNodeGroup'); stage.node_tree = pipeline; stage.name = NAME; stage.label = NAME; stage.width = 300
        output = tree.nodes.new('NodeGroupOutput'); output.location = (450, 150)
        tree.links.new(source.outputs['Image'],stage.inputs['Image']); tree.links.new(stage.outputs['Image'],output.inputs['Image'])
        if not had_backup:scene[BACKUP] = before
        scene.compositing_node_group = tree
        scene.render.use_compositing = True; scene.render.compositor_precision = 'FULL'
        scene.view_settings.view_transform = 'Standard'; scene.view_settings.look = 'None'
        scene.view_settings.exposure = 0; scene.view_settings.gamma = 1; scene.render.dither_intensity = 0
        scene['cmb_post_disabled'] = False
        return {'status':'installed','group':NAME,'asset_revision':pipeline.get('cmb_post_revision',513)}
    except Exception:
        restore_settings(scene, before)
        if not had_backup and BACKUP in scene:del scene[BACKUP]
        bpy.data.batch_remove(ids=(set(bpy.data.node_groups)-groups_before) | (set(bpy.data.images)-images_before))
        for item, name in old_names:item.name = name
        raise


def restore(scene):
    data = scene.get(BACKUP)
    if data is None:return {'status':'no_backup','message':'没有本插件保存的原后处理'}
    if not installed(scene):
        return {'status':'blocked','message':'当前合成已被其他工具替换，未覆盖；原备份仍保留'}
    tree = scene.compositing_node_group
    if any(s != scene and s.compositing_node_group == tree for s in bpy.data.scenes):
        return {'status':'blocked','message':'后处理树被多个场景共用，请先使合成树独立再恢复；原备份仍保留'}
    # A saved backup may belong to another OCIO config. Validate its enum
    # settings before changing the actual compositor or its compatibility name.
    probe = bpy.data.scenes.new('CMB restore compatibility probe')
    try:
        probe.display_settings.display_device = scene.display_settings.display_device
        probe.view_settings.view_transform = data['view_transform']
        probe.view_settings.look = data['look']
        probe.render.compositor_precision = data['precision']
    except Exception as exc:
        return {'status':'ocio_incompatible',
                'message':'无法恢复原后处理：当前配置不支持备份设置；现有合成与原备份均已保留。' + str(exc)}
    finally:
        bpy.data.scenes.remove(probe)
    before = snapshot(scene)
    tree_name = tree.name
    old = data.get('tree')
    old_name = data.get('tree_name')
    old_current_name = old.name if old is not None else None
    disabled = scene.get('cmb_post_disabled')
    backup = data.to_dict() if hasattr(data, 'to_dict') else dict(data)
    try:
        tree.name = NAME + ' · 已停用场景'
        restore_settings(scene, data)
        if old is not None and old_name and not bpy.data.node_groups.get(old_name):old.name = old_name
        del scene[BACKUP]; scene['cmb_post_disabled'] = True
    except Exception:
        restore_settings(scene, before)
        if old is not None:old.name = old_current_name
        tree.name = tree_name
        scene[BACKUP] = backup
        if disabled is None:scene.pop('cmb_post_disabled',None)
        else:scene['cmb_post_disabled'] = disabled
        raise
    return {'status':'restored'}
