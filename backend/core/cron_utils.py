# cron_utils.py 标准 crontab 兼容层
# 差异点：标准 crontab 周编号 0/7=周日、1=周一 ... 6=周六；
#         APScheduler 周编号 0=周一 ... 6=周日，且不接受 7。
# 本模块统一把用户按标准习惯写的规则翻译成 APScheduler 能接受的写法，
# 同时兼容青龙式 6 字段写法：秒 分 时 天 月 周（APScheduler 的 from_crontab 只认 5 字段）。
from apscheduler.triggers.cron import CronTrigger

_DOW_NAMES = {'sun': 0, 'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4, 'fri': 5, 'sat': 6}

def _dow_num(s):
    s = s.strip().lower()
    if s in _DOW_NAMES: return _DOW_NAMES[s]
    n = int(s)
    if not 0 <= n <= 7: raise ValueError(f'周字段数值超出范围: {n} (标准 crontab 为 0-7)')
    return n

def _dow_token_days(token):
    # 把一个周字段 token 展开成标准编号集合(0-6, 0=周日；7 归一为周日)
    token = token.strip()
    step = 1
    if '/' in token:
        token, step_s = token.split('/', 1)
        step = int(step_s)
        if step < 1: raise ValueError(f'周字段步长非法: /{step_s}')
    if token in ('*', '?'):
        start, end = 0, 7
    elif '-' in token:
        a, b = token.split('-', 1)
        start, end = _dow_num(a), _dow_num(b)
        if start > end: raise ValueError(f'周字段区间不支持回绕: {a}-{b}')
    else:
        start = end = _dow_num(token)
    return {d % 7 for d in range(start, end + 1, step)}

def _compact_days(days):
    # APS 编号集合压缩成紧凑写法：{0,1,2,3,4} -> '0-4'，{1,3,5,6} -> '1,3,5,6'
    nums = sorted(days)
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1: j += 1
        parts.append(str(nums[i]) if i == j else f'{nums[i]}-{nums[j]}')
        i = j + 1
    return ','.join(parts)

def convert_weekday(expr):
    """标准 crontab -> APScheduler 写法，仅翻译周字段；支持 5 字段与带秒 6 字段。"""
    fields = expr.strip().split()
    if len(fields) not in (5, 6):
        raise ValueError(f'cron 字段数错误: {len(fields)} 个 (应为 5 个，或带秒 6 个)')
    second = fields[0] if len(fields) == 6 else None
    if second is not None: fields = fields[1:]
    fields[2] = fields[2].replace('?', '*')  # 日字段兼容 Quartz 风格的 '?'
    days = set()
    for token in fields[4].replace('?', '*').split(','):
        days |= _dow_token_days(token)
    fields[4] = '*' if len(days) == 7 else _compact_days({(d + 6) % 7 for d in days})
    return ' '.join(([second] if second is not None else []) + fields)

def build_cron_trigger(expr, timezone=None):
    """把用户 cron 规则构造为 APScheduler CronTrigger，任务/订阅统一走这里。
    兼容标准周编号(0/7=周日)与 6 字段(秒 分 时 天 月 周)，非法规则抛 ValueError。"""
    fields = convert_weekday(expr).split()
    if len(fields) == 5:
        return CronTrigger.from_crontab(' '.join(fields), timezone=timezone)
    return CronTrigger(second=fields[0], minute=fields[1], hour=fields[2], day=fields[3],
                       month=fields[4], day_of_week=fields[5], timezone=timezone)
