"""Scene-derived uniforms for CMB-owned vertex snapshots.

Uses Ruri's object-local projection inputs, with scale-free camera orientation.
Never call its tree rebuilder: CMB owns the copied stack and smoothing edits.
Registration is safe under RestrictBlend; scene access starts after registration.
"""
import math
import traceback
import time
import threading
from collections import defaultdict

import bpy
from mathutils import Vector

API_REVISION = 527
INTERVAL = 1.0 / 30.0
ALL = frozenset({'PARAMETERS', 'RIG', 'CAMERA', 'LIGHTS'})
AUTO = frozenset({'VIEWPORT', 'FRAME'})
_states = {}
_pending = {}
_render_scenes = set()
_render_force = set()
_msg_owner = object()
_bootstrap_pending = False
_resume_pending = False
STATS = defaultdict(int)
CAMERA_INPUTS = ('cam_right', 'cam_up', 'cam_look', 'cam_pos',
                 'half_fov', 'screen_x', 'screen_y')
LAST_WARNING = ''
_busy = False
_registered = False
_last_error = ''
_coordinating = False


def _main_thread():
    return threading.current_thread() is threading.main_thread()


def _rendering():
    return bool(_render_scenes) or bpy.app.is_job_running('RENDER')


def prepare_render(scene):
    """Lock the UI before a render job starts, never from its worker callback.

    Native frame handlers mutate data shared with the viewport. Blender's
    documented protection is Render > Lock Interface, enabled for CMB scenes.
    This does not migrate bindings, scan node trees, or run during register().
    """
    if not _main_thread() or _rendering():
        return False
    managed = scene.get('cmb_runtime_ready') or len(getattr(scene, 'cmb_anim_channels', ()))
    if managed and not scene.render.use_lock_interface:
        scene.render.use_lock_interface = True
        return True
    return False


def scene_depsgraph(scene, depsgraph=None):
    """Handlers must supply their own graph; UI overrides are main-thread only."""
    if depsgraph is not None:
        try:
            original = depsgraph.scene.original
            mode = depsgraph.mode
        except (AttributeError, ReferenceError) as exc:
            raise RuntimeError('同步依赖图无效，未访问界面上下文') from exc
        if original == scene:
            if mode != 'RENDER' and (not _main_thread() or _rendering()):
                raise RuntimeError('渲染期间拒绝使用 VIEWPORT 依赖图')
            return depsgraph
    # temp_override changes the Python context dictionary used by WM events.
    # Accessing it from render_pre can race the UI event loop and crash Blender.
    if not _main_thread() or _rendering():
        raise RuntimeError('渲染同步缺少本场景的求值依赖图；已阻止访问界面上下文')
    layer = bpy.context.view_layer if bpy.context.scene == scene else scene.view_layers[0]
    with bpy.context.temp_override(scene=scene, view_layer=layer):
        return bpy.context.evaluated_depsgraph_get()


def _owned(obj):
    return obj.type == 'MESH' and obj.get('cmb_role', obj.get('jmb_role')) == 'result'


def _receiver(node):
    if node.type != 'GROUP' or node.node_tree is None:
        return False
    names = {s.name for s in node.inputs}
    return (set(CAMERA_INPUTS).issubset(names) and node.outputs.get('offset') is not None
            or 'input_camPos' in names and node.outputs.get('ret_positionWS') is not None)


def _has_receiver(tree, seen=None):
    seen = set() if seen is None else seen
    if tree in seen:
        return False
    seen.add(tree)
    return any(_receiver(n) or (n.type == 'GROUP' and n.node_tree is not None
                               and _has_receiver(n.node_tree, seen)) for n in tree.nodes)


def receivers(obj):
    """Localize only paths holding per-object uniforms, including linked duplicates."""
    def local(tree, seen):
        if tree in seen:
            return tree, []
        seen = seen | {tree}
        if tree.users > 1:
            tree = tree.copy()
            tree.use_fake_user = False
            if not tree.name.startswith('CMB '):
                tree.name = 'CMB ' + tree.name
        found = []
        for node in tree.nodes:
            if _receiver(node):
                found.append(node)
            elif node.type == 'GROUP' and node.node_tree and _has_receiver(node.node_tree):
                child, nested = local(node.node_tree, seen)
                if child != node.node_tree:
                    node.node_tree = child
                found.extend(nested)
        return tree, found
    found = []
    for mod in obj.modifiers:
        if (mod.type == 'NODES' and mod.node_group and mod.name.startswith('CMB ')
                and _has_receiver(mod.node_group)):
            tree, nodes = local(mod.node_group, set())
            if tree != mod.node_group:
                mod.node_group = tree
            found.extend(nodes)
    return found


