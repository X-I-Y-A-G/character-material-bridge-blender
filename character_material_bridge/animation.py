"""Persistent Scene channels -> packed Uber parameter textures (no package Actions)."""
import uuid
import re
from collections import defaultdict

import bpy
from . import core

API_REVISION = 527
_busy = False
_registered = False
_cache = {}
_indices = {}
STATS = defaultdict(int)
LAST_WARNING = ''


def invalidate(scene=None):
    if scene is None:
        _indices.clear()
        _cache.clear()
    else:
        _indices.pop(scene.as_pointer(), None)


def prune_scenes(live, invalid=()):
    """Drop channel RNA owned by deleted scenes without invalidating images."""
    for key in (set(_indices) - live) | set(invalid):
        _indices.pop(key, None)


def index(scene):
    """Stable UUID and physical binding indices; no RNA collection name scans."""
    key = scene.as_pointer()
    channels = scene.cmb_anim_channels
    cached = _indices.get(key)
    if cached is None or cached['length'] != len(channels):
        by_name, by_binding = {}, {}
        # Blender may reallocate PropertyGroup storage when the collection
        # grows. Cache append-only integer indices, never long-lived RNA rows.
        for position, channel in enumerate(channels):
            by_name[channel.name] = position
            image = channel.image
            by_binding[(image.as_pointer() if image else 0, channel.col,
                        channel.part, channel.pname)] = position
        cached = dict(length=len(channels), names=by_name, bindings=by_binding, rows=None)
        _indices[key] = cached
        STATS['index_builds'] += 1
    return cached


def indexed_rows(scene):
    cached = index(scene)
    if cached['rows'] is None:
        cached['rows'] = tuple(scene.cmb_anim_channels)
    return cached['rows']


def binding_updated(channel, context):
    if _busy or getattr(channel.id_data, 'is_evaluated', False):
        return
    scene = channel.id_data
    channel.pop('cmb_committed', None)
    invalidate(scene)
    _cache.clear()
    from . import runtime
    runtime.invalidate(scene, parameters=True)


def animated_names(scene):
    """Only visit curves, never all static channels on a frame change.

    Numeric paths keep Blender's append-only indices. Named paths are accepted
    too. An unrecognized CMB path falls back to the complete parameter module.
    Muted/NLA actions are deliberately included for conservative evaluation.
    """
    names = set()
    channels = scene.cmb_anim_channels
    rows = None
    for curve in action_curves(scene):
        path = curve.data_path
        if not path.startswith('cmb_anim_channels['):
            continue
        match = re.match(r'cmb_anim_channels\[(\d+)\]\.', path)
        if match:
            position = int(match[1])
            if position < len(channels):
                if rows is None:rows = indexed_rows(scene)
                names.add(rows[position].name)
            continue
        match = re.match(r'cmb_anim_channels\["([a-zA-Z0-9_-]+)"\]\.', path)
        if match:
            names.add(match[1])
        else:
            return None
    return names


def value(channel):
    return (list(channel.cval) if channel.ptype == 'V4' else
            list(channel.v3val) if channel.ptype == 'V3' else channel.fval)


def assign(channel, new_value):
    if channel.ptype == 'V4':
        channel.cval = new_value
    elif channel.ptype == 'V3':
        channel.v3val = new_value
    else:
        channel.fval = float(new_value)


def remember(channel, val):
    """Undoable last uploaded value, separate from evaluated animation values.

    Blender's global undo restores Scene properties but not Image.pixels. This
    small non-animated snapshot lets paused undo restore exactly the frozen
    texture, without accidentally advancing unrelated animated channels.
    """
    values = tuple(val) if isinstance(val, (list, tuple)) else (float(val),)
    previous = channel.get('cmb_committed')
    if previous is None or tuple(previous) != values:
        channel['cmb_committed'] = values
        STATS['snapshot_writes'] += 1


def restore_committed(scene):
    global LAST_WARNING
    groups = defaultdict(lambda: defaultdict(list))
    for channel in getattr(scene, 'cmb_anim_channels', ()):
        saved = channel.get('cmb_committed')
        if saved is None or channel.image is None:continue
        val = list(saved) if channel.ptype in {'V3', 'V4'} else saved[0]
        groups[channel.image][channel.col].append((channel.part, channel.pname, val))
    uploads, warnings = 0, []
    for image, columns in groups.items():
        try:
            uploads += bool(core.write_params_batch(image, columns.items(), pack=False)['changed'])
        except RuntimeError as exc:
            warnings.append('撤销参数恢复失败：' + image.name + '：' + str(exc))
    LAST_WARNING = '；'.join(warnings)
    _cache.clear()
    return uploads


