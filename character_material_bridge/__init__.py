bl_info = {
    'name': 'Character Material Bridge / 角色材质桥',
    'author': '新杨XIYAG',
    'version': (0, 5, 26),
    'blender': (5, 2, 0),
    'location': 'View3D > Sidebar > 材质移植',
    'description': '请注意阅读RM,插件可快捷导入终末地角色材质。',
    'category': 'Material',
}

# 0.5.26: worker-safe render graph synchronization and image invalidation.
# Guarded render callbacks do not override the window context.
# Keep a root revision note too. Fast same-version reinstalls can otherwise
# reuse __init__.pyc when its timestamp second and source size both match.

import importlib
import json
import os
import sys
from array import array

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, \
    FloatVectorProperty, IntProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from . import core
from . import outline
from . import runtime
from . import animation
from . import defaults, postprocess
from . import lighting, rig_runtime, mesh_state, face_outline, hair_outline
from . import fur, fur_layers, uv_transfer
from . import color_management


def _reload_changed(module):
    # Rapid ZIP upgrades can keep the same source size and integer mtime.
    # A stale .pyc then survives importlib.reload despite a newer ZIP on disk.
    # Remove only this module's own cache when its API check requires reload.
    cached = getattr(module, '__cached__', None)
    source = getattr(module, '__file__', None)
    if (cached and source and os.path.basename(os.path.dirname(cached)) == '__pycache__'
            and os.path.normcase(os.path.dirname(os.path.dirname(cached))) == os.path.normcase(os.path.dirname(source))
            and os.path.basename(cached).startswith(os.path.splitext(os.path.basename(source))[0] + '.')):
        try:
            os.unlink(cached)
        except FileNotFoundError:
            pass
    importlib.invalidate_caches()
    return importlib.reload(module)


if getattr(fur, 'API_REVISION', 0) < 524:fur = _reload_changed(fur)
if getattr(fur_layers, 'API_REVISION', 0) < 524:fur_layers = _reload_changed(fur_layers)
if getattr(uv_transfer, 'API_REVISION', 0) < 524:uv_transfer = _reload_changed(uv_transfer)

if getattr(runtime, 'API_REVISION', 0) < 527:runtime = _reload_changed(runtime)
if getattr(animation, 'API_REVISION', 0) < 527:animation = _reload_changed(animation)
if getattr(lighting, 'API_REVISION', 0) < 527:lighting = _reload_changed(lighting)
if getattr(color_management, 'API_REVISION', 0) < 526:color_management = _reload_changed(color_management)
if getattr(face_outline, 'API_REVISION', 0) < 517:face_outline = _reload_changed(face_outline)
if getattr(hair_outline, 'API_REVISION', 0) < 522:hair_outline = _reload_changed(hair_outline)
if getattr(rig_runtime, 'API_REVISION', 0) < 527:rig_runtime = _reload_changed(rig_runtime)
if getattr(mesh_state, 'API_REVISION', 0) < 524:mesh_state = _reload_changed(mesh_state)

if getattr(postprocess, 'IMPLEMENTATION_REVISION', 0) < 526:
    postprocess = _reload_changed(postprocess)

if getattr(outline, 'API_REVISION', 0) < 523:
    outline = _reload_changed(outline)

# 自愈：sys.modules 中残留旧版 core（禁用/刷新不彻底、或外部脚本从别处导入过）时，
# 按磁盘文件重载，避免"新 __init__ + 旧 core"的 AttributeError。
# 0.4.0 起面板还依赖 core.load_layout（参数引擎）与 core.export_final_package（最终包），
# 0.4.1 依赖旧包部位/列号推断，同样触发重载。
if not (hasattr(core, 'PRESET_SUFFIX') and hasattr(core, 'fix_face_mainlight')
        and hasattr(core, 'load_layout') and hasattr(core, 'export_final_package')
        and hasattr(core, 'material_uber_part_inferred')
        and hasattr(core, 'repair_result_tangents')
        and getattr(core, 'API_REVISION', 0) >= 527
        and getattr(core, 'VERSION', 0) >= 2):
    core = _reload_changed(core)

_enum_cache = {}


def entry_items(self, context):
    if not context:
        return [('SKIP', '跳过 / 待确认', '')]
    raw = context.scene.cmb_settings.manifest
    key = (raw, self.evidence)
    if key not in _enum_cache:
        entries = []
        if raw:
            try:
                entries = json.loads(raw).get('entries', [])
            except ValueError:
                entries = []  # 损坏的 manifest：回退到原顺序
        _enum_cache[key] = core.row_menu_items(self.evidence, entries)
    return _enum_cache[key]


class CMB_Row(bpy.types.PropertyGroup):
    object_name: StringProperty()
    slot: IntProperty()
    material_name: StringProperty()
    status: StringProperty()
    reason: StringProperty()
    evidence: StringProperty()
    choice: EnumProperty(name='源材质 + 修改器组合', items=entry_items)


class CMB_Settings(bpy.types.PropertyGroup):
    filepath: StringProperty(name='材质包', subtype='FILE_PATH')
    manifest: StringProperty()
    digest: StringProperty()
    rows: CollectionProperty(type=CMB_Row)
    index: IntProperty()
    mapping_preset_note: StringProperty()
    transfer_source_uv: BoolProperty(
        name='移植源 UV 数据', default=True,
        description='从新版源包按面角复制完整 UV 层；保留目标已有 UV，同名冲突另存。'
                    '无法完整匹配或达到 8 层上限时跳过该层，继续材质移植；不修改节点连接')
    rebuild_tangents: BoolProperty(
        name='根据目标 UV 重建切线（含已有属性）', default=True,
        description='成对重建目标切线与符号，使用源端逐面角校准的约定，保留镜像 UV 的手性；'
                    '不复制源逐点数据，也不按源符号众数翻转')
    rebuild_outline_smoothing: BoolProperty(
        name='重建目标描边平滑信息', default=True,
        description='按同位置几何面法线及面角生成专用 CMB_OutlineUV2；核验 Ruri 解码并抑制零附近像素跳变，'
                    '不改原贴图 UV、表面法线和相机参数')
    restore_final_mesh: BoolProperty(
        name='恢复最终包网格校正数据', default=True,
        description='同模型且网格位置/拓扑顺序一致时，恢复最终包的表面法线、切线、UV及描边属性，'
                    '优先于重新计算；保留目标骨架、权重和形态键。移植到不同模型时可关闭')
    fix_face_light: BoolProperty(
        name='脸部主光方向 180° 修正', default=True,
        description='移植后按源/目标对象旋转差，在克隆材质树的主光方向链上插入方向修正；'
                    '仅对源对象名含 face 的材质生效（基于 lizhiyan 单角色的视觉验收结论，'
                    '其他材质不需要也不修改）')
    # 0.5.0（V2）：载入包的种类（'final'=最终包；空=源包/旧包）。final 包经 V1 归一化
    # 后走名称弱证据匹配，载入文案与面板 final 模式提示据此切换。
    package_kind: StringProperty()