def camera_values(scene, camera, obj, depsgraph):
    """Evaluated camera frame, excluding display scale as Blender projection does.

    Normalize the camera in WORLD space before converting to object space.
    Normalizing afterwards would discard the target mesh's required inverse
    scale. Camera position and target-object transform retain their full scale.
    """
    cam = camera.evaluated_get(depsgraph) if depsgraph else camera
    target = obj.evaluated_get(depsgraph) if depsgraph else obj
    inv = target.matrix_world.inverted()  # singular transforms must not silently pass
    inv3, cam3 = inv.to_3x3(), cam.matrix_world.normalized().to_3x3()
    if any(axis.length < 1e-12 for axis in cam3.col):
        raise ValueError('相机方向轴退化，请避免零缩放')
    render = scene.render
    proj = cam.calc_matrix_camera(depsgraph, x=render.resolution_x, y=render.resolution_y,
                                 scale_x=render.pixel_aspect_x, scale_y=render.pixel_aspect_y)
    scale = render.resolution_percentage / 100.0
    return {'cam_right': inv3 @ cam3.col[0], 'cam_up': inv3 @ cam3.col[1],
            'cam_look': inv3 @ (cam3 @ Vector((0, 0, -1))),
            'cam_pos': inv @ cam.matrix_world.translation,
            'half_fov': math.atan(1.0 / abs(proj[1][1])),
            'screen_x': float(render.resolution_x) * scale,
            'screen_y': float(render.resolution_y) * scale}


def _set(socket, value):
    if socket is None or socket.is_linked:
        return False
    animation = socket.id_data.animation_data
    path = socket.path_from_id('default_value')
    if animation and any(fc.data_path == path for fc in animation.drivers):
        return False
    old = socket.default_value
    a, b = (tuple(old), tuple(value)) if hasattr(value, '__len__') else ((old,), (value,))
    if all(abs(x-y) <= 1e-6 * max(1.0, abs(y)) for x, y in zip(a, b)):
        return False
    socket.default_value = value
    return True


def sync_scene(scene, depsgraph=None, objects=None, receiver_map=None):
    """Update uniforms only. Report unsupported camera states instead of inventing one."""
    global _busy, LAST_WARNING
    if _busy:
        return {'objects': 0, 'sockets': 0, 'warnings': []}
    report = {'objects': 0, 'sockets': 0, 'warnings': []}
    _busy = True
    try:
        targets = [(o, receiver_map[o.as_pointer()] if receiver_map is not None else receivers(o)) for o in (objects if objects is not None else scene.objects)
                   if _owned(o)]
        targets = [(o, nodes) for o, nodes in targets if nodes]
        if not targets:
            LAST_WARNING = ''
            return report
        camera = scene.camera
        if camera is None or camera.type != 'CAMERA':
            report['warnings'].append('描边未同步：请设置场景活动相机')
            LAST_WARNING = report['warnings'][0]
            return report
        depsgraph = scene_depsgraph(scene, depsgraph)
        if camera.data.type != 'PERSP':
            report['warnings'].append('当前描边沿用 Importer 的透视公式，正交/全景相机不保证正确')
        for obj, nodes in targets:
            try:
                values = camera_values(scene, camera, obj, depsgraph)
            except (ValueError, ZeroDivisionError) as exc:
                report['warnings'].append(obj.name + '：相机/对象变换不可逆，未同步 ' + str(exc))
                continue
            count = 0
            for node in nodes:
                for key, value in values.items():
                    count += _set(node.inputs.get(key), value)
                count += _set(node.inputs.get('input_camPos'), values['cam_pos'])
            if count:
                obj.update_tag(refresh={'DATA'})
                report['objects'] += 1
                report['sockets'] += count
        LAST_WARNING = '; '.join(report['warnings'])
        return report
    finally:
        _busy = False


def preview_enabled(scene):
    return getattr(getattr(scene, 'cmb_runtime_settings', None), 'preview_sync', True)


