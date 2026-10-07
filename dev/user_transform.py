"""User-edition source transformer for character_material_bridge 0.5.0 (fused-spec-v2 §1/§2).

纯函数模块（无 IO）。user 版与 dev 版的差异 100% 在构建期以"删除/替换"实现
（I5：user = 源码树 − 删除集，不允许任何新增代码逻辑）。每条规则按声明的命中
数断言，失配即 BuildError（fail loud，C2）；全部锚点为结构锚（AST 定位顶层
ClassDef/FunctionDef/Assign/Dict 键）或精确文本子串，零行号锚定。U7/U9
（separator delete_line_exact）已按 S4 攻击裁决 A01 撤销——separator 保留。

四类规则：
  delete_node                 AST 顶层定位（含装饰器行区间），原文行区间整切；
  delete_statement_containing 最小语句（整句，含多行语句整跨）删除；
  delete_if_block_containing  含锚文本的最内层 If 整块删除；
  replace_text / replace_bl_info_values / delete_import  精确文本/AST 键值替换。

源码演化导致锚失配时构建失败而非静默错切（A01/A02 的教训）。
"""
import ast

__all__ = ['BuildError', 'transform_user', 'transform_user_verbose',
           'USER_NAME', 'USER_LOCATION', 'USER_DESCRIPTION',
           'DELETED_INIT_SYMBOLS', 'DELETED_CORE_SYMBOLS', 'RULE_TABLE']


class BuildError(Exception):
    """A transform rule did not hit the source exactly as declared (fail loud, C2)."""


# U15：addon 身份（fused-spec-v2 §2/§3）。description 为用户视角，不含「导出」。
USER_NAME = 'Character Material Bridge User / 角色材质桥（用户版）'
USER_LOCATION = 'View3D > Sidebar > 材质移植（用户版）'
USER_DESCRIPTION = '请注意阅读RM,插件可快捷导入终末地角色材质。'

# 检查③用：被删符号全集（__init__ 侧 + core 侧）。
DELETED_INIT_SYMBOLS = frozenset({'CMB_OT_export', 'CMB_OT_export_final'})
DELETED_CORE_SYMBOLS = frozenset({'export_package', 'export_final_package', '_final_mapper',
                                  'LibraryBuilder', 'requirements', 'SAFE_MODS',
                                  'clear_custom', 'remap_rna', 'remap_custom', 'uuid'})

