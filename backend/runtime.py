# runtime.py 全局运行时状态（进程内共享，不做持久化）
# database.py 为 EXE 打包时的 datas 兼容占位文件，请勿删除。

running_processes = {}    # task_id -> subprocess.Popen
debug_processes = {}      # stream_id -> subprocess.Popen

SUBPROCESS_KWARGS = {}    # 平台相关参数，由 app_factory 按操作系统注入

queued_tasks = {}         # task_id -> {'task_id', 'ignore_disabled', 'trigger_source', 'queued_at'}
task_queue = []           # 等待执行的 task_id 顺序队列
queue_lock = None
queue_worker_started = False

# [修复 P2-7] 智能修复去重标记：原先挂在 execute_task 函数属性上，重启丢失且残留；改为模块级字典
last_auto_fixes = {}      # task_id -> "pkg_type:package"

# [修复 P2-13] 任务超时监控：原先每个任务单独起线程 sleep，改为统一注册，由共享扫描线程检查
task_start_times = {}     # task_id -> {'process': Popen, 'deadline': 时间戳, 'log_path': str, 'max_hours': float}