def _empty():
    return dict(objects=0, sockets=0, parameters=0, rig_objects=0,
                evaluated_writes=0, light_uploads=0, warnings=[])


def _counts(scene):
    return (len(scene.objects), len(bpy.data.objects), len(bpy.data.materials),
            len(bpy.data.node_groups), len(bpy.data.images),
            tuple((s.as_pointer(), len(s.objects)) for s in bpy.data.scenes))


def _object_binding(obj):
    return (obj.data.as_pointer() if obj.data else 0,
            tuple(s.material.as_pointer() if s.material else 0 for s in obj.material_slots),
            tuple((m.as_pointer(), m.name, m.node_group.as_pointer() if m.node_group else 0)
                  for m in obj.modifiers if m.type == 'NODES'),
            obj.get('cmb_role', obj.get('jmb_role')), obj.get('cmb_runtime_owner'), obj.get('cmb_basis_enabled'),
            obj.get('cmb_rig_armature'), obj.get('cmb_rig_bone_id'))


def _tree_binding(tree):
    # Values written by the coordinator are deliberately excluded. They cannot
    # invalidate the very bindings used to write them and start an idle loop.
    return (tuple((n.as_pointer(), n.node_tree.as_pointer() if n.type == 'GROUP' and n.node_tree else 0,
                   getattr(n, 'image', None),
                   tuple(s.default_value for s in n.inputs
                         if isinstance(getattr(s, 'default_value', None), bpy.types.Image)))
                  for n in tree.nodes),
            tuple((l.from_socket.as_pointer(), l.to_socket.as_pointer()) for l in tree.links))


def _camera_settings(scene):
    r = scene.render
    return (scene.camera, r.resolution_x, r.resolution_y, r.resolution_percentage,
            r.pixel_aspect_x, r.pixel_aspect_y)


def _state(scene):
    key = scene.as_pointer()
    state = _states.get(key)
    if state is None:
        state = dict(scene=scene, structure=True, param_full=True, objects=(),
                     receivers={}, rigs=(), trees={}, object_bindings={},
                     lights=(), counts=None, camera_settings=None,
                     warnings={}, last=0.0, channel_count=-1, scope_error='')
        _states[key] = state
    return state


def _build(scene, state):
    from . import core, lighting, rig_runtime
    STATS['binding_builds'] += 1
    _prune_scenes()
    objects = lighting.targets(scene)
    state['scope_error'] = ''
    if scene.get('cmb_runtime_ready'):
        try:
            lighting.validate_scope(scene, objects)
        except RuntimeError as exc:
            state['scope_error'] = str(exc)
    state['objects'] = objects
    # Scope rejection happens before localization or any texture/property write.
    if not state['scope_error']:
        state['receivers'] = {o.as_pointer(): receivers(o) for o in objects}
        state['rigs'] = rig_runtime.bindings_for(objects) if scene.get('cmb_runtime_ready') else ()
        state['trees'] = {t.as_pointer(): (t, _tree_binding(t))
                          for t in core.walk_trees_for_objects(objects)}
    state['object_bindings'] = {o.as_pointer(): _object_binding(o) for o in objects}
    state['lights'] = tuple(o for o in scene.objects if o.type == 'LIGHT')
    state['light_ids'] = {i.as_pointer() for o in state['lights'] for i in (o, o.data)}
    state['rig_ids'] = {i.as_pointer() for _, arm, _ in state['rigs'] if arm for i in (arm, arm.data)}
    state['materials'] = {s.material.as_pointer() for o in objects for s in o.material_slots if s.material}
    state['counts'] = _counts(scene)
    state['camera_settings'] = _camera_settings(scene)
    state['structure'] = False


def _prune_scenes():
    """Deleted paused scenes never rebuild, so prune before using cached RNA."""
    from . import animation
    live = {s.as_pointer() for s in bpy.data.scenes}
    invalid = (set(_states) | set(_pending)) - live
    # A freed ID's address can be reused by a new scene while the old Python
    # wrapper remains invalid. Membership of the pointer alone is insufficient.
    for key, cached in list(_states.items()):
        if key in invalid:continue
        try:
            if cached['scene'].as_pointer() != key:invalid.add(key)
        except ReferenceError:
            invalid.add(key)
    for key, (cached_scene, _) in list(_pending.items()):
        if key in invalid:continue
        try:
            if cached_scene.as_pointer() != key:invalid.add(key)
        except ReferenceError:
            invalid.add(key)
    for key in invalid:
        _states.pop(key, None)
        _pending.pop(key, None)
    _render_scenes.intersection_update(live - invalid)
    _render_force.intersection_update(live - invalid)
    animation.prune_scenes(live, invalid)