# 规则表（fused-spec-v2 §2；U7/U9 撤销）。规则按表序对当前文本依次应用——
# U6/U11 的语句锚在 U1/U2 删除类定义之后才唯一，表序不可乱。
RULE_TABLE = {
    '__init__.py': [
        {'id': 'U1', 'kind': 'delete_node', 'node_type': 'class', 'name': 'CMB_OT_export',
         'expected': 1},
        {'id': 'U2', 'kind': 'delete_node', 'node_type': 'class', 'name': 'CMB_OT_export_final',
         'expected': 1},
        {'id': 'U3', 'kind': 'replace_text', 'old': 'CMB_OT_export, ', 'new': '', 'expected': 1},
        {'id': 'U4', 'kind': 'replace_text', 'old': 'CMB_OT_export_final, ', 'new': '',
         'expected': 1},
        {'id': 'U5', 'kind': 'delete_statement_containing', 'needle': '源工程：选择角色网格',
         'expected': 1},
        {'id': 'U6', 'kind': 'delete_statement_containing', 'needle': 'cmb.export_package',
         'expected': 1},
        {'id': 'U8', 'kind': 'delete_if_block_containing', 'needle': '确认源组合', 'expected': 1},
        {'id': 'U10', 'kind': 'delete_statement_containing',
         'needle': '最终包（分发用 · 可包含校正网格）', 'expected': 1},
        {'id': 'U11', 'kind': 'delete_statement_containing',
         'needle': 'cmb.export_final_package', 'expected': 1},
        {'id': 'U12', 'kind': 'delete_statement_containing',
         'needle': '选中移植结果对象；骨架/MMD 资产硬闸拒绝进包', 'expected': 1},
        # A02 修正：:27 行还含 load_layout conjunct，整行匹配 0 命中——改删子串，布尔链仍合法。
        {'id': 'U13', 'kind': 'replace_text',
         'old': " and hasattr(core, 'export_final_package')", 'new': '', 'expected': 1},
        # 守卫注释里的 core.export_final_package 提及同步移除（文本级禁词归零）。
        {'id': 'U14', 'kind': 'replace_text',
         'old': '（参数引擎）与 core.export_final_package（最终包）', 'new': '（参数引擎）',
         'expected': 1},
        # AST 定位 bl_info Dict 键，值替换为用户版常量（name/location/description）。
        {'id': 'U15', 'kind': 'replace_bl_info_values',
         'values': {'name': USER_NAME, 'location': USER_LOCATION,
                    'description': USER_DESCRIPTION}, 'expected': 3},
        {'id': 'U16', 'kind': 'delete_if_block_containing',
         'needle': '修复脸部/身体描边亮度', 'expected': 1},
    ],
    'core.py': [
        {'id': 'K1', 'kind': 'delete_import', 'module': 'uuid', 'expected': 1},
        {'id': 'K2', 'kind': 'delete_node', 'node_type': 'assign', 'name': 'SAFE_MODS',
         'expected': 1},
        {'id': 'K3', 'kind': 'delete_node', 'node_type': 'function', 'name': 'requirements',
         'expected': 1},
        {'id': 'K4', 'kind': 'delete_node', 'node_type': 'function', 'name': 'clear_custom',
         'expected': 1},
        {'id': 'K5', 'kind': 'delete_node', 'node_type': 'function', 'name': 'remap_rna',
         'expected': 1},
        {'id': 'K6', 'kind': 'delete_node', 'node_type': 'function', 'name': 'remap_custom',
         'expected': 1},
        {'id': 'K7', 'kind': 'delete_node', 'node_type': 'class', 'name': 'LibraryBuilder',
         'expected': 1},
        {'id': 'K8', 'kind': 'delete_node', 'node_type': 'function', 'name': '_final_mapper',
         'expected': 1},
        {'id': 'K9', 'kind': 'delete_node', 'node_type': 'function', 'name': 'export_package',
         'expected': 1},
        {'id': 'K10', 'kind': 'delete_node', 'node_type': 'function', 'name': 'export_final_package',
         'expected': 1},
    ],
}


def _fail(filename, rule_id, message):
    raise BuildError(f'{filename}/{rule_id}: {message}')


def _parse(text, filename, rule_id):
    try:
        return ast.parse(text)
    except SyntaxError as exc:
        _fail(filename, rule_id, f'source does not parse: {exc}')


def _lines(text):
    return text.splitlines(keepends=True)


def _span(node):
    """(start_line, start_col, end_line, end_col)，含装饰器行。"""
    start_line, start_col = node.lineno, node.col_offset
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
        start_line = min([start_line] + [d.lineno for d in node.decorator_list])
        start_col = min([start_col] + [d.col_offset for d in node.decorator_list])
    return start_line, start_col, node.end_lineno, node.end_col_offset


