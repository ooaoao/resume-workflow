"""Evaluate grouped AND/OR qualification requirements."""

def evaluate_logic(node, requirements, depth=0):
    """三态AND/OR；不适用项在组合中跳过，优先项不得成为硬门槛。"""
    if depth > 64 or not isinstance(node, dict):
        raise ValueError('资格逻辑须为有限AND/OR树')
    if set(node) == {'ref'}:
        row = requirements.get(node['ref'])
        if row is None or row['requirement_type'] == 'priority':
            raise ValueError('资格逻辑引用缺失或优先条款')
        if row['applicability'] == 'not_applicable':
            return 'NOT_APPLICABLE'
        if row['applicability'] == 'unknown' or row['requirement_type'] == 'ambiguous':
            return 'UNCERTAIN'
        return {'confirmed_met': 'PASS', 'confirmed_not_met': 'FAIL', 'unknown': 'UNCERTAIN'}[row['candidate_status']]
    if set(node) != {'op', 'children'} or node['op'] not in {'AND', 'OR'} or (not isinstance(node['children'], list)):
        raise ValueError('资格逻辑只接受ref或op/children')
    if node['op'] == 'OR' and (not node['children']):
        raise ValueError('OR不得没有替代路径')
    values = [evaluate_logic(child, requirements, depth + 1) for child in node['children']]
    values = [v for v in values if v != 'NOT_APPLICABLE']
    if not values:
        return 'PASS'
    if node['op'] == 'AND':
        return 'FAIL' if 'FAIL' in values else 'UNCERTAIN' if 'UNCERTAIN' in values else 'PASS'
    return 'PASS' if 'PASS' in values else 'UNCERTAIN' if 'UNCERTAIN' in values else 'FAIL'

def logic_refs(node, depth=0):
    if depth > 64:
        return set()
    if not isinstance(node, dict):
        return set()
    if 'ref' in node:
        return {node['ref']} if isinstance(node['ref'], str) else set()
    result = set()
    for child in node.get('children', []) if isinstance(node.get('children'), list) else []:
        result.update(logic_refs(child, depth + 1))
    return result