def invalidate(scene=None, parameters=False):
    """Mark bindings stale; rebuilding is deferred and never runs in register()."""
    from . import animation
    _prune_scenes()
    states = [_state(scene)] if scene is not None else list(_states.values())
    for state in states:
        if parameters:
            state['param_full'] = True
        else:
            state['structure'] = True
            state['param_full'] = True
            animation.invalidate(state['scene'])
        request(state['scene'], {'PARAMETERS'} if parameters else ALL)


def request(scene, categories=ALL):
    """Coalesce without postponing an already scheduled deadline."""
    if (not _registered or not preview_enabled(scene) or not _main_thread() or _rendering()):
        return
    key = scene.as_pointer()
    item = _pending.setdefault(key, (scene, set()))
    item[1].update(categories)
    if not bpy.app.timers.is_registered(_tick):
        delay = max(0.0, _state(scene)['last'] + INTERVAL - time.monotonic())
        bpy.app.timers.register(_tick, first_interval=delay)


def _resume_viewport():
    """Native job cleanup returns to the main thread before clearing job flags."""
    global _resume_pending
    if not _registered or not _resume_pending or not _main_thread() or _render_scenes:
        return
    _resume_pending = False
    if (_bootstrap_pending or _pending) and not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.0)


def preview_changed(settings, context):
    scene = settings.id_data
    _pending.pop(scene.as_pointer(), None)
    if _rendering():
        return
    if settings.preview_sync:
        _safe_sync(scene, force=True, reason='RESUME')
    elif not _pending and not _bootstrap_pending and bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)


def settings_changed(settings, context):
    request(settings.id_data, {'LIGHTS'})


def _safe_sync(scene, depsgraph=None, for_render=False, force=False, **kwargs):
    global LAST_WARNING, _last_error
    try:
        result = synchronize(scene, depsgraph, for_render=for_render, force=force, **kwargs)
        _last_error = ''
        return result
    except Exception as exc:
        LAST_WARNING = '角色运行同步失败：' + str(exc)
        if LAST_WARNING != _last_error:
            traceback.print_exc()
            _last_error = LAST_WARNING
        return dict(_empty(), warnings=[LAST_WARNING])