def _delete_spans(text, spans, filename, rule_id):
    """原文行区间切除：整行独占校验（前后只允许空白）+ 区间不重叠，否则 BuildError。

    ast 的 col_offset 是 UTF-8 **字节**偏移——含中文的行上字节偏移≠字符偏移，
    故校验与拼接一律在 utf-8 字节串上进行。
    """
    data = text.encode('utf-8')
    lines = data.splitlines(keepends=True)
    ordered = sorted(spans)
    for index in range(len(ordered) - 1):
        if ordered[index][2] >= ordered[index + 1][0]:
            _fail(filename, rule_id, 'deletion line ranges overlap')
    for start_line, start_col, end_line, end_col in ordered:
        if not (1 <= start_line <= end_line <= len(lines)):
            _fail(filename, rule_id, f'span out of file bounds: {start_line}..{end_line}')
        if lines[start_line - 1][:start_col].strip():
            _fail(filename, rule_id,
                  f'line {start_line} has non-whitespace before the node (whole-line ownership violated)')
        tail = lines[end_line - 1][end_col:]
        if tail.strip():
            _fail(filename, rule_id,
                  f'line {end_line} has trailing non-whitespace after the node '
                  '(whole-line ownership violated)')
    for start_line, _start_col, end_line, _end_col in sorted(ordered, reverse=True):
        del lines[start_line - 1:end_line]
    return b''.join(lines).decode('utf-8')


def _inside(inner, outer):
    """inner 语句严格嵌套于 outer 语句（span 包含且不相等）。"""
    if inner is outer:
        return False
    ikey = (inner.lineno, inner.col_offset, inner.end_lineno, inner.end_col_offset)
    okey = (outer.lineno, outer.col_offset, outer.end_lineno, outer.end_col_offset)
    if ikey == okey:
        return False
    return (ikey[0], ikey[1]) >= (okey[0], okey[1]) and (ikey[2], ikey[3]) <= (okey[2], okey[3])


def _minimal_containing(tree, text, needle, filename, rule_id, types):
    """含锚文本的最小语句（types 限定节点类型，如全部 stmt 或仅 If）。"""
    lines = _lines(text)
    hits = [node for node in ast.walk(tree)
            if isinstance(node, types) and needle in ''.join(lines[node.lineno - 1:node.end_lineno])]
    return [node for node in hits if not any(_inside(other, node) for other in hits)]


def _apply_delete_node(text, tree, rule, filename):
    wanted_type, wanted_name = rule['node_type'], rule['name']
    matches = []
    for node in tree.body:
        if wanted_type == 'class' and isinstance(node, ast.ClassDef) and node.name == wanted_name:
            matches.append(node)
        elif (wanted_type == 'function' and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              and node.name == wanted_name):
            matches.append(node)
        elif wanted_type == 'assign' and isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == wanted_name for target in node.targets):
            matches.append(node)
    if len(matches) != rule['expected']:
        _fail(filename, rule['id'],
              f'expected {rule["expected"]} top-level {wanted_type} {wanted_name!r}, found {len(matches)}')
    return _delete_spans(text, [_span(node) for node in matches], filename, rule['id']), len(matches)


def _apply_delete_import(text, tree, rule, filename):
    matches = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            aliases = [alias for alias in node.names if alias.name == rule['module']]
            if aliases and len(node.names) != len(aliases):
                _fail(filename, rule['id'],
                      f'import {rule["module"]!r} shares its statement with other modules')
            if aliases:
                matches.append(node)
    if len(matches) != rule['expected']:
        _fail(filename, rule['id'],
              f'expected {rule["expected"]} top-level import of {rule["module"]!r}, '
              f'found {len(matches)}')
    return _delete_spans(text, [_span(node) for node in matches], filename, rule['id']), len(matches)


def _apply_delete_statement(text, tree, rule, filename):
    minimal = _minimal_containing(tree, text, rule['needle'], filename, rule['id'], ast.stmt)
    if len(minimal) != rule['expected']:
        _fail(filename, rule['id'],
              f'expected {rule["expected"]} minimal statement containing {rule["needle"]!r}, '
              f'found {len(minimal)}')
    return _delete_spans(text, [_span(node) for node in minimal], filename, rule['id']), len(minimal)


def _apply_delete_if_block(text, tree, rule, filename):
    minimal = _minimal_containing(tree, text, rule['needle'], filename, rule['id'], ast.If)
    if len(minimal) != rule['expected']:
        _fail(filename, rule['id'],
              f'expected {rule["expected"]} innermost if-block containing {rule["needle"]!r}, '
              f'found {len(minimal)}')
    return _delete_spans(text, [_span(node) for node in minimal], filename, rule['id']), len(minimal)


