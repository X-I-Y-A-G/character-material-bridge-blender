"""Offline unit tests for the v2 matching core (plain python, no Blender).

Run:  python dev/test_match_v2.py

STUB NOTE: core.py does `import bpy` at module level only. These tests exercise only
bpy-free code paths (rank_rows / build_preset / apply_preset / row_menu_items), so a
single empty module registered as 'bpy' before import is sufficient; the stub provides
exactly the import-time name and nothing else. core.py is loaded directly by file path
so the package __init__ (which needs real bpy.props) is never imported.
"""
import sys
import types

if 'bpy' not in sys.modules:  # minimal import-time stub; see file header
    sys.modules['bpy'] = types.ModuleType('bpy')

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'cmb_core', Path(__file__).resolve().parents[1] / 'character_material_bridge' / 'core.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def entry(id_, material, object_name, textures=(), uv=(), **extra):
    data = {'id': id_, 'carrier': 'C_' + id_, 'source_object': object_name,
            'source_material': material, 'slot': 0, 'slot_used': True,
            'textures': list(textures), 'uv_histogram': list(uv)}
    data.update(extra)
    return data


def row(object_name, material, textures=(), uv=(), slot=0):
    return {'object': object_name, 'slot': slot, 'material': material,
            'textures': list(textures), 'uv_histogram': list(uv)}


def test_t3_uv_decisive_after_dilution():
    """Shared atlas dilutes texture evidence (3 materials -> 21.67); T3 must still fire."""
    manifest = {'entries': [
        entry('a:0', 'M_a', 'S_a', ['atlas_d'], [0.9]),
        entry('b:0', 'M_b', 'S_b', ['atlas_d'], [0.3]),
        entry('c:0', 'M_c', 'S_c', ['atlas_d'], [0.1]),
    ]}
    rows = core.rank_rows([row('O', '区域', ['atlas_d'], [0.9])], manifest)
    assert rows[0]['choice'] == 'a:0', rows[0]
    assert rows[0]['status'] == '自动·UV', rows[0]['status']
    assert rows[0]['candidates'][0]['score'] < core.AUTO_SCORE  # T2 was indeed unreachable


def test_t4_unique_name_bridge_without_textures():
    """T4 fires on a unique MATERIAL-name bridge with zero texture evidence.

    0.2.2 起 eye 系词条已撤，改用仍保留的「睫眉→eyebrow」词条验证 T4 机制本身。"""
    manifest = {'entries': [
        entry('a:0', 'M_plain_eyebrow_a', 'S_actor_x_brow_01', ['t_brow_m']),
        entry('b:0', 'M_plain_b', 'S_actor_x_body_01', ['t_body_d']),
        entry('c:0', 'M_plain_c', 'S_actor_x_hair_01', ['t_hair_d']),
    ]}
    rows = core.rank_rows([row('O', '睫眉', [], [])], manifest)
    assert rows[0]['choice'] == 'a:0', rows[0]
    assert rows[0]['status'] == '自动·名称', rows[0]['status']
    assert rows[0]['candidates'][0]['bridge_kind'] == 'material', rows[0]['candidates'][0]


def test_multi_bridge_ties_do_not_auto():
    """Two candidates carry the same bridge word: uniqueness fails, row stays manual."""
    manifest = {'entries': [
        entry('a:0', 'M_hairshadow_one', 'S_one_hairshadow', ['t_s1']),
        entry('b:0', 'M_hairshadow_two', 'S_two_hairshadow', ['t_s2']),
        entry('c:0', 'M_plain', 'S_plain', ['t_p']),
    ]}
    rows = core.rank_rows([row('O', '发影', [], [])], manifest)
    assert rows[0]['choice'] == '', rows[0]
    assert rows[0]['status'].startswith('待确认'), rows[0]['status']


def test_t5_never_excludes_already_used_recipes():
    manifest = {'entries': [
        entry('a:0', 'M_a', 'S_a', ['k_d', 'k2'], [0.9, 0.0]),
        entry('b:0', 'M_b', 'S_b', ['k_d'], [0.0, 0.9]),
        entry('c:0', 'M_c', 'S_c', ['k_d'], [0.4, 0.4]),
    ]}
    rows = core.rank_rows([
        row('O', 'R0', ['k_d', 'k2'], [0.9, 0.0], slot=0),
        row('O', 'R1', ['k_d'], [0.0, 0.9], slot=1),
        row('O', 'R2', ['k_d'], [0.2, 0.2], slot=2),
    ], manifest)
    assert rows[0]['choice'] == 'a:0' and rows[0]['status'] == '自动·评分', rows[0]
    assert rows[1]['choice'] == 'b:0' and rows[1]['status'] == '自动·UV', rows[1]
    assert rows[2]['choice'] == '', rows[2]
    assert rows[2]['status'].startswith('待确认'), rows[2]['status']