def find(scene, image, col, part, pname):
    cached = index(scene)
    position = cached['bindings'].get((image.as_pointer() if image else 0, col, part, pname))
    if position is None:return None
    return indexed_rows(scene)[position]


def ensure(scene, image, col, part, pname, initial):
    """Only initialize once. Refresh/filter operations must never reset animation."""
    global _busy
    channel = find(scene, image, col, part, pname)
    if channel is not None:
        return channel
    previous = _busy
    _busy = True
    try:
        channel = scene.cmb_anim_channels.add()
        channel.name = uuid.uuid4().hex
        channel.image, channel.col = image, col
        channel.part, channel.pname = part, pname
        channel.ptype = core.param_slot(part, pname)[2]
        channel.stamp = str(core.load_layout()['_meta']['stamp'])
        assign(channel, initial)
        remember(channel, value(channel))
        # Append to the existing index in O(1), preserving every native path.
        cached = _indices[scene.as_pointer()]
        cached['length'] += 1
        cached['rows'] = None
        cached['names'][channel.name] = cached['length'] - 1
        cached['bindings'][(image.as_pointer(), col, part, pname)] = cached['length'] - 1
        from . import runtime
        runtime.invalidate(scene, parameters=True)
        return channel
    finally:
        _busy = previous


def adopt_updates(scene, image, col, updates):
    """Manual writes/presets are authoritative; keep existing channels in step."""
    global _busy
    if not hasattr(scene, 'cmb_anim_channels'):
        return
    previous = _busy
    _busy = True
    try:
        for part, pname, new_value in updates:
            channel = find(scene, image, col, part, pname)
            if channel is not None:
                assign(channel, new_value)
                remember(channel, value(channel))
        _cache.pop(image.as_pointer(), None)
    finally:
        _busy = previous


def updated(channel, context):
    if _busy or getattr(channel.id_data, 'is_evaluated', False):
        return
    scene = channel.id_data
    if isinstance(scene, bpy.types.Scene):
        from . import runtime
        # An edit must never flush other animated values while preview is paused.
        runtime._safe_sync(scene, reason='EDIT', channels=(channel,))


def sync_scene(scene, depsgraph=None, force=False, channels=None,
               animated_only=False, validated=False):
    """Batch once per original Image. Never write an evaluated datablock."""
    global _busy, LAST_WARNING
    if _busy or not hasattr(scene, 'cmb_anim_channels'):
        return 0
    _busy = True
    try:
        if scene.get('cmb_runtime_ready') and not validated:
            from . import lighting
            lighting.validate_scope(scene, lighting.targets(scene))
        originals = index(scene)['names']
        if channels is None:
            names = animated_names(scene) if animated_only else None
            positions = (originals[n] for n in names if n in originals) if names is not None else originals.values()
            rows = indexed_rows(scene)
            channels = (rows[i] for i in positions)
        channels = tuple(channels)
        if not channels:
            LAST_WARNING = ''
            return 0
        evaluated = scene.evaluated_get(depsgraph) if depsgraph is not None else scene
        current_channels = ({c.name: c for c in evaluated.cmb_anim_channels}
                            if evaluated != scene else {c.name: c for c in channels})
        STATS['evaluated_index_builds'] += evaluated != scene
        groups = defaultdict(list)
        snapshots = defaultdict(list)
        stamp = str(core.load_layout()['_meta']['stamp'])
        warnings = []
        for channel in channels:
            STATS['channel_reads'] += 1
            image = channel.image
            if image is None:
                continue
            if channel.stamp != stamp or core.param_slot(channel.part, channel.pname) is None:
                warnings.append('参数动画布局已改变，请重新绑定：' + channel.pname)
                continue
            current = current_channels.get(channel.name)
            if current is None:
                continue
            val = value(current)
            snapshots[image].append((channel, val))
            groups[image].append((channel.col, channel.part, channel.pname,
                                  tuple(val) if isinstance(val, list) else val))
        uploads = 0
        for image, rows in groups.items():
            key = image.as_pointer()
            previous = _cache.get(key)
            if previous is None or previous[0] != tuple(image.size):
                previous = (tuple(image.size), {})
            changed_rows = [row for row in rows if force or previous[1].get(row[:3]) != row[3]]
            if not changed_rows:
                continue
            columns = defaultdict(list)
            for col, part, pname, val in changed_rows:
                columns[col].append((part, pname, val))
            try:
                report = core.write_params_batch(image, columns.items(), pack=False)
            except RuntimeError as exc:
                # A stale column or invalid driven value in one texture must not
                # freeze every other character. The batch writer validates all
                # edits before uploading, so this image remains unchanged.
                _cache.pop(key, None)
                warnings.append('参数动画图未更新：' + image.name + '：' + str(exc))
                continue
            uploads += bool(report['changed'])
            for channel, val in snapshots[image]:
                remember(channel, val)
            previous[1].update((row[:3], row[3]) for row in changed_rows)
            _cache[key] = previous
        STATS['uploads'] += uploads
        LAST_WARNING = '；'.join(dict.fromkeys(warnings))
        return uploads
    finally:
        _busy = False


