running_processes = {}
debug_processes = {}

SUBPROCESS_KWARGS = {}

queued_tasks = {}
task_queue = []
queue_lock = None
queue_worker_started = False