class CMB_OT_export(bpy.types.Operator, ExportHelper):
    bl_idname = 'cmb.export_package'
    bl_label = '导出所选角色材质包'
    bl_description = '导出材质、修改器及用于 UV 移植的源参考网格，不携带骨架、权重、形态键或动画'
    filename_ext = '.blend'
    filter_glob: StringProperty(default='*.blend', options={'HIDDEN'})

    def execute(self, context):
        try:
            manifest = core.export_package(self.filepath, context.selected_objects)
            core.write_report(manifest)
            self.report({'INFO'}, f"已导出 {len(manifest['entries'])} 个组合，"
                                  f"包含 {manifest['audit']['reference_meshes']} 个源 UV 参考网格")
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CMB_OT_load(bpy.types.Operator, ImportHelper):
    bl_idname = 'cmb.load_package'
    bl_label = '选择材质包'
    filename_ext = '.blend'
    filter_glob: StringProperty(default='*.blend', options={'HIDDEN'})

    def execute(self, context):
        try:
            manifest = core.read_manifest(self.filepath)
            settings = context.scene.cmb_settings
            settings.filepath = self.filepath
            settings.manifest = json.dumps(manifest, ensure_ascii=False)
            settings.digest = core.file_digest(self.filepath)
            settings.package_kind = str(manifest.get('package_kind') or '')
            settings.rows.clear()
            settings.mapping_preset_note = ''
            preset = self.filepath + core.PRESET_SUFFIX
            if settings.package_kind == 'final':
                # 0.5.0（V2）：final 包载入文案——提示弱证据匹配语义，请用户核对后应用。
                message = f"已读取 {len(manifest['entries'])} 个材质（最终包：按名称弱证据匹配，请核对自动结果后应用）"
                if os.path.exists(preset):
                    message += '；发现映射预设，可一键载入: ' + bpy.path.basename(preset)
            elif os.path.exists(preset):
                message = (f"已读取 {len(manifest['entries'])} 个源组合；发现映射预设，可一键载入: "
                           + bpy.path.basename(preset))
            else:
                message = f"已读取 {len(manifest['entries'])} 个源组合，请选择目标角色并预览"
            self.report({'INFO'}, message)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CMB_OT_save_preset(bpy.types.Operator):
    bl_idname = 'cmb.save_preset'
    bl_label = '保存映射预设'
    bl_description = '把当前所有区域的确认结果写到材质包旁的 .mapping.json'

    def execute(self, context):
        settings = context.scene.cmb_settings
        if not settings.filepath:
            self.report({'ERROR'}, '请先选择材质包')
            return {'CANCELLED'}
        if not settings.rows:
            self.report({'ERROR'}, '没有可保存的匹配行，请先分析匹配')
            return {'CANCELLED'}
        manifest = {}
        if settings.manifest:
            try:
                manifest = json.loads(settings.manifest)
            except ValueError:
                manifest = {}
        rows = [{'object': row.object_name, 'slot': row.slot, 'material': row.material_name,
                 'choice': '' if row.choice == 'SKIP' else row.choice} for row in settings.rows]
        payload = core.build_preset(rows, manifest.get('package_id', ''),
                                    '.'.join(str(part) for part in bl_info['version']),
                                    entries=manifest.get('entries', []))
        path = settings.filepath + core.PRESET_SUFFIX
        try:
            with open(path, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.report({'ERROR'}, '预设写入失败: ' + str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, '已保存映射预设: ' + bpy.path.basename(path))
        return {'FINISHED'}


class CMB_OT_load_preset(bpy.types.Operator, ImportHelper):
    bl_idname = 'cmb.load_preset'
    bl_label = '载入映射预设'
    bl_description = '读取 .mapping.json 并应用到当前匹配行；最终包请使用导出时自动生成的配套预设'
    filename_ext = '.json'
    filter_glob: StringProperty(default='*.json', options={'HIDDEN'})

    def invoke(self, context, event):
        settings = context.scene.cmb_settings
        if settings.filepath:  # 默认目录 = 当前材质包所在目录
            paired = settings.filepath + core.PRESET_SUFFIX
            self.filepath = paired if os.path.isfile(paired) else os.path.dirname(settings.filepath) + os.sep
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        settings = context.scene.cmb_settings
        if not settings.rows:
            self.report({'ERROR'}, '请先分析匹配')
            return {'CANCELLED'}
        try:
            with open(self.filepath, encoding='utf-8') as stream:
                payload = json.load(stream)
        except (OSError, ValueError) as exc:
            self.report({'ERROR'}, '预设读取失败: ' + str(exc))
            return {'CANCELLED'}
        rows = [{'object': row.object_name, 'slot': row.slot, 'material': row.material_name,
                 'choice': '' if row.choice == 'SKIP' else row.choice} for row in settings.rows]
        outcome = {}
        try:
            core.apply_preset(payload, rows, manifest=json.loads(settings.manifest), report=outcome)
        except (RuntimeError, ValueError, TypeError) as exc:
            self.report({'ERROR'}, '映射预设未应用: ' + str(exc))
            return {'CANCELLED'}
        filename = bpy.path.basename(self.filepath)
        message = (f"{filename}：匹配 {outcome['matched']} 行，修改 {outcome['changed']} 行，"
                   f"未匹配 {len(outcome['unmatched'])} 行，歧义 {len(outcome['ambiguous'])} 行")
        previous = [(row.choice, row.status, row.reason) for row in settings.rows]
        try:
            for match in outcome['matches']:
                row = settings.rows[match['index']]
                row.choice = match['choice'] or 'SKIP'
                row.status = '预设' if match['choice'] else '预设·跳过'
                row.reason = '映射预设：' + filename + ('（数字尾号兼容）' if match['method'].startswith('SUFFIX') else '')
        except (TypeError, ValueError) as exc:
            for row, values in zip(settings.rows, previous):
                row.choice, row.status, row.reason = values
            self.report({'ERROR'}, '映射未写入，已保留原选择: ' + str(exc))
            return {'CANCELLED'}
        settings.mapping_preset_note = (filename + '\n'
            + f"匹配 {outcome['matched']} 行；修改 {outcome['changed']} 行\n"
            + f"未匹配 {len(outcome['unmatched'])} 行；歧义 {len(outcome['ambiguous'])} 行")
        core.write_report({'mapping_preset': self.filepath, **outcome,
                           'matching': rows})
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        self.report({'INFO'} if outcome['matched'] else {'WARNING'}, message)
        return {'FINISHED'}


class CMB_OT_preview(bpy.types.Operator):
    bl_idname = 'cmb.preview'
    bl_label = '分析所选角色 / 刷新匹配'

    def execute(self, context):
        try:
            settings = context.scene.cmb_settings
            if not settings.manifest:
                raise RuntimeError('请先选择材质包')
            manifest = json.loads(settings.manifest)
            rows = core.analyze(context.selected_objects, manifest)
            if not rows:
                raise RuntimeError('请选择 MMD 角色网格，不要选择骨架或刚体')
            settings.rows.clear()
            settings.mapping_preset_note = ''
            by_id = {e['id']: e for e in manifest['entries']}
            for item in rows:
                row = settings.rows.add()
                row.object_name, row.slot = item['object'], item['slot']
                row.material_name, row.status = item['material'], item['status']
                row.reason = item.get('reason', '')
                # 0.4.1：先写 evidence 再写 choice。choice 是动态 EnumProperty，赋值时
                # Blender 会按当前 items 帧把 identifier 折算成 int 索引存——虽然
                # row_menu_items 已改为与证据无关的恒定顺序（根治），这里仍保证写帧
                # 所见即读帧，双保险杜绝错位回归。
                row.evidence = json.dumps([{**r, 'source': by_id[r['id']]['source_material'],
                                           'object': by_id[r['id']]['source_object']} for r in item['candidates']], ensure_ascii=False)
                row.choice = item['choice'] or 'SKIP'
            core.write_report({'matching': rows, 'export_warnings': manifest['warnings']})
            self.report({'INFO'}, f"{len(rows)} 个区域；{sum(bool(r['choice']) for r in rows)} 个自动匹配，其余请确认")
            return {'FINISHED'}
        except KeyError as exc:  # 0.5.0（V3）：数据不完整时给可读中文，而非裸键名
            self.report({'ERROR'}, '材质包数据不完整（缺少键 ' + str(exc)
                        + '）；请重新选择材质包后再分析匹配')
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CMB_OT_apply(bpy.types.Operator):
    bl_idname = 'cmb.apply'
    bl_label = '复制角色、按材质分离并移植'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            settings = context.scene.cmb_settings
            if not settings.rows:
                raise RuntimeError('请先分析匹配')
            if core.file_digest(settings.filepath) != settings.digest:
                raise RuntimeError('材质包已改变，请重新选择并分析')
            objects = []
            rows = []
            for row in settings.rows:
                obj = bpy.data.objects.get(row.object_name)
                if not obj or obj not in list(context.selected_objects):
                    raise RuntimeError('所选角色已变化，请重新分析匹配')
                if row.slot >= len(obj.material_slots) or not obj.material_slots[row.slot].material or obj.material_slots[row.slot].material.name != row.material_name:
                    raise RuntimeError('材质槽已变化，请重新分析匹配')
                if obj not in objects:
                    objects.append(obj)
                rows.append({'object': row.object_name, 'slot': row.slot,
                             'choice': '' if row.choice == 'SKIP' else row.choice})
            result = core.apply_transfer(settings.filepath, objects, json.loads(settings.manifest), rows,
                                         settings.rebuild_tangents, settings.fix_face_light,
                                         settings.rebuild_outline_smoothing, settings.restore_final_mesh,
                                         transfer_source_uv=settings.transfer_source_uv)
            self.report({'INFO'}, f"已移植 {len(result['applied'])} 个区域；{len(result['warnings'])} 条属性提示见报告")
            return {'FINISHED'}
        except KeyError as exc:  # 0.5.0（V3）：数据不完整时给可读中文，而非裸键名
            self.report({'ERROR'}, '材质包数据不完整（缺少键 ' + str(exc)
                        + '）；请重新选择材质包并重新分析匹配')
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CMB_OT_repair_tangents(bpy.types.Operator):
    bl_idname = 'cmb.repair_tangents'
    bl_label = '重建/修复所选移植结果切线'
    bl_description = '修复已有结果的切线与符号；旧结果需先载入同角色用新版重新导出的源材质包'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(
            o.type == 'MESH' and core.role_of(o) == 'result' for o in context.selected_objects)

    def execute(self, context):
        try:
            raw = context.scene.cmb_settings.manifest
            report = core.repair_result_tangents(context.selected_objects, json.loads(raw) if raw else None)
            count = len(report['repaired'])
            self.report({'INFO'} if count else {'WARNING'},
                        f"已重建 {count} 个部件；跳过 {len(report['skipped'])} 个，原因见报告")
            return {'FINISHED'} if count else {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CMB_OT_outline_smoothing(bpy.types.Operator):
    bl_idname = 'cmb.rebuild_outline_smoothing'
    bl_label = '重建所选角色描边平滑信息'
    bl_description = '为同批同角色的所有分离部件共同重建描边法线；不修改表面着色法线'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(o.type == 'MESH' and core.role_of(o) == 'result'
                                                for o in context.selected_objects)

    def execute(self, context):
        try:
            report = core.rebuild_outline_smoothing(context.selected_objects)
            count = len(report['rebuilt'])
            self.report({'INFO'} if count else {'WARNING'},
                        f"已重建 {count} 个描边部件；跳过 {len(report['skipped'])} 个，详情见报告")
            return {'FINISHED'} if count else {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CMB_OT_restore(bpy.types.Operator):
    bl_idname = 'cmb.restore'
    bl_label = '移除所选移植结果并恢复原角色'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        collections = {c for o in context.selected_objects for c in o.users_collection if core.role_of(c) == 'transaction'}
        if len(collections) != 1:
            self.report({'ERROR'}, '请选择同一批移植结果中的对象')
            return {'CANCELLED'}
        try:
            count = core.restore(collections.pop())
            self.report({'INFO'}, f'已删除 {count} 个生成部件，原始角色已恢复')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CMB_OT_report(bpy.types.Operator):
    bl_idname = 'cmb.report'
    bl_label = '在文本编辑器查看报告'

    def execute(self, context):
        text = bpy.data.texts.get(core.REPORT)
        if not text:
            self.report({'INFO'}, '尚无报告')
            return {'CANCELLED'}
        area = context.area
        area.type = 'TEXT_EDITOR'
        area.spaces.active.text = text
        return {'FINISHED'}


class CMB_UL_rows(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row()
        row.label(text=item.material_name, icon='CHECKMARK' if item.choice != 'SKIP' else 'QUESTION')
        row.prop(item, 'choice', text='')


# --------------------------------------------------------------------------
# 参数调节（0.4.0 params-expose）：把 Ruri Uber 材质编码在参数图里的基础参数
# 暴露成 MMD 端可调项。调参直接写参数图列（core.write_params），视口即时生效。
# 保守实现：enum 过滤 + template_list 浏览 + 单参数编辑区。

_GUARD = [False]  # 重建行集合期间关掉写路径（回读一次写一次的镜子问题，同 RuriRipper 模式）
_group_cache = {}


def _panel_material(context):
    """The material slot the params panel currently points at (active object)."""
    state = context.scene.cmb_params
    obj = context.view_layer.objects.active if context else None
    if not obj or obj.type != 'MESH':
        return None
    try:
        slot = int(state.material)
    except (TypeError, ValueError):
        slot = 0
    if slot >= len(obj.material_slots):
        return None
    return obj.material_slots[slot].material


def _format_value(value):
    if value is None:
        return '-'
    if isinstance(value, (list, tuple)):
        return ' '.join(f'{float(v):.3g}' for v in value)
    return f'{float(value):.3g}'


def _row_value(row):
    if row.ptype == 'V4':
        return list(row.cval)
    if row.ptype == 'V3':
        return list(row.v3val)
    return row.fval


def _store_row_value(row, value):
    """Push a read/layout value into the row's edit properties (write path stays off)."""
    if row.ptype == 'V4':
        row.cval = tuple(value) if isinstance(value, (list, tuple)) else (float(value),) * 4
    elif row.ptype == 'V3':
        row.v3val = tuple(value[:3]) if isinstance(value, (list, tuple)) else (float(value),) * 3
    else:
        row.fval = float(value[0]) if isinstance(value, (list, tuple)) else float(value)


def _param_updated(row, context):
    """Edit -> write the params image column immediately (viewport refreshes via
    core.write_params' update/gl_free path)."""
    if _GUARD[0]:
        return
    state = context.scene.cmb_params
    if animation.legacy_warning(context.scene):
        state.note = animation.legacy_warning(context.scene)
        return
    if not parameter_target_is_current(state, context):
        state.note = '参数目标或布局已改变，请先刷新参数列表'
        return
    material = _panel_material(context)
    value = _row_value(row)
    try:
        core.write_params(core.mat_param_image(material), state.col, [(state.part, row.pname, value)])
    except RuntimeError as exc:
        state.note = str(exc)
        return
    row.modified = defaults.differs(material, state.part, row.pname, value)
    row.value_text = _format_value(value)


def _material_items(self, context):
    obj = context.view_layer.objects.active if context else None
    if not obj or obj.type != 'MESH':
        return [('0', '（无网格）', '')]
    items = []
    for index, slot in enumerate(obj.material_slots):
        name = slot.material.name if slot.material else '（空槽）'
        items.append((str(index), f'{index:02d} {name}', ''))
    return items or [('0', '（无材质）', '')]


def _group_items(self, context):
    """权威 INTERFACE 分组下拉（ruri_params_layout.json 合并数据；引擎内部参数归「未分组」）。"""
    key = self.part or ''
    cached = _group_cache.get(key)
    if cached is None:
        groups = core.interface_groups(key) if key else []
        cached = [('UNGROUPED', '（未分组）', '引擎内部参数（无界面分组）')] if groups and groups[0] == '' else []
        cached += [(group, group, '') for group in groups if group]
        if not cached:
            cached = [('NONE', '（无）', '')]
        _group_cache[key] = cached
    return cached


def _state_changed(self, context):
    if _GUARD[0]:
        return
    if self.filter == 'GROUP' and self.part:
        groups = [item[0] for item in _group_items(self, context)]
        if self.group not in groups:
            _GUARD[0] = True
            try:
                self.group = groups[0] if groups else 'NONE'
            finally:
                _GUARD[0] = False
    rebuild_param_rows(self, context)


def rebuild_param_rows(state, context):
    """Re-read the current material's params image column and rebuild the browsable
    rows for the active filter (values only change through this plugin's writes)."""
    material = _panel_material(context)
    _GUARD[0] = True
    try:
        state.rows.clear()
        state.note = ''
        state.part = ''
        state.col = 0
        state.bound_material = None
        state.bound_image = None
        if material is None:
            state.note = '请激活网格对象'
            return
        part = core.material_uber_part(material)
        col = core.material_param_col(material)
        image = core.mat_param_image(material)
        state.bound_material = material
        state.bound_image = image
        state.part = part or ''
        state.col = int(col) if col else 0
        # 0.4.1：部位/列号来自组树名/RuriMatCol 节点推断（旧包无属性）时面板标注（推断）。
        state.inferred = bool(part and not material.get('ruri_uber_part')) or \
            bool(col and material.get('ruri_param_col') is None)
        notes = []
        if not part:
            notes.append('非 Ruri Uber 材质（无部位记录）')
        if not col:
            notes.append('未找到参数列号（属性与树内 RuriMatCol 均无）')
        elif not image:
            notes.append('材质树未引用 Uber 参数图')
        if part and not core.stamp_matches(material):
            notes.append('布局表 stamp 与材质记录不一致，参数槽位可能已变化')
        state.note = '；'.join(notes)
        if not part:
            return
        width = int(image.size[0]) if image else 0
        height = int(image.size[1]) if image else 0
        pixels = None
        if image and width and height:
            pixels = array('f', [0.0]) * (width * height * 4)
            image.pixels.foreach_get(pixels)
        pool = core.param_names(part)
        if pixels is not None and (not 0 < state.col < width
                or any((core.param_slot(part, name)[0] + 1) * width * 4 > len(pixels) for name in pool)):
            state.note = '参数图尺寸或列号与布局不匹配，已禁止编辑'
            return
        if state.filter == 'PBR':
            pool = core.curated_names(part, 'PBR')
        infos = []
        for name in pool:
            current = core.read_param(pixels, width, state.col, part, name) if pixels and state.col else None
            if current is not None and core.stamp_matches(material):
                channel = animation.ensure(context.scene, image, state.col, part, name, current)
                current = animation.value(channel)
            group, label = core.param_group_label(part, name)
            infos.append({'name': name,
                          'type': (core.param_slot(part, name) or (0, 0, 'F'))[2],
                          'current': current,
                          'group': group,
                          'label': label,
                          'modified': defaults.differs(material, part, name, current)})
        if state.filter == 'MODIFIED':
            infos = [info for info in infos if info['modified']]
        elif state.filter == 'GROUP':
            want = '' if state.group == 'UNGROUPED' else state.group
            infos = [info for info in infos if info['group'] == want]
        if state.search:
            needles = state.search.casefold().split()
            infos = [info for info in infos if all(
                needle in core.param_search_text(part, info['name'], info['label']) for needle in needles)]
        for info in infos:
            row = state.rows.add()
            row.pname = info['name']
            row.ptype = info['type']
            row.pgroup = info['group']
            row.plabel = info['label']
            value = info['current'] if info['current'] is not None else defaults.default_value(material, part, info['name'])
            row.modified = info['modified']
            row.value_text = _format_value(value)
            _store_row_value(row, value)
        state.index = min(state.index, len(infos) - 1) if infos else 0
    finally:
        _GUARD[0] = False


def parameter_target_is_current(state, context):
    """A row is a snapshot of one material/image/column/part, never a global edit target."""
    material = _panel_material(context)
    return bool(material is not None and material == state.bound_material
                and core.mat_param_image(material) == state.bound_image
                and state.bound_image is not None
                and core.material_param_col(material) == state.col
                and core.material_uber_part(material) == state.part
                and core.stamp_matches(material))


def _component_getter(index):
    def get(row):
        values = row.cval if row.ptype == 'V4' else row.v3val
        return float(values[index]) if index < len(values) else 0.0
    return get


def _component_setter(index):
    def set_value(row, value):
        # Single source of truth: the RGBA/vector property triggers the existing
        # guarded write, preset, default and modified-row paths exactly once.
        values = list(row.cval if row.ptype == 'V4' else row.v3val)
        if index >= len(values):
            return
        values[index] = float(value)
        if row.ptype == 'V4':
            row.cval = values
        else:
            row.v3val = values
    return set_value


class CMB_ParamRow(bpy.types.PropertyGroup):
    pname: StringProperty()  # 参数名（布局表键）
    ptype: StringProperty()  # F / V4 / V3
    pgroup: StringProperty()  # 权威 INTERFACE 分组（空=引擎内部参数）
    plabel: StringProperty()  # 面板行 label（中英混排按源提取）
    value_text: StringProperty()
    modified: BoolProperty(default=False)
    fval: FloatProperty(name='数值', min=-10000.0, max=10000.0, soft_min=0.0, soft_max=10.0,
                        update=_param_updated)
    cval: FloatVectorProperty(name='颜色+Alpha', size=4, subtype='COLOR',
                              min=-10000.0, max=10000.0, soft_min=0.0, soft_max=1.0,
                              update=_param_updated)
    v3val: FloatVectorProperty(name='颜色', size=3, subtype='COLOR',
                               min=-10000.0, max=10000.0, soft_min=0.0, soft_max=1.0,
                               update=_param_updated)
    alpha: FloatProperty(name='Alpha', min=-10000.0, max=10000.0, soft_min=0.0, soft_max=1.0,
                         description='颜色第四分量；与颜色框同步，保留 RGB',
                         get=_component_getter(3), set=_component_setter(3))
    component_x: FloatProperty(name='X', min=-10000.0, max=10000.0,
                               get=_component_getter(0), set=_component_setter(0))
    component_y: FloatProperty(name='Y', min=-10000.0, max=10000.0,
                               get=_component_getter(1), set=_component_setter(1))
    component_z: FloatProperty(name='Z', min=-10000.0, max=10000.0,
                               get=_component_getter(2), set=_component_setter(2))
    component_w: FloatProperty(name='W', min=-10000.0, max=10000.0,
                               get=_component_getter(3), set=_component_setter(3))


class CMB_AnimChannel(bpy.types.PropertyGroup):
    # UUID identifies the binding. Blender's native UI uses numeric collection
    # paths, so this collection is append-only: NEVER remove/reorder its entries.
    name: StringProperty(options={'HIDDEN'}, update=animation.binding_updated)
    image: PointerProperty(type=bpy.types.Image, update=animation.binding_updated)
    col: IntProperty(options=set(), update=animation.binding_updated)
    part: StringProperty(options=set(), update=animation.binding_updated)
    pname: StringProperty(options=set(), update=animation.binding_updated)
    ptype: StringProperty(options=set(), update=animation.binding_updated)
    stamp: StringProperty(options=set(), update=animation.binding_updated)
    fval: FloatProperty(name='数值', min=-10000.0, max=10000.0,
                        soft_min=0.0, soft_max=10.0, update=animation.updated)
    cval: FloatVectorProperty(name='颜色+Alpha', size=4, subtype='COLOR',
                              min=-10000.0, max=10000.0, soft_min=0.0, soft_max=1.0,
                              update=animation.updated)
    v3val: FloatVectorProperty(name='颜色', size=3, subtype='COLOR',
                               min=-10000.0, max=10000.0, soft_min=0.0, soft_max=1.0,
                               update=animation.updated)


class CMB_ParamState(bpy.types.PropertyGroup):
    bound_material: PointerProperty(type=bpy.types.Material)
    bound_image: PointerProperty(type=bpy.types.Image)
    material: EnumProperty(name='材质', items=_material_items, update=_state_changed)
    filter: EnumProperty(name='分组', items=[
        # Preserve persisted enum numbers; 2 used to mean NPR.
        ('ALL', '全部', '', 0),
        ('PBR', 'PBR 基础精选', '', 1),
        ('MODIFIED', '已修改', '与布局默认值有差异的参数', 3),
        ('GROUP', '按界面分组', '权威 INTERFACE 分组（ruri_params_layout.json）', 4),
    ], update=_state_changed)
    group: EnumProperty(name='界面分组', items=_group_items, update=_state_changed)
    search: StringProperty(name='搜索', description='按参数名/面板标签过滤', update=_state_changed)
    part: StringProperty()
    col: IntProperty()
    inferred: BoolProperty(default=False)  # 部位/列号至少一项为推断（旧包无属性）
    rows: CollectionProperty(type=CMB_ParamRow)
    index: IntProperty()
    note: StringProperty()
    preset_note: StringProperty()


class CMB_UL_params(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        channel = animation.find(context.scene, data.bound_image, data.col, data.part, item.pname)
        current = animation.value(channel) if channel is not None else _row_value(item)
        changed = defaults.differs(data.bound_material, data.part, item.pname, current)
        row.label(text=item.pname, icon='DOT' if changed else 'CHECKMARK')
        row.label(text=_format_value(current))


class CMB_OT_param_refresh(bpy.types.Operator):
    bl_idname = 'cmb.param_refresh'
    bl_label = '刷新参数列表'
    bl_description = '按当前材质与过滤条件重读参数图数值'

    @classmethod
    def poll(cls, context):
        return context.view_layer.objects.active is not None

    def execute(self, context):
        rebuild_param_rows(context.scene.cmb_params, context)
        return {'FINISHED'}


class CMB_OT_param_default(bpy.types.Operator):
    bl_idname = 'cmb.param_default'
    bl_label = '恢复默认值'
    bl_description = '按材质部位恢复推荐默认值；其余参数使用原布局默认值'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        state = context.scene.cmb_params
        return 0 <= state.index < len(state.rows)

    def execute(self, context):
        state = context.scene.cmb_params
        if not parameter_target_is_current(state, context):
            self.report({'ERROR'}, '参数目标或布局已改变，请先刷新参数列表')
            return {'CANCELLED'}
        item = state.rows[state.index]
        material = _panel_material(context)
        default = defaults.default_value(material, state.part, item.pname)
        if defaults.binding_conflict(material):
            self.report({'ERROR'}, '共享参数列存在部位冲突，未恢复默认值')
            return {'CANCELLED'}
        if default is None:
            self.report({'ERROR'}, '默认部位未确定或布局无此参数，请先选择默认参数部位')
            return {'CANCELLED'}
        try:
            core.write_params(core.mat_param_image(material), state.col,
                              [(state.part, item.pname, default)])
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        _GUARD[0] = True
        try:
            _store_row_value(item, default)
            item.modified = False
            item.value_text = _format_value(default)
        finally:
            _GUARD[0] = False
        return {'FINISHED'}


class CMB_OT_save_params_preset(bpy.types.Operator, ExportHelper):
    bl_idname = 'cmb.save_params_preset'
    bl_label = '保存参数预设'
    bl_description = '把场景材质的完整参数数值保存到 .params.json，包含恢复为默认值的参数'
    filename_ext = '.json'
    filter_glob: StringProperty(default='*.json', options={'HIDDEN'})

    def invoke(self, context, event):
        settings = context.scene.cmb_settings
        if settings.filepath:  # 默认 = 材质包旁 <包名>.params.json
            self.filepath = settings.filepath + core.PARAMS_PRESET_SUFFIX
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.filepath:
            self.report({'ERROR'}, '请指定预设文件路径')
            return {'CANCELLED'}
        settings = context.scene.cmb_settings
        package_id = ''
        if settings.manifest:
            try:
                package_id = json.loads(settings.manifest).get('package_id', '') or ''
            except ValueError:
                package_id = ''
        payload = core.build_params_preset(context.scene.objects, package_id,
                                           '.'.join(str(part) for part in bl_info['version']))
        if not payload['mats']:
            self.report({'ERROR'}, '没有可保存的材质参数')
            return {'CANCELLED'}
        try:
            with open(self.filepath, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.report({'ERROR'}, '参数预设写入失败: ' + str(exc))
            return {'CANCELLED'}
        note = f"已保存参数预设 {bpy.path.basename(self.filepath)}（{len(payload['mats'])} 个材质）"
        context.scene.cmb_params.preset_note = note
        self.report({'INFO'}, note)
        return {'FINISHED'}


class CMB_OT_load_params_preset(bpy.types.Operator, ImportHelper):
    bl_idname = 'cmb.load_params_preset'
    bl_label = '载入参数预设'
    bl_description = '读取 .params.json 并写回记录的参数；兼容旧版仅记录修改项的预设'
    filename_ext = '.json'
    filter_glob: StringProperty(default='*.json', options={'HIDDEN'})

    def invoke(self, context, event):
        settings = context.scene.cmb_settings
        if settings.filepath:  # 默认目录 = 当前材质包所在目录
            self.filepath = os.path.dirname(settings.filepath) + os.sep
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            with open(self.filepath, encoding='utf-8') as stream:
                payload = json.load(stream)
        except (OSError, ValueError) as exc:
            self.report({'ERROR'}, '参数预设读取失败: ' + str(exc))
            return {'CANCELLED'}
        try:
            result = core.apply_params_preset(payload)
        except (RuntimeError, ValueError, TypeError) as exc:
            self.report({'ERROR'}, '参数预设未应用: ' + str(exc))
            return {'CANCELLED'}
        note = f"已套用参数预设 {result['applied']} 个材质 / {result['params']} 项"
        if result['skipped']:
            note += f"；跳过 {len(result['skipped'])} 项（详情见报告）"
        core.write_report({'params_preset': {'path': self.filepath, **result}})
        state = context.scene.cmb_params
        state.preset_note = note
        rebuild_param_rows(state, context)
        self.report({'INFO'}, note)
        return {'FINISHED'}


class CMB_OT_export_final(bpy.types.Operator, ExportHelper):
    bl_idname = 'cmb.export_final_package'
    bl_label = '导出最终材质包'
    bl_description = '把选中的移植结果导出为可分发的材质包，可携带参考网格以还原校正数据；' \
                     '同时生成配套映射预设；防泄露审计（骨架/MMD 资产命名）不过即整体拒绝'
    filename_ext = '.blend'
    filter_glob: StringProperty(default='*.blend', options={'HIDDEN'})

    include_reference_mesh: BoolProperty(
        name='包含校正后的参考网格', default=True,
        description='保存表面法线、切线、UV和描边数据；不携带骨架、权重、形态键或动画。'
                    '关闭则只导出材质，无法还原网格校正')

    def execute(self, context):
        try:
            manifest = core.export_final_package(self.filepath, context.selected_objects,
                                                  '.'.join(str(v) for v in bl_info['version']),
                                                  include_reference_mesh=self.include_reference_mesh)
            self.report({'INFO'}, f"最终包已导出 {len(manifest['entries'])} 个材质，"
                                  f"参考网格 {manifest['audit']['reference_meshes']} 个；"
                                  f"已生成配套映射预设 {manifest['mapping_preset']['rows']} 行；"
                                  '请将 .blend 与 .blend.mapping.json 一起分发')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CMB_OT_outline_width(bpy.types.Operator):
    bl_idname = 'cmb.outline_width'
    bl_label = '调整描边宽度'
    bl_description = '调整当前部位或所选移植部位的几何描边宽度；按倍率可保留各部位原有比例，结果随最终包保存'
    bl_options = {'REGISTER', 'UNDO'}

    scope: EnumProperty(name='范围', items=(('ACTIVE', '当前部位', '当前活动的移植网格'),
                        ('SELECTED', '所选移植部位', '批量处理选中的移植网格')), default='ACTIVE')
    mode: EnumProperty(name='调整方式', items=(('MULTIPLY', '按倍率调整', '保留各部位原宽度比例'),
                       ('SET', '设为指定宽度', '将范围内每个描边组设为同一个宽度')), default='MULTIPLY')
    width: FloatProperty(name='描边宽度', default=0.5, min=0.0, max=20.0, soft_max=2.0, precision=3)
    factor: FloatProperty(name='宽度倍率', default=1.0, min=0.0, max=20.0, soft_max=3.0, precision=3)

    @classmethod
    def poll(cls, context):
        return bool(outline.width_controls(context.object))

    def invoke(self, context, event):
        self.width = outline.width_controls(context.object)[0][1].default_value
        self.scope = 'SELECTED' if sum(bool(outline.width_controls(o)) for o in context.selected_objects) > 1 else 'ACTIVE'
        return context.window_manager.invoke_props_dialog(self, width=350)

    def draw(self, context):
        self.layout.prop(self, 'scope')
        self.layout.prop(self, 'mode')
        self.layout.prop(self, 'factor' if self.mode == 'MULTIPLY' else 'width')
        self.layout.label(text='倍率 1 保持原值；大于 1 加宽，小于 1 变细')

    def execute(self, context):
        objects = [context.object] if self.scope == 'ACTIVE' else context.selected_objects
        try:
            report = outline.adjust_width(objects, self.mode, self.width, self.factor)
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        core.write_report({'outline_width': report})
        self.report({'WARNING'} if report['warnings'] else {'INFO'},
                    '已调整 %d 个部位、%d 处描边宽度；%d 条提示' %
                    (len(report['objects']), len(report['changes']), len(report['warnings'])))
        return {'FINISHED'}


class CMB_PT_params(bpy.types.Panel):
    bl_label = '参数调节（Ruri Uber）'
    bl_idname = 'CMB_PT_params'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '材质移植'
    bl_parent_id = 'CMB_PT_panel'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        state = context.scene.cmb_params
        obj = context.view_layer.objects.active
        if not obj or obj.type != 'MESH':
            layout.label(text='激活对象需为网格', icon='INFO')
            return
        widths = outline.width_controls(obj)
        if widths:
            box = layout.box()
            box.label(text='几何描边宽度 · 当前部位')
            for index, (node, socket) in enumerate(widths):
                box.label(text='%d：%s%s' % (index + 1, format(socket.default_value, '.4g'),
                                           '（连线控制）' if socket.is_linked else ''))
            box.operator('cmb.outline_width')
            box.label(text='选择多个移植部位可按倍率批量调整')
        layout.prop(state, 'material')
        material = _panel_material(context)
        if material is not None:
            layout.prop(material, 'cmb_default_role')
            role = defaults.resolve(material)
            label = dict((r[0], r[1]) for r in defaults.ROLE_ITEMS).get(role, '未确定，请手动选择')
            layout.label(text='默认配置: ' + label)
        layout.operator('cmb.apply_recommended_defaults', text='对所选对象应用部位默认值')
        if material is not None and core.material_uber_part(material) == 'Face' and core.role_of(obj) != 'result':
            layout.label(text='Face 描边亮度修正在移植时自动接入', icon='INFO')
        if (material is not None and core.material_uber_part(material) == 'Face'
                and core.role_of(obj) == 'result' and not face_outline.installed(material)):
            layout.operator('cmb.runtime_initialize', text='修复脸部/身体描边亮度', icon='FILE_REFRESH')
        if state.part:
            layout.label(text='部位 ' + state.part + ' · 列 ' + (str(state.col) if state.col else '未知')
                         + ('（推断）' if state.inferred else ''))
        if state.note:
            layout.label(text=state.note, icon='ERROR')
        layout.prop(state, 'filter')
        if state.filter == 'GROUP':
            layout.prop(state, 'group')
        layout.prop(state, 'search', icon='VIEWZOOM')
        row = layout.row(align=True)
        row.operator('cmb.param_refresh', icon='FILE_REFRESH')
        if state.rows and not parameter_target_is_current(state, context):
            layout.label(text='参数目标或布局已改变，请先刷新参数列表', icon='ERROR')
            return
        if not state.rows:
            layout.label(text='当前过滤条件下没有参数')
            return
        layout.template_list('CMB_UL_params', '', state, 'rows', state, 'index', rows=8)
        if not (0 <= state.index < len(state.rows)):
            return
        item = state.rows[state.index]
        box = layout.box()
        box.label(text=item.pname + '（' + item.ptype + '）')
        origin = ' / '.join(part for part in (item.pgroup, item.plabel) if part)
        if origin:
            box.label(text=origin, icon='INFO')
        box.label(text='默认 ' + _format_value(defaults.default_value(_panel_material(context), state.part, item.pname)))
        if item.pname == '_OutlineColorBrightness' and face_outline.installed(material):
            box.label(text='0.5 = 原始描边；0 = 黑色；1 = 两倍线性颜色')
        if not state.col or not core.mat_param_image(_panel_material(context)):
            box.label(text='无参数图列，不可编辑', icon='ERROR')
            return
        channel = animation.find(context.scene, state.bound_image, state.col, state.part, item.pname)
        if channel is None:
            box.label(text='请刷新参数列表以初始化动画通道', icon='INFO')
            return
        legacy = animation.legacy_warning(context.scene)
        if legacy:
            box.label(text=legacy, icon='ERROR')
        if animation.LAST_WARNING:
            box.label(text=animation.LAST_WARNING, icon='ERROR')
        box.label(text='鼠标悬停数值按 I / 右键插入关键帧；Alpha 可单独打帧')
        if item.ptype in ('V4', 'V3') and core.param_ui_kind(state.part, item.pname) == 'VECTOR':
            prop = 'cval' if item.ptype == 'V4' else 'v3val'
            for index, axis in enumerate('XYZW'[:4 if item.ptype == 'V4' else 3]):
                box.prop(channel, prop, index=index, text=axis)
        elif item.ptype == 'V4':
            box.prop(channel, 'cval', text='颜色')
            box.prop(channel, 'cval', index=3, text='底色 Alpha' if item.pname == '_BaseColor' else 'Alpha（A）', slider=True)
            if item.pname == '_BaseColor':
                box.label(text='透明效果还取决于贴图 Alpha 和表面模式')
        elif item.ptype == 'V3':
            box.prop(channel, 'v3val', text='颜色')
        else:
            box.prop(channel, 'fval', slider=True)
            box.prop(channel, 'fval', text='精确值')
        box.operator('cmb.param_default', icon='LOOP_BACK')
        layout.separator()
        row = layout.row(align=True)
        row.operator('cmb.save_params_preset', icon='EXPORT', text='保存参数预设')
        row.operator('cmb.load_params_preset', icon='FILE_BLEND', text='载入参数预设')
        if state.preset_note:
            layout.label(text=state.preset_note, icon='CHECKMARK')
        layout.separator()
        layout.label(text='最终包（分发用 · 可包含校正网格）')
        layout.operator('cmb.export_final_package', icon='PACKAGE')
        layout.label(text='选中移植结果对象；骨架/MMD 资产硬闸拒绝进包')


class CMB_PT_panel(bpy.types.Panel):
    bl_label = '角色材质与修改器移植'
    bl_idname = 'CMB_PT_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '材质移植'

    def draw(self, context):
        layout = self.layout
        settings = context.scene.cmb_settings
        layout.label(text='源工程：选择角色网格')
        layout.operator('cmb.export_package', icon='EXPORT')
        layout.separator()
        layout.label(text='目标工程：选择 MMD 角色网格')
        layout.operator('cmb.load_package', icon='FILE_BLEND')
        if settings.filepath:
            layout.label(text=bpy.path.basename(settings.filepath))
            preset_path = settings.filepath + core.PRESET_SUFFIX
            if os.path.exists(preset_path):
                layout.label(text='映射预设：' + bpy.path.basename(preset_path), icon='CHECKMARK')
            else:
                layout.label(text='映射预设：无')
        if settings.package_kind == 'final':
            # 0.5.0（V6）：final 包模式提示——名称弱证据匹配 + 主光修正已固化（A10）。
            layout.label(text='最终包模式：名称弱证据匹配；脸部主光修正已随包固化', icon='INFO')
        row = layout.row(align=True)
        row.operator('cmb.save_preset', icon='EXPORT', text='保存预设')
        row.operator('cmb.load_preset', icon='FILE_BLEND', text='载入预设')
        layout.label(text='预设按目标材质匹配；跨包按源组合身份核验')
        if settings.mapping_preset_note:
            for line in settings.mapping_preset_note.splitlines():
                layout.label(text=line, icon='INFO')
        layout.operator('cmb.preview', icon='VIEWZOOM')
        if settings.rows:
            layout.template_list('CMB_UL_rows', '', settings, 'rows', settings, 'index', rows=7)
            if 0 <= settings.index < len(settings.rows):
                item = settings.rows[settings.index]
                box = layout.box()
                box.label(text=item.object_name + ' / ' + item.material_name)
                box.label(text=item.status + ('：' + item.reason if item.reason else ''))
                box.label(text='分数用于排序，并非正确率')
                try:
                    candidates = json.loads(item.evidence)[:3]
                except ValueError:
                    candidates = []
                for candidate in candidates:
                    box.label(text=f"{candidate['id']}  {candidate['score']:.1f}  {candidate['source']}")
                    box.label(text=candidate['object'])
                box.label(text='为当前区域选择源侧“材质+修改器”组合；跳过=保留 MMD 原材质')
                box.prop(item, 'choice', text='确认源组合')
        layout.prop(settings, 'rebuild_tangents')
        layout.prop(settings, 'rebuild_outline_smoothing')
        if settings.package_kind != 'final':
            layout.prop(settings, 'transfer_source_uv')
        if settings.package_kind == 'final':
            layout.prop(settings, 'restore_final_mesh')
        layout.prop(settings, 'fix_face_light')
        layout.operator('cmb.apply', icon='GEOMETRY_NODES')
        layout.label(text='未确认项保留原材质；原角色保留并隐藏')
        if runtime.LAST_WARNING:
            layout.label(text=runtime.LAST_WARNING, icon='ERROR')
        layout.operator('cmb.repair_tangents', icon='FILE_REFRESH')
        layout.operator('cmb.rebuild_outline_smoothing', icon='GEOMETRY_NODES')
        layout.operator('cmb.restore', icon='LOOP_BACK')
        layout.operator('cmb.report', icon='TEXT')


class CMB_OT_apply_recommended_defaults(bpy.types.Operator):
    bl_idname = 'cmb.apply_recommended_defaults'
    bl_label = '应用部位默认值'
    bl_description = '更新所选网格使用的材质；共享同一参数列的材质也会同步'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        try:
            report = defaults.apply(context.selected_objects, context.scene, undo_checkpoint=True)
            rebuild_param_rows(context.scene.cmb_params, context)
            core.write_report({'recommended_defaults':report})
            self.report({'WARNING'} if report['warnings'] else {'INFO'},
                        '已更新 %d 个材质；%d 个冲突/未确定部位' % (report['materials'], len(report['warnings'])))
            return {'FINISHED'}
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}


class CMB_OT_post_enable(bpy.types.Operator):
    bl_idname = 'cmb.post_enable'
    bl_label = '启用终末地通用后处理'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            report = postprocess.install(context.scene)
            self.report({'INFO'} if report['status'] in {'installed','already_installed'} else {'WARNING'},
                        report.get('message', '终末地通用后处理已启用'))
            return {'FINISHED'} if report['status'] in {'installed','already_installed'} else {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}


class CMB_OT_post_restore(bpy.types.Operator):
    bl_idname = 'cmb.post_restore'
    bl_label = '恢复原后处理'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        report = postprocess.restore(context.scene)
        self.report({'INFO'} if report['status']=='restored' else {'WARNING'}, report.get('message','已恢复原后处理'))
        return {'FINISHED'} if report['status']=='restored' else {'CANCELLED'}


class CMB_OT_post_reset(bpy.types.Operator):
    bl_idname = 'cmb.post_reset'
    bl_label = '恢复后处理默认值'
    bl_description = '从内置人工 v6 资产恢复全部参数、节点顺序、色相和 RGB 曲线'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return postprocess.installed(context.scene)

    def execute(self, context):
        try:
            report = postprocess.install(context.scene, force=True)
            self.report({'INFO'} if report['status']=='installed' else {'WARNING'},report.get('message','已恢复人工 v6 后处理默认值'))
            return {'FINISHED'} if report['status']=='installed' else {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc)); return {'CANCELLED'}


class CMB_PT_post(bpy.types.Panel):
    bl_label = '终末地通用后处理'
    bl_idname = 'CMB_PT_post'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '材质移植'

    def draw(self, context):
        layout = self.layout
        stage = postprocess.stage_node(context.scene)
        if stage is None:
            layout.operator('cmb.post_enable')
            return
        for socket in stage.inputs:
            if socket.type == 'VALUE' and not socket.is_linked:
                layout.prop(socket, 'default_value', text=socket.name)
        if postprocess.unbound_render_inputs(context.scene):
            layout.operator('cmb.post_enable', text='绑定当前渲染场景')
        layout.label(text='色相、RGB 曲线及分区校色可进入节点组编辑')
        layout.operator('cmb.post_reset')
        layout.operator('cmb.post_restore')


def _sun_poll(self, obj):
    return obj.type == 'LIGHT' and obj.data.type == 'SUN'


def _arm_poll(self, obj):
    return obj.type == 'ARMATURE'


class CMB_RuntimeSettings(bpy.types.PropertyGroup):
    preview_sync: BoolProperty(name='实时预览同步', default=True, options=set(),
        description='关闭时暂停材质桥的自动视口同步；手动调参、渲染、保存和导出仍更新当前值，不影响 Blender 原生骨骼和物理',
        update=runtime.preview_changed)
    main_sun: PointerProperty(name='主日光', type=bpy.types.Object, poll=_sun_poll,
                              update=runtime.settings_changed,
                              description='留空时按名称排序自动选择可见 Sun')


class CMB_RigBinding(bpy.types.PropertyGroup):
    armature: PointerProperty(name='基座骨架', type=bpy.types.Object, poll=_arm_poll, update=rig_runtime.binding_updated)
    bone: StringProperty(name='基座骨骼', update=rig_runtime.binding_updated)


class CMB_OT_runtime_initialize(bpy.types.Operator):
    bl_idname='cmb.runtime_initialize'
    bl_label='初始化／修复独立同步'
    bl_description='隔离移植结果的参数图和灯表，绑定头骨并修复 Face 描边亮度；保留参数及动画'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        try:
            report=lighting.initialize(context.scene)
        except Exception as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        # Initialization is committed and must retain its undo step even if a
        # subsequent UI refresh/report fails.
        synced=runtime._safe_sync(context.scene,force=True)
        report['warnings'].extend(synced['warnings'])
        try:
            rebuild_param_rows(context.scene.cmb_params,context)
        except Exception as exc:
            report['warnings'].append('参数面板刷新失败：'+str(exc))
        try:
            core.write_report({'runtime_initialization':report})
        except Exception as exc:
            report['warnings'].append('报告写入失败：'+str(exc))
        runtime.LAST_WARNING='；'.join(dict.fromkeys(report['warnings']))
        repaired=len(report.get('face_outline',{}).get('repaired',[]))
        self.report({'WARNING'} if report['warnings'] else {'INFO'},'已初始化 %d 个移植对象；修复 %d 个 Face 描边材质；%d 条提示'%(report['objects'],repaired,len(report['warnings'])))
        return {'FINISHED'}


class CMB_OT_runtime_sync(bpy.types.Operator):
    bl_idname='cmb.runtime_sync'
    bl_label='立即同步当前帧'

    @classmethod
    def poll(cls,context):
        return bool(context.scene and context.scene.get('cmb_runtime_ready'))

    def execute(self,context):
        report=runtime._safe_sync(context.scene,force=True)
        self.report({'WARNING'} if report['warnings'] else {'INFO'},runtime.LAST_WARNING or '灯光、基座、相机及参数已同步')
        return {'FINISHED'}


class CMB_PT_runtime(bpy.types.Panel):
    bl_label='独立运行同步'
    bl_idname='CMB_PT_runtime'
    bl_space_type='VIEW_3D'
    bl_region_type='UI'
    bl_category='材质移植'

    def draw(self,context):
        layout=self.layout;scene=context.scene
        layout.prop(scene.cmb_runtime_settings,'preview_sync')
        if not scene.cmb_runtime_settings.preview_sync:
            layout.label(text='视口同步已暂停；渲染仍自动同步',icon='PAUSE')
            layout.label(text='手动调参、保存和导出仍生效')
        if scene.get('cmb_runtime_ready'):
            layout.label(text='渲染时锁定界面，保护逐帧同步',icon='LOCKED')
        layout.label(text='已启用独立同步' if scene.get('cmb_runtime_ready') else '已有工程需先初始化独立同步')
        layout.prop(scene.cmb_runtime_settings,'main_sun')
        layout.label(text='主日光留空：按名称排序自动选择')
        obj=context.object
        if obj and lighting.owned(obj) and rig_runtime.object_needs_basis(obj):
            settings=obj.cmb_rig_binding
            layout.prop(settings,'armature')
            if settings.armature and settings.armature.type=='ARMATURE':layout.prop_search(settings,'bone',settings.armature.data,'bones')
            else:layout.prop(settings,'bone')
        layout.operator('cmb.runtime_initialize')
        layout.operator('cmb.runtime_sync')
        if runtime.LAST_WARNING:
            for text in runtime.LAST_WARNING.split('；')[:4]:layout.label(text=text,icon='INFO')


CLASSES = (CMB_RuntimeSettings, CMB_RigBinding, CMB_Row, CMB_Settings, CMB_OT_export, CMB_OT_load, CMB_OT_save_preset,
           CMB_OT_load_preset, CMB_OT_preview, CMB_OT_apply, CMB_OT_repair_tangents,
           CMB_OT_outline_smoothing, CMB_OT_restore, CMB_OT_report,
           CMB_UL_rows, CMB_PT_panel,
           CMB_ParamRow, CMB_AnimChannel, CMB_ParamState, CMB_UL_params, CMB_OT_param_refresh,
           CMB_OT_param_default, CMB_OT_save_params_preset, CMB_OT_load_params_preset,
           CMB_OT_export_final, CMB_PT_params, CMB_OT_apply_recommended_defaults,
           CMB_OT_post_enable, CMB_OT_post_restore, CMB_OT_post_reset, CMB_PT_post,
           CMB_OT_runtime_initialize, CMB_OT_runtime_sync, CMB_PT_runtime, CMB_OT_outline_width)


def _cleanup_failed_registration():
    """Release our 0.5.6/0.5.7 failed-enable remnants, without reading scenes.

    addon_utils removes a module after register() fails, but leaves its RNA
    classes and handlers alive. The old callback still owns that module's
    globals, so its unregister() can release the exact classes it registered.
    This is installation cleanup, not saved-setting migration.
    """
    seen = set()
    for handler in reversed(tuple(bpy.app.handlers.load_post)):
        if (getattr(handler, '__module__', '') != __name__ or
                getattr(handler, '__name__', '') != '_migrate_legacy_param_filters'):
            continue
        namespace = handler.__globals__
        if namespace is globals() or namespace.get('__addon_enabled__', False):
            continue
        if id(namespace) not in seen:
            seen.add(id(namespace))
            cleanup = namespace.get('unregister')
            if callable(cleanup):
                cleanup()
        if handler in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.remove(handler)


def register():
    # 0.5.0 双开守卫（A06）：两版共用全部 bl_idname 与 Scene 键，先后启用会半注册互删、
    # 除重启外不可恢复。对方模块已加载且其面板已在 bpy.types 时拒绝启用（单版安装/
    # 正常使用不触发、零行为差；对方模块名在运行时按本模块名判定，两版源码同源）。
    other = sys.modules.get('character_material_bridge_user'
                            if __name__ == 'character_material_bridge' else 'character_material_bridge')
    other_panel = getattr(other, 'CMB_PT_panel', None) if other is not None else None
    if other_panel is not None and getattr(bpy.types, 'CMB_PT_panel', None) is other_panel:
        raise RuntimeError('角色材质桥：开发版与用户版不能同时启用，请先停用另一版再开启本版')
    _cleanup_failed_registration()
    registered, properties = [], []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
        bpy.types.Scene.cmb_settings = PointerProperty(type=CMB_Settings)
        properties.append('cmb_settings')
        bpy.types.Scene.cmb_params = PointerProperty(type=CMB_ParamState)
        properties.append('cmb_params')
        bpy.types.Scene.cmb_anim_channels = CollectionProperty(type=CMB_AnimChannel)
        properties.append('cmb_anim_channels')
        bpy.types.Scene.cmb_runtime_settings = PointerProperty(type=CMB_RuntimeSettings)
        properties.append('cmb_runtime_settings')
        bpy.types.Object.cmb_rig_binding = PointerProperty(type=CMB_RigBinding)
        defaults.register()
        runtime.register()
        animation.register()
    except Exception:
        # Only roll back this attempt; never leave half-registered UI after an
        # install error, or unregister classes owned by a different attempt.
        runtime.unregister()
        animation.unregister()
        defaults.unregister()
        if hasattr(bpy.types.Object,'cmb_rig_binding'):del bpy.types.Object.cmb_rig_binding
        for name in reversed(properties):
            delattr(bpy.types.Scene, name)
        for cls in reversed(registered):
            bpy.utils.unregister_class(cls)
        raise


def unregister():
    animation.unregister()
    defaults.unregister()
    runtime.unregister()
    lighting.clear()
    if hasattr(bpy.types.Scene,'cmb_runtime_settings'):del bpy.types.Scene.cmb_runtime_settings
    if hasattr(bpy.types.Object,'cmb_rig_binding'):del bpy.types.Object.cmb_rig_binding
    # 0.5.0 防御化（A06）：半注册态下 disable 不悬挂——Scene 键 del 前、类 unregister
    # 前均先探测存在性（键/类可能已被对方版本或上次失败的注册流程移除）。
    if hasattr(bpy.types.Scene, 'cmb_params'):
        del bpy.types.Scene.cmb_params
    if hasattr(bpy.types.Scene, 'cmb_anim_channels'):
        del bpy.types.Scene.cmb_anim_channels
    if hasattr(bpy.types.Scene, 'cmb_settings'):
        del bpy.types.Scene.cmb_settings
    for cls in reversed(CLASSES):
        # bpy.types 对 PropertyGroup 与部分 operator 类不暴露（hasattr 恒 False），不能
        # 只靠命名空间探测：可见类直接卸载；不可见类尝试卸载并容忍「未注册」（半注册
        # 态下不悬挂，也保证正常卸载把不可见类卸全）。
        exposed = hasattr(bpy.types, cls.__name__) or (getattr(cls, 'bl_idname', '')
                                                       and hasattr(bpy.types, cls.bl_idname))
        if exposed:
            bpy.utils.unregister_class(cls)
        else:
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass
    _enum_cache.clear()
    _group_cache.clear()
