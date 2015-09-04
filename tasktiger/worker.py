from collections import OrderedDict
import errno
import json
import os
import random
import select
import signal
import time
import traceback

from redis_lock import Lock

from ._internal import *
from .retry import *
from .timeouts import UnixSignalDeathPenalty, JobTimeoutException

__all__ = ['Worker']

class Worker(object):
    def __init__(self, tiger, queues=None):
        self.log = tiger.log.bind(pid=os.getpid())
        self.connection = tiger.connection
        self.scripts = tiger.scripts
        self.config = tiger.config
        self._key = tiger._key
        self._redis_move_task = tiger._redis_move_task

        # TODO: Also support wildcards in filter
        if queues:
            self.queue_filter = queues.split(',')
        else:
            self.queue_filter = None

        self._stop_requested = False

    def _install_signal_handlers(self):
        def request_stop(signum, frame):
            self._stop_requested = True
            self.log.info('stop requested, waiting for task to finish')
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

    def _uninstall_signal_handlers(self):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def _filter_queues(self, queues):
        if self.queue_filter:
            return [q for q in queues if q in self.queue_filter]
        else:
            return queues

    def _worker_queue_scheduled_tasks(self):
        queues = set(self._filter_queues(self.connection.smembers(
                self._key(SCHEDULED))))
        now = time.time()
        for queue in queues:
            # Move due items from scheduled queue to active queue. If items
            # were moved, remove the queue from the scheduled set if it is
            # empty, and add it to the active set so the task gets picked up.

            result = self.scripts.zpoppush(
                self._key(SCHEDULED, queue),
                self._key(QUEUED, queue),
                self.config['SCHEDULED_TASK_BATCH_SIZE'],
                now,
                now,
                on_success=('update_sets', self._key(SCHEDULED), self._key(QUEUED), queue),
            )

            # XXX: ideally this would be in the same pipeline, but we only want
            # to announce if there was a result.
            if result:
                self.connection.publish(self._key('activity'), queue)

    def _update_queue_set(self, timeout=None):
        """
        This method checks the activity channel for any new queues that have
        activities and updates the queue_set. If there are no queues in the
        queue_set, this method blocks until there is activity or the timeout
        elapses. Otherwise, this method returns as soon as all messages from
        the activity channel were read.
        """

        # Pubsub messages generator
        gen = self._pubsub.listen()
        while True:
            # Since Redis' listen method blocks, we use select to inspect the
            # underlying socket to see if there is activity.
            fileno = self._pubsub.connection._sock.fileno()
            r, w, x = select.select([fileno], [], [],
                                    0 if self._queue_set else timeout)
            if fileno in r: # or not self._queue_set:
                message = gen.next()
                if message['type'] == 'message':
                    for queue in self._filter_queues([message['data']]):
                        self._queue_set.add(queue)
            else:
                break

    def _worker_queue_expired_tasks(self):
        active_queues = self.connection.smembers(self._key(ACTIVE))
        now = time.time()
        for queue in active_queues:
            result = self.scripts.zpoppush(
                self._key(ACTIVE, queue),
                self._key(QUEUED, queue),
                self.config['ACTIVE_TASK_EXPIRED_BATCH_SIZE'],
                now - self.config['ACTIVE_TASK_UPDATE_TIMEOUT'],
                now,
                on_success=('update_sets', self._key(ACTIVE), self._key(QUEUED), queue),
            )
            # XXX: Ideally this would be atomic with the operation above.
            if result:
                self.log.info('queueing expired tasks', task_ids=result)
                self.connection.publish(self._key('activity'), queue)

    def _execute_forked(self, tasks, log):
        """
        Executes the tasks in the forked process. Multiple tasks can be passed
        for batch processing. However, they must all use the same function and
        will share the execution entry.
        """
        success = False

        execution = {}

        assert len(tasks)
        task_func = tasks[0]['func']
        assert all([task_func == task['func'] for task in tasks[1:]])

        try:
            func = import_attribute(task_func)
        except (ValueError, ImportError, AttributeError):
            log.error('could not import', func=task_func)
        else:
            execution['time_started'] = time.time()

            try:
                if getattr(func, '_task_batch', False):
                    # Batch process if the task supports it.
                    params = [{
                        'args': task.get('args', []),
                        'kwargs': task.get('kwargs', {}),
                    } for task in tasks]
                    hard_timeout = max(task.get('hard_timeout', None) for task in tasks) or \
                                   getattr(func, '_task_hard_timeout', None) or \
                                   self.config['DEFAULT_HARD_TIMEOUT']

                    with UnixSignalDeathPenalty(hard_timeout):
                        func(params)
                else:
                    # Process sequentially.
                    for task in tasks:
                        hard_timeout = task.get('hard_timeout', None) or \
                                       getattr(func, '_task_hard_timeout', None) or \
                                       self.config['DEFAULT_HARD_TIMEOUT']
                        args = task.get('args', [])
                        kwargs = task.get('kwargs', {})

                        with UnixSignalDeathPenalty(hard_timeout):
                            func(*args, **kwargs)

            except Exception, exc:
                execution['traceback'] = traceback.format_exc()
                execution['exception_name'] = serialize_func_name(exc.__class__)
                execution['time_failed'] = time.time()
            else:
                success = True

        if not success:
            # Currently we only log failed task executions to Redis.
            execution['success'] = success
            serialized_execution = json.dumps(execution)
            for task in tasks:
                self.connection.rpush(
                    self._key('task', task['id'],'executions'),
                    serialized_execution)

        return success

    def _heartbeat(self, queue, task_ids):
        now = time.time()
        self.connection.zadd(self._key(ACTIVE, queue),
                             **{task_id: now for task_id in task_ids})

    def _execute(self, queue, tasks, log, locks, all_task_ids):
        """
        Executes the given tasks. Returns a boolean indicating whether
        the tasks were executed succesfully.
        """

        # The tasks must use the same function.
        assert len(tasks)
        task_func = tasks[0]['func']
        assert all([task_func == task['func'] for task in tasks[1:]])

        # Adapted from rq Worker.execute_job / Worker.main_work_horse
        child_pid = os.fork()
        if child_pid == 0:
            # Child process
            log = log.bind(child_pid=os.getpid())

            # We need to reinitialize Redis' connection pool, otherwise the parent
            # socket will be disconnected by the Redis library.
            # TODO: We might only need this if the task fails.
            pool = self.connection.connection_pool
            pool.__init__(pool.connection_class, pool.max_connections,
                          **pool.connection_kwargs)

            random.seed()
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            success = self._execute_forked(tasks, log)
            os._exit(int(not success))
        else:
            # Main process
            log = log.bind(child_pid=child_pid)
            log.debug('processing', func=task_func, params=[{
                    "args": task.get('args', []),
                    "kwargs": task.get('kwargs', {})
            } for task in tasks])

            while True:
                try:
                    with UnixSignalDeathPenalty(self.config['ACTIVE_TASK_UPDATE_TIMER']):
                        _, return_code = os.waitpid(child_pid, 0)
                        break
                except OSError as e:
                    if e.errno != errno.EINTR:
                        raise
                except JobTimeoutException:
                    self._heartbeat(queue, all_task_ids)
                    for lock in locks:
                        lock.renew(self.config['ACTIVE_TASK_UPDATE_TIMEOUT'])

            status = not return_code
            return status

    def _process_from_queue(self, queue):
        """
        Internal method to processes a task batch from the given queue. Returns
        the task IDs that were processed (even if there was an error so that
        client code can assume the queue is empty if nothing was returned).
        """
        now = time.time()

        log = self.log.bind(queue=queue)

        # Fetch one item unless this is a batch queue.
        batch_size = self.config['BATCH_QUEUES'].get(queue, 1)

        # Move an item to the active queue, if available.
        task_ids = self.scripts.zpoppush(
            self._key(QUEUED, queue),
            self._key(ACTIVE, queue),
            batch_size,
            None,
            now,
            on_success=('update_sets', self._key(QUEUED), self._key(ACTIVE), queue),
        )

        if task_ids:
            # Get all tasks
            serialized_tasks = self.connection.mget([
                self._key('task', task_id) for task_id in task_ids
            ])

            # Parse tasks
            tasks = []
            for task_id, serialized_task in zip(task_ids, serialized_tasks):
                if serialized_task:
                    task = json.loads(serialized_task)
                    if task['id'] == task_id:
                        tasks.append(task)
                    else:
                        log.error('task ID mismatch', task_id=task_id)
                        # Remove task
                        self._redis_move_task(queue, task_id, ACTIVE)
                else:
                    log.error('not found', task_id=task_id)
                    # Remove task
                    self._redis_move_task(queue, task_id, ACTIVE)

            all_task_ids = [task['id'] for task in tasks]

            # Group by task func
            tasks_by_func = OrderedDict()
            for task in tasks:
                if task['func'] in tasks_by_func:
                    tasks_by_func[task['func']].append(task)
                else:
                    tasks_by_func[task['func']] = [task]

            # Execute tasks for each task func
            for tasks in tasks_by_func.values():
                success, processed_tasks = self._process_task_group(queue, tasks, all_task_ids)
                for task in processed_tasks:
                    self._finish_task_processing(queue, task, success)

        return task_ids

    def _process_task_group(self, queue, tasks, all_task_ids):
        log = self.log.bind(queue=queue)

        locks = []
        ready_tasks = []
        for task in tasks:
            if task.get('lock', False):
                lock = Lock(self.connection, self._key('lock', gen_unique_id(
                    task['func'],
                    task.get('args', []),
                    task.get('kwargs', []),
                )), timeout=self.config['ACTIVE_TASK_UPDATE_TIMEOUT'])

                acquired = lock.acquire(blocking=False)
                if not acquired:
                    log.info('could not acquire lock', task_id=task['id'])

                    # Reschedule the task
                    when = time.time() + self.config['LOCK_RETRY']
                    self._redis_move_task(queue, task['id'], ACTIVE, SCHEDULED, when)
                    continue
            else:
                lock = None

            if lock:
                locks.append(lock)

            ready_tasks.append(task)

        if not ready_tasks:
            return True, []

        success = self._execute(queue, ready_tasks, log, locks, all_task_ids)

        for lock in locks:
            lock.release()

        return success, ready_tasks

    def _finish_task_processing(self, queue, task, success):
        log = self.log.bind(queue=queue, task_id=task['id'])

        if success:
            # Remove the task from active queue
            if task.get('unique', False):
                # For unique tasks we need to check if they're in use in
                # another queue since they share the task ID.
                remove_task = 'check'
            else:
                remove_task = 'always'
            self._redis_move_task(queue, task['id'], ACTIVE,
                                  remove_task=remove_task)
            log.debug('done')
        else:
            should_retry = False
            # Get execution info (for logging and retry purposes)
            execution = self.connection.lindex(
                self._key('task', task['id'], 'executions'), -1)
            if execution:
                execution = json.loads(execution)
            if 'retry_method' in task:
                if 'retry_on' in task:
                    if execution:
                        exception_name = execution.get('exception_name')
                        try:
                            exception_class = import_attribute(exception_name)
                        except (ValueError, ImportError, AttributeError):
                            log.error('could not import exception',
                                      exception_name=exception_name)
                        else:
                            for n in task['retry_on']:
                                if issubclass(exception_class, import_attribute(n)):
                                    should_retry = True
                                    break
                else:
                    should_retry = True

            queue_type = ERROR

            when = time.time()

            log_context = {}

            if should_retry:
                retry_func, retry_args = task['retry_method']
                retry_num = self.connection.llen(self._key('task', task['id'], 'executions'))
                log_context['retry_func'] = retry_func
                log_context['retry_num'] = retry_num

                try:
                    func = import_attribute(retry_func)
                except (ValueError, ImportError, AttributeError):
                    log.error('could not import retry function',
                              func=retry_func)
                else:
                    try:
                        retry_delay = func(retry_num, *retry_args)
                        log_context['retry_delay'] = retry_delay
                        when += retry_delay
                    except StopRetry:
                        pass
                    else:
                        queue_type = SCHEDULED

            if queue_type == ERROR:
                log_func = log.error
            else:
                log_func = log.warning

            log_func(func=task['func'],
                     time_failed=execution.get('time_failed'),
                     traceback=execution.get('traceback'),
                     exception_name=execution.get('exception_name'),
                     **log_context)

            # Move task to the scheduled queue for retry, or move to error
            # queue if we don't want to retry.
            self._redis_move_task(queue, task['id'], ACTIVE, queue_type, when)

    def _worker_run(self):
        """
        Performs one worker run:
        * Processes a set of messages from each queue and removes any empty
          queues from the working set.
        * Move any expired items from the active queue to the queued queue.
        * Move any scheduled items from the scheduled queue to the queued
          queue.
        """

        queues = list(self._queue_set)
        random.shuffle(queues)

        for queue in queues:
            if not self._process_from_queue(queue):
                self._queue_set.remove(queue)
            if self._stop_requested:
                break

        if not self._stop_requested:
            self._worker_queue_scheduled_tasks()
            self._worker_queue_expired_tasks()

    def run(self, once=False):
        """
        Main loop of the worker. Use once=True to execute any queued tasks and
        then exit.
        """

        self.log.info('ready', queues=self.queue_filter)

        # First scan all the available queues for new items until they're empty.
        # Then, listen to the activity channel.
        # XXX: This can get inefficient when having lots of queues.

        self._pubsub = self.connection.pubsub()
        self._pubsub.subscribe(self._key('activity'))

        self._queue_set = set(self._filter_queues(
                self.connection.smembers(self._key(QUEUED))))

        try:
            while True:
                if not self._queue_set:
                    self._update_queue_set(
                            timeout=self.config['SELECT_TIMEOUT'])

                self._install_signal_handlers()
                self._worker_run()
                self._uninstall_signal_handlers()
                if once and not self._queue_set:
                    break
                if self._stop_requested:
                    raise KeyboardInterrupt()
        except KeyboardInterrupt:
            pass
        self.log.info('done')