def synchronize(scene, depsgraph=None, for_render=False, force=False,
                reason='MANUAL', categories=None, channels=None):
    """One coordinator with explicit automatic, edit, frame and IO semantics."""
    from . import animation, lighting, rig_runtime
    global _coordinating, LAST_WARNING
    if _coordinating or (reason in AUTO and not for_render and not preview_enabled(scene)):
        return _empty()
    # for_render is also used by export to select render-visible lights. It is
    # not permission to obtain a graph from UI context while a job is running.
    render_frame = reason == 'RENDER' or (depsgraph is not None and depsgraph.mode == 'RENDER')
    if _rendering() and not render_frame:
        if reason in {'EXPORT', 'SAVE'}:
            raise RuntimeError('渲染进行中，不能同时保存或导出材质数据')
        return _empty()
    if render_frame:
        if depsgraph is None or depsgraph.mode != 'RENDER':
            raise RuntimeError('渲染同步需要当前帧的 RENDER 依赖图；未访问界面上下文')
        depsgraph = scene_depsgraph(scene, depsgraph)
    else:
        prepare_render(scene)
    _coordinating = True
    state = _state(scene)
    result = _empty()
    categories = set(ALL if categories is None else categories)
    if reason in {'EDIT', 'HISTORY'}:
        categories = {'PARAMETERS'}
    try:
        STATS['sync_calls'] += 1
        STATS['reason_' + reason] += 1
        if (force and not render_frame) or state['counts'] != _counts(scene):
            state['structure'] = True
        if state['structure']:
            _build(scene, state)
        if state['scope_error']:
            LAST_WARNING = state['scope_error']
            return dict(result, warnings=[state['scope_error']])
        count = len(getattr(scene, 'cmb_anim_channels', ()))
        if state['channel_count'] != count:
            state['param_full'] = True
            state['channel_count'] = count
        # An edit uses the exact RNA value entered by the user, independent of
        # the other channels' evaluated (possibly paused) animation values.
        if reason not in {'EDIT', 'HISTORY'}:
            depsgraph = scene_depsgraph(scene, depsgraph)
        else:
            depsgraph = None
        if 'PARAMETERS' in categories:
            STATS['parameter_syncs'] += 1
            animated_only = reason in AUTO and not for_render and not state['param_full']
            if reason == 'HISTORY':
                result['parameters'] = animation.restore_committed(scene)
            else:
                result['parameters'] = animation.safe_sync(
                    scene, depsgraph, force=force, channels=channels,
                    animated_only=animated_only, validated=True)
            state['warnings']['PARAMETERS'] = [animation.LAST_WARNING] if animation.LAST_WARNING else []
            if reason not in {'EDIT', 'HISTORY'} and not animated_only:
                state['param_full'] = False
        if 'RIG' in categories:
            STATS['rig_syncs'] += 1
            rigs = rig_runtime.sync_scene(scene, depsgraph, bindings=state['rigs'])
            result['rig_objects'] = rigs['objects']
            result['evaluated_writes'] = rigs['evaluated_writes']
            state['warnings']['RIG'] = rigs['warnings']
        if 'CAMERA' in categories:
            STATS['camera_syncs'] += 1
            camera = sync_scene(scene, depsgraph, objects=state['objects'], receiver_map=state['receivers'])
            result.update(objects=camera['objects'], sockets=camera['sockets'])
            state['warnings']['CAMERA'] = camera['warnings']
            state['camera_settings'] = _camera_settings(scene)
        if 'LIGHTS' in categories:
            STATS['light_syncs'] += 1
            lights = lighting.sync_scene(scene, depsgraph, for_render=for_render,
                                         force=force, candidates=state['lights'])
            result['light_uploads'] = lights['uploads']
            state['view_layer'] = depsgraph.view_layer.name
            state['warnings']['LIGHTS'] = lights['warnings']
        result['warnings'] = list(dict.fromkeys(w for ws in state['warnings'].values() for w in ws))
        LAST_WARNING = '；'.join(result['warnings'])
        for key in ('parameters', 'rig_objects', 'evaluated_writes', 'sockets', 'light_uploads'):
            STATS[key] += result[key]
        if reason != 'EDIT':
            state['last'] = time.monotonic()
        # A full explicit/frame sync supersedes queued viewport work.
        queued = _pending.get(scene.as_pointer())
        if queued:
            queued[1].difference_update(categories)
            if not queued[1]:
                _pending.pop(scene.as_pointer(), None)
        # Blender performs a second evaluation for data tagged by frame_post.
        # Recursing into update() here would re-enter that evaluation. Explicit
        # main-thread operations still need to commit new geometry immediately.
        if (result['sockets'] and depsgraph is not None
                and reason not in {'FRAME', 'RENDER'} and _main_thread() and not _rendering()):
            depsgraph.update()
        return result
    finally:
        _coordinating = False