def test_eyewhite_texture_evidence_ranks_top_after_dict_removal():
    """0.2.2 eye 词典撤除后：目白无名称桥接，贴图证据（脸图集）排首位但分数不足，保持人工。
    （用户实测：目白区域实际就在脸图集上，M_eyewhiteshadow_* 名实相反，不再被桥接抬正。）"""
    manifest = {'entries': [
        entry('face:0', 'M_actor_face_01', 'S_actor_face_01', ['face_d'], [0.1, 0.0]),
        entry('brow:0', 'M_actor_brow_01', 'S_actor_brow_01', ['face_d'], [0.05, 0.0]),
        entry('white:0', 'M_eyewhiteshadow_common_01', 'S_actor_eyeshadow_01', ['shadow_m'], [0.2, 0.0]),
    ]}
    rows = core.rank_rows([row('O', '目白', ['face_d'], [0.1, 0.0])], manifest)
    assert rows[0]['choice'] == '', rows[0]
    assert rows[0]['status'].startswith('待确认'), rows[0]['status']
    top = rows[0]['candidates'][0]
    assert top['id'] == 'face:0' and top['bridge'] == '', top  # 贴图证据首位、无桥接加成
    assert rows[0]['candidates'][1]['id'] == 'brow:0'


def test_eyewhite_face_atlas_top_stays_manual():
    """目白 real-data shape: shared-atlas evidence puts 009:000 (face) first, margin over
    brow is small, no auto — 目白回归人工+预设（用户实测其首选即 009:000 脸图集）。"""
    uv_face = [0.1355] + [0.0] * 255
    uv_brow = [0.01] + [0.0] * 255
    manifest = {'entries': [
        entry('009:000', 'M_actor_lizhiyan_face_01', 'S_actor_lizhiyan_face_01_lod0',
              ['t_actor_lizhiyan_face_01_d'], uv_face),
        entry('007:000', 'M_actor_lizhiyan_brow_01', 'S_actor_lizhiyan_eyebrow_01_lod0',
              ['t_actor_lizhiyan_face_01_d'], uv_brow),
        entry('008:000', 'M_eyewhiteshadow_common_01', 'S_actor_lizhiyan_eyeshadow_01_lod0',
              ['t_actor_common_eyeshadow_01_m'], [0.2] + [0.0] * 254),
    ]}
    rows = core.rank_rows([row('O', '目白', ['t_actor_lizhiyan_face_01_d'], uv_face)], manifest)
    top, second = rows[0]['candidates'][0], rows[0]['candidates'][1]
    assert top['id'] == '009:000' and abs(top['score'] - 37.92) < 0.01, top
    assert second['id'] == '007:000', second
    assert rows[0]['choice'] == '', rows[0]
    assert rows[0]['status'].startswith('待确认'), rows[0]['status']


def test_eyeshadow_no_bridge_no_evidence_never_auto():
    """0.2.2 eye 词典撤除后：目影无贴图证据也无桥接（v1 包仅剩对象名命中，已无词条可命），
    不得自动——用户实测证明 M_eyewhiteshadow_* 实际承担目影，名称映射不可信。"""
    manifest = {'entries': [
        entry('008:000', 'M_eyewhiteshadow_common_01', 'S_actor_lizhiyan_eyeshadow_01_lod0',
              ['t_actor_common_eyeshadow_01_m']),
    ]}
    rows = core.rank_rows([row('O', '目影', [], [])], manifest)
    assert rows[0]['choice'] == '', rows[0]
    assert rows[0]['status'] == '无匹配', rows[0]['status']
    assert all(c['bridge'] == '' for c in rows[0]['candidates']), rows[0]['candidates']


def test_eyeshadow_v2_package_still_never_auto():
    """v2 包的 008:001（M_eyeshadow_common_03）存在也不改变：目影无贴图证据且无桥接 →
    无匹配（0.2.0 时代的『自动·名称/008:001』是 eye 词条误导，本条为故意的行为变化）。"""
    manifest = {'entries': [
        entry('008:000', 'M_eyewhiteshadow_common_01', 'S_actor_lizhiyan_eyeshadow_01_lod0',
              ['t_actor_common_eyeshadow_01_m']),
        entry('008:001', 'M_eyeshadow_common_03', 'S_actor_lizhiyan_eyeshadow_01_lod0',
              ['t_actor_common_eyeshadow_01_m'], slot_used=False),
    ]}
    rows = core.rank_rows([row('O', '目影', [], [])], manifest)
    assert rows[0]['choice'] == '', rows[0]
    assert rows[0]['status'] == '无匹配', rows[0]['status']
    assert all(c['bridge'] == '' for c in rows[0]['candidates']), rows[0]['candidates']