def _apply_replace_text(text, rule, filename):
    hits = text.count(rule['old'])
    if hits != rule['expected']:
        _fail(filename, rule['id'],
              f'expected {rule["expected"]} occurrence(s) of {rule["old"]!r}, found {hits}')
    return text.replace(rule['old'], rule['new']), hits


def _apply_bl_info(text, tree, rule, filename):
    values = rule['values']
    assign = next((node for node in tree.body
                   if isinstance(node, ast.Assign) and any(
                       isinstance(target, ast.Name) and target.id == 'bl_info'
                       for target in node.targets)), None)
    if assign is None or not isinstance(assign.value, ast.Dict):
        _fail(filename, rule['id'], 'bl_info module-level dict not found')
    data = text.encode('utf-8')  # ast col 是 UTF-8 字节偏移，拼接必须按字节
    lines = data.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    replacements, found = [], 0
    for key_node, value_node in zip(assign.value.keys, assign.value.values):
        if not (isinstance(key_node, ast.Constant) and key_node.value in values):
            continue
        if not (isinstance(value_node, ast.Constant) and isinstance(value_node.value, str)):
            _fail(filename, rule['id'], f'bl_info[{key_node.value!r}] value is not a string constant')
        start = offsets[value_node.lineno - 1] + value_node.col_offset
        end = offsets[value_node.end_lineno - 1] + value_node.end_col_offset
        replacements.append((start, end, repr(values[key_node.value]).encode('utf-8')))
        found += 1
    if found != rule['expected']:
        _fail(filename, rule['id'],
              f'expected {rule["expected"]} bl_info key replacement(s), matched {found}')
    for start, end, replacement in sorted(replacements, reverse=True):
        data = data[:start] + replacement + data[end:]
    return data.decode('utf-8'), found


_APPLICATORS = {
    'delete_node': lambda text, tree, rule, filename: _apply_delete_node(text, tree, rule, filename),
    'delete_import': lambda text, tree, rule, filename: _apply_delete_import(text, tree, rule, filename),
    'delete_statement_containing': lambda text, tree, rule, filename: _apply_delete_statement(text, tree, rule, filename),
    'delete_if_block_containing': lambda text, tree, rule, filename: _apply_delete_if_block(text, tree, rule, filename),
    'replace_text': lambda text, tree, rule, filename: _apply_replace_text(text, rule, filename),
    'replace_bl_info_values': lambda text, tree, rule, filename: _apply_bl_info(text, tree, rule, filename),
}


def transform_user_verbose(text, filename):
    """Apply the edition rules for `filename` ('__init__.py' | 'core.py').

    Returns (transformed_text, report)；report 逐规则自报命中数（编排层交叉复核用，⑤）。
    任一规则命中数 ≠ 声明值 → BuildError（fail loud，C2）。
    """
    rules = RULE_TABLE.get(filename)
    if rules is None:
        raise BuildError(f'no rule table for {filename!r} (expected one of {sorted(RULE_TABLE)})')
    applied = []
    for rule in rules:
        tree = _parse(text, filename, rule['id'])
        text, hits = _APPLICATORS[rule['kind']](text, tree, rule, filename)
        if hits != rule['expected']:  # 双保险：applicator 内已断言，此处再核一次
            _fail(filename, rule['id'], f'hits {hits} != expected {rule["expected"]}')
        applied.append({'id': rule['id'], 'kind': rule['kind'], 'hits': hits,
                        'expected': rule['expected']})
    _parse(text, filename, 'final')  # 变换后必须仍是合法 Python（结构损伤在此暴露）
    return text, {'filename': filename, 'rules': applied}


def transform_user(text, filename):
    """Pure: source text of one file -> user-edition text (no IO)."""
    return transform_user_verbose(text, filename)[0]