def _tick():
    global _bootstrap_pending
    if not _registered:
        return None
    if _rendering():
        # Keep queued work/bootstrap for the main loop after complete/cancel.
        # No context reads, binding scans or writes while the worker is active.
        return 0.1 if _bootstrap_pending or _pending else None
    STATS['timer_calls'] += 1
    if _bootstrap_pending:
        _bootstrap_pending = False
        for managed in bpy.data.scenes:
            prepare_render(managed)
            if (managed.get('cmb_runtime_ready') or len(getattr(managed, 'cmb_anim_channels', ()))) and preview_enabled(managed):
                _pending.setdefault(managed.as_pointer(), (managed, set()))[1].update(ALL)
        scene = getattr(bpy.context, 'scene', None)
        if scene is not None and preview_enabled(scene):
            _pending.setdefault(scene.as_pointer(), (scene, set()))[1].update(ALL)
    now = time.monotonic()
    for key, (scene, categories) in list(_pending.items()):
        try:
            if not preview_enabled(scene) or key in _render_scenes:
                _pending.pop(key, None)
                continue
            if now < _state(scene)['last'] + INTERVAL:
                continue
            _pending.pop(key, None)
            _safe_sync(scene, reason='VIEWPORT', categories=categories)
        except ReferenceError:
            _pending.pop(key, None)
            _states.pop(key, None)
    if not _pending:
        return None
    return max(0.001, min(max(0.0, _state(s)['last'] + INTERVAL - time.monotonic())
                         for s, _ in _pending.values()))


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    _resume_viewport()
    if (not _registered or _busy or _coordinating or not preview_enabled(scene)
            or _rendering()):
        return
    queued = _pending.get(scene.as_pointer())
    if queued:
        request(scene, queued[1])
    state = _state(scene)
    if state['structure'] or state['counts'] != _counts(scene):
        state['structure'] = True
        request(scene)
        return
    categories = set()
    if state.get('view_layer') != depsgraph.view_layer.name:
        categories.add('LIGHTS')
    if state['camera_settings'] != _camera_settings(scene):
        categories.add('CAMERA')
    camera = scene.camera
    camera_ids = {i.as_pointer() for i in (camera, camera.data) if i} if camera else set()
    for update in depsgraph.updates:
        original = update.id.original
        pointer = original.as_pointer()
        if isinstance(original, bpy.types.Object) and original.type == 'MESH':
            if _owned(original) and pointer not in state['object_bindings']:
                state['structure'] = True
            elif pointer not in state['object_bindings'] and update.is_updated_geometry:
                materials = {s.material.as_pointer() for s in original.material_slots if s.material}
                for other in tuple(_states.values()):
                    if materials.intersection(other.get('materials', ())):
                        other['structure'] = True
                        request(other['scene'])
        if pointer in state['light_ids']:
            categories.add('LIGHTS')
        if pointer in camera_ids:
            categories.add('CAMERA')
        if pointer in state['rig_ids']:
            categories.add('RIG')
            if isinstance(original, bpy.types.Armature):
                state['structure'] = True
        if pointer in state['object_bindings']:
            if _object_binding(original) != state['object_bindings'][pointer]:
                state['structure'] = True
            if update.is_updated_transform:
                categories.update({'RIG', 'CAMERA'})
        if pointer in state['trees']:
            if _tree_binding(original) != state['trees'][pointer][1]:
                state['structure'] = True
        if pointer in state['materials']:
            # Material tree replacement / slot structure can change receivers.
            if original.node_tree and original.node_tree.as_pointer() not in state['trees']:
                state['structure'] = True
        if isinstance(original, bpy.types.Collection):
            state['structure'] = True
        if isinstance(original, bpy.types.Action):
            categories.add('PARAMETERS')
        if original == scene and scene.animation_data is not None:
            categories.add('PARAMETERS')
    if state['structure']:
        categories.update(ALL)
        state['param_full'] = True
    if categories:
        request(scene, categories)


@bpy.app.handlers.persistent
def _on_frame(scene, depsgraph=None):
    _resume_viewport()
    if not _registered or _busy or _coordinating:
        return
    rendering = depsgraph is not None and depsgraph.mode == 'RENDER'
    # The UI may also issue VIEWPORT callbacks while another scene is rendering.
    # Only the render worker's actual graph is eligible to update render data.
    if _rendering() and not rendering:
        return
    if rendering or preview_enabled(scene):
        key = scene.as_pointer()
        result = _safe_sync(scene, depsgraph, for_render=rendering,
                           force=rendering and key in _render_force,
                           reason='RENDER' if rendering else 'FRAME')
        if rendering:
            _render_force.discard(key)


@bpy.app.handlers.persistent
def _on_render_init(scene):
    if _registered:
        _render_scenes.add(scene.as_pointer())


@bpy.app.handlers.persistent
def _on_render(scene):
    if _registered:
        # render_pre runs before the current RENDER graph exists, on a worker
        # in GUI jobs. frame_change_post receives it and commits this frame.
        key = scene.as_pointer()
        _render_scenes.add(key)
        _render_force.add(key)


@bpy.app.handlers.persistent
def _on_render_end(scene):
    global _resume_pending
    key = scene.as_pointer()
    _render_scenes.discard(key)
    _render_force.discard(key)
    if _registered and preview_enabled(scene):
        # Pure queue bookkeeping: do not register timers from the render worker.
        # Native main-thread frame/depsgraph callbacks resume viewport updates.
        _pending.setdefault(key, (scene, set()))[1].update(ALL)
    _resume_pending = bool(_registered and (_pending or _bootstrap_pending))