def test_tangent_sign_statistics_are_not_calibration():
    """0.5.1: legacy statistics remain readable but cannot justify a target flip."""
    # 源脸实测 9525/9576 负（99.5%）→ 众数 -1
    assert core.tangent_sign_mode([-1.0] * 9525 + [1.0] * 51) == -1
    assert core.tangent_sign_mode([1.0, 1.0, -1.0]) == 1
    assert core.tangent_sign_mode([]) is None
    rebuilt = [1.0] * 5106  # MikkTSpace 重建结果全 +1（用户实测）
    flip = core.tangent_flip_count(-1, rebuilt)
    assert flip == 0, 'Legacy majority is not a tangent convention'
    flipped = [-v for v in rebuilt] if flip else rebuilt
    assert all(v == 1.0 for v in flipped)
    assert core.tangent_flip_count(1, rebuilt) == 0   # 同众数 → 不动
    assert core.tangent_flip_count(None, rebuilt) == 0  # 旧包无键 → 不动
    assert core.tangent_flip_count(-1, []) == 0


def test_t1_exact_name_and_bridge_exclusion():
    manifest = {'entries': [entry('a:0', 'M_Actor_Face_01', 'S_face', ['face_d'])]}
    rows = core.rank_rows([row('O', 'M_actor_face_01.001', ['face_d'], [1.0])], manifest)
    assert rows[0]['choice'] == 'a:0'
    assert rows[0]['status'] == '自动·精确名', rows[0]['status']
    assert rows[0]['candidates'][0]['bridge'] == ''  # exact suppresses the bridge bonus


def test_v1_manifest_and_unused_slot_entries():
    v1 = {'entries': [entry('a:0', 'M_a', 'S_a', ['k_d'], [0.9])]}
    for e in v1['entries']:
        del e['slot_used']  # v1 manifests carry no slot_used flag
    rows = core.rank_rows([row('O', 'R', ['k_d'], [0.9])], v1)
    assert rows[0]['choice'] == 'a:0' and rows[0]['status'] == '自动·评分'
    v2 = {'entries': [entry('a:0', 'M_a', 'S_a', ['k_d'], [0.9], slot_used=True),
                      entry('a:1', 'M_unused', 'S_a', ['u_d'], [], slot_used=False)]}
    rows = core.rank_rows([row('O', 'R', ['k_d'], [0.9])], v2)
    ids = [c['id'] for c in rows[0]['candidates']]
    assert 'a:1' in ids  # slot_used=False stays visible as a candidate (uv=0)
    assert rows[0]['choice'] == 'a:0'


def test_preset_round_trip():
    rows = [row('A', '面', slot=0), row('A', '目白', slot=1), row('A', '口内', slot=2)]
    rows[0]['choice'], rows[1]['choice'], rows[2]['choice'] = '009:000', '', 'SKIP'
    payload = core.build_preset(rows, 'pkg-1', '0.2.0')
    assert payload['map']['A::面'] == '009:000'
    assert payload['map']['A::目白'] == '' and payload['map']['A::口内'] == ''
    fresh = [row('A', '面', slot=0), row('A', '目白', slot=1), row('A', '口内', slot=2)]
    for item in fresh:
        item['choice'] = '999:999'
    changed = core.apply_preset(payload, fresh)
    assert changed == 3
    assert [r['choice'] for r in fresh] == ['009:000', '', '']
    renamed = [row('B', '面', slot=0)]  # object key miss -> by_material fallback
    renamed[0]['choice'] = 'x:0'
    core.apply_preset(payload, renamed)
    assert renamed[0]['choice'] == '009:000'


def test_row_menu_items_constant_order():
    # 0.4.1 根治 cmb.preview 错位（t4 阻断 2）：items 顺序必须与行证据彻底解耦——
    # 恒定 = SKIP + 全部条目按 id 升序；证据只装饰 label（分数/提示），损坏证据只
    # 降级 label。Blender 动态枚举按 int 索引存取，顺序随证据变化的帧差即错位根源。
    entries = [entry('b:0', 'M_b', 'S_b'), entry('a:0', 'M_a', 'S_a')]
    evidence = json.dumps([{'id': 'b:0', 'score': 72.4, 'exact_name': False,
                            'bridge': '', 'textures': ['x'], 'uv_similarity': 0.9}])
    items = core.row_menu_items(evidence, entries)
    assert [i[0] for i in items] == ['SKIP', 'a:0', 'b:0']
    assert '72.4' in items[2][1] and items[2][2] == 'b:0' and 'UV' in items[2][1]
    assert '72.4' not in items[1][1] and items[1][2] == 'a:0'
    for broken in ('', '{"broken":', '{"not": "a list"}'):
        plain = core.row_menu_items(broken, entries)
        assert [i[0] for i in plain] == ['SKIP', 'a:0', 'b:0'], broken
        assert '72.4' not in plain[2][1], broken


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith('test_')]
    for test in tests:
        test()
        print('PASS', test.__name__, flush=True)
    print(f'MATCH_V2_TESTS_OK {len(tests)}/{len(tests)}', flush=True)


if __name__ == '__main__':
    main()