def safe_sync(scene, depsgraph=None, force=False, **kwargs):
    global LAST_WARNING
    try:
        return sync_scene(scene, depsgraph, force, **kwargs)
    except Exception as exc:
        message = '材质参数动画同步失败：' + str(exc)
        if message != LAST_WARNING:
            print(message)
        LAST_WARNING = message
        return 0


def flush(scene, depsgraph=None):
    """Persist the current frame's image values, not the Scene animation itself."""
    if not hasattr(scene, 'cmb_anim_channels'):
        return
    from . import runtime
    depsgraph = runtime.scene_depsgraph(scene, depsgraph)
    sync_scene(scene, depsgraph, force=True)
    if LAST_WARNING:
        raise RuntimeError(LAST_WARNING)
    for image in {c.image for c in scene.cmb_anim_channels if c.image is not None}:
        if image.is_dirty or not image.packed_file:
            image.pack()


def action_curves(scene):
    """Active and NLA Actions, including Blender 4.4+ layered Actions."""
    data = scene.animation_data
    if data is None:
        return
    actions = {data.action} if data.action else set()
    def collect(strips):
        for strip in strips:
            if strip.action:actions.add(strip.action)
            if strip.type == 'META':collect(strip.strips)
    for track in data.nla_tracks:
        collect(track.strips)
    for action in actions:
        if getattr(action, 'is_action_layered', False):
            for layer in action.layers:
                for strip in layer.strips:
                    for bag in getattr(strip, 'channelbags', ()):
                        yield from bag.fcurves
        else:
            yield from action.fcurves
    yield from data.drivers


def legacy_warning(scene):
    if any(c.data_path.startswith('cmb_params.rows[') for c in action_curves(scene)):
        return '检测到旧参数列表的关键帧：请在新控件重新打帧；旧曲线不会自动迁移'
    return ''


@bpy.app.handlers.persistent
def _frame(scene, depsgraph=None):
    if _registered:
        safe_sync(scene, depsgraph)


@bpy.app.handlers.persistent
def _render(scene):
    if _registered:
        # Frame-change already evaluated the Scene channels, including background
        # animation rendering. A forced comparison also catches external pixels.
        sync_scene(scene, force=True)


@bpy.app.handlers.persistent
def _save(_unused):
    if _registered and bpy.context.scene is not None:
        flush(bpy.context.scene)


def _tick():
    if not _registered:
        return None
    if not bpy.app.is_job_running('RENDER') and bpy.context.scene is not None:
        safe_sync(bpy.context.scene)
    return None


@bpy.app.handlers.persistent
def _reset(_unused):
    invalidate()


HANDLERS = (('frame_change_post', _frame), ('depsgraph_update_post', _frame),
            ('render_pre', _render), ('save_pre', _save),
            ('load_post', _reset), ('undo_post', _reset), ('redo_post', _reset))


def register():
    global _registered
    _registered = True
    # Lifecycle work is scheduled once by runtime's coordinator. Manual updates
    # still use the same computation functions and persistent channels.


def unregister():
    global _registered, LAST_WARNING
    _registered = False
    for name, callback in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if callback in handlers:
            handlers.remove(callback)
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    invalidate()
    LAST_WARNING = ''