def _rna_notice(category):
    _resume_viewport()
    if _rendering():
        return
    # RNA notifications cover output settings and view-layer visibility even
    # when no useful dependency-graph ID is reported. No polling is necessary.
    live_scenes = {s.as_pointer() for s in bpy.data.scenes}
    for key, state in tuple(_states.items()):
        if key not in live_scenes:
            _states.pop(key, None)
            _pending.pop(key, None)
            continue
        if category == 'STRUCTURE':
            state['structure'] = True
            request(state['scene'])
        else:
            request(state['scene'], {category})


def _subscribe():
    bpy.msgbus.clear_by_owner(_msg_owner)
    specs = [(bpy.types.Scene, 'camera', 'CAMERA'), (bpy.types.Window, 'view_layer', 'LIGHTS')]
    specs += [(bpy.types.RenderSettings, p, 'CAMERA') for p in
              ('resolution_x', 'resolution_y', 'resolution_percentage', 'pixel_aspect_x', 'pixel_aspect_y')]
    specs += [(bpy.types.Object, p, 'LIGHTS') for p in ('hide_render', 'hide_viewport', 'name')]
    specs += [(bpy.types.Collection, p, 'LIGHTS') for p in ('hide_render', 'hide_viewport')]
    specs += [(bpy.types.LayerCollection, p, 'LIGHTS') for p in ('exclude', 'hide_viewport')]
    for typ, prop, category in specs:
        bpy.msgbus.subscribe_rna(key=(typ, prop), owner=_msg_owner,
                                args=(category,), notify=_rna_notice)


@bpy.app.handlers.persistent
def _on_load(_unused):
    from . import animation, lighting
    global _bootstrap_pending, _resume_pending
    _resume_pending = False
    animation.invalidate()
    lighting.clear()
    _states.clear()
    _pending.clear()
    _render_scenes.clear()
    _render_force.clear()
    if _registered:
        for scene in bpy.data.scenes:
            prepare_render(scene)
        _subscribe()
        _bootstrap_pending = True
        if not bpy.app.timers.is_registered(_tick):
            bpy.app.timers.register(_tick, first_interval=0.0)


@bpy.app.handlers.persistent
def _on_history(_unused):
    _on_load(_unused)
    if _registered:
        for scene in bpy.data.scenes:
            if not preview_enabled(scene) and len(getattr(scene, 'cmb_anim_channels', ())):
                _safe_sync(scene, reason='HISTORY')


@bpy.app.handlers.persistent
def _on_save(_unused):
    from . import animation, lighting
    global LAST_WARNING
    if not _registered:
        return
    warnings = []
    for scene in bpy.data.scenes:
        if not (scene.get('cmb_runtime_ready') or len(getattr(scene, 'cmb_anim_channels', ()))):
            continue
        try:
            depsgraph = scene_depsgraph(scene)
            synchronize(scene, depsgraph, force=True, reason='SAVE')
            animation.flush(scene, depsgraph)
            lighting.flush(scene)
        except Exception as exc:
            warnings.append(scene.name + '：保存前同步失败：' + str(exc))
    if warnings:
        LAST_WARNING = '；'.join(warnings)
        print(LAST_WARNING)


HANDLERS = (('depsgraph_update_post', _on_depsgraph), ('frame_change_post', _on_frame),
            ('render_init', _on_render_init), ('render_pre', _on_render),
            ('render_complete', _on_render_end), ('render_cancel', _on_render_end),
            ('load_post', _on_load), ('undo_post', _on_history), ('redo_post', _on_history),
            ('save_pre', _on_save))


def register():
    global _registered, _bootstrap_pending
    _registered = True
    for name, callback in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if callback not in handlers:
            handlers.append(callback)
    _subscribe()
    # One deferred bootstrap, then the timer stops. No scene access here.
    _bootstrap_pending = True
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.0)


def unregister():
    global _registered, LAST_WARNING, _bootstrap_pending, _resume_pending
    _resume_pending = False
    _registered = False
    _bootstrap_pending = False
    for name, callback in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if callback in handlers:
            handlers.remove(callback)
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    bpy.msgbus.clear_by_owner(_msg_owner)
    _states.clear()
    _pending.clear()
    _render_scenes.clear()
    _render_force.clear()
    LAST_WARNING = ''
