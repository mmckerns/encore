# Copyright 2009 Brian Quinlan. All Rights Reserved.
# Licensed to PSF under a Contributor Agreement.
#
# Changes:
# May 2013
#   CLF: Ported to encore
#   CLF: Renamed ThreadPoolExecutor to EnhancedThreadPoolExecutor
#   CLF: Patched from upstream issue16284
#   CLF: Patched from upstream issue11777
#   CLF: Added 'initializer' and 'uninitializer' arguments to
#        EnhancedThreadPoolExecutor
#   CLF: Added 'name' argument to EnhancedThreadPoolExecutor

"""Implements EnhancedThreadPoolExecutor.

This builds off of concurrent.futures.thread and implements the following
changes:
    * Each worker can be initialized and unitialized with specified functions.
    * 'map' works without iterating (bugs.python.org/issue11777).
    * Workers do not unnecessarily retain references to work items
      (bugs.python.org/issue16284).

The implementation is largely copied to avoid reliance on undocumented, private
parts of the code. For example, '_thread_references' is needed to properly
manage threads in the ThreadPoolExecutor, but this is not guaranteed to exist
in future implementations of concurrent.futures.thread.

"""

from __future__ import with_statement
import atexit
import itertools
import threading
import weakref
import time
import Queue as queue

from concurrent.futures import _base


# Workers are created as daemon threads. This is done to allow the interpreter
# to exit when there are still idle threads in a ThreadPoolExecutor's thread
# pool (i.e. shutdown() was not called). However, allowing workers to die with
# the interpreter has two undesirable properties:
#   - The workers would still be running during interpretor shutdown,
#     meaning that they would fail in unpredictable ways.
#   - The workers could be killed while evaluating a work item, which could
#     be bad if the callable being evaluated has external side-effects e.g.
#     writing to a file.
#
# To work around this problem, an exit handler is installed which tells the
# workers to exit when their work queues are empty and then waits until the
# threads finish.

_thread_references = set()
_shutdown = False


def _python_exit():
    global _shutdown
    _shutdown = True
    for thread_reference in set(_thread_references):
        thread = thread_reference()
        if thread is not None:
            thread.join()


def _remove_dead_thread_references():
    """Remove inactive threads from _thread_references.

    Should be called periodically to prevent memory leaks in scenarios such as:
    >>> while True:
    ...    t = ThreadPoolExecutor(max_workers=5)
    ...    t.map(int, ['1', '2', '3', '4', '5'])
    """
    for thread_reference in set(_thread_references):
        if thread_reference() is None:
            _thread_references.discard(thread_reference)

atexit.register(_python_exit)


class _WorkItem(object):
    def __init__(self, future, fn, args, kwargs):
        self.future = future
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        if not self.future.set_running_or_notify_cancel():
            return

        try:
            result = self.fn(*self.args, **self.kwargs)
        except BaseException as e:
            self.future.set_exception(e)
        else:
            self.future.set_result(result)


def _worker(executor_reference, work_queue, initialize_reference=None,
        uninitialize_reference=None):

    if initialize_reference is not None:
        initialize = initialize_reference()
        if initialize is None:
            _base.LOGGER.critical('Initializer reference is empty',
                    exc_info=True)
        else:
            try:
                initialize()
            except BaseException:
                _base.LOGGER.critical('Initialize exception in worker',
                        exc_info=True)

    try:
        while True:
            try:
                work_item = work_queue.get(block=True, timeout=0.1)
            except queue.Empty:
                executor = executor_reference()
                # Exit if:
                #   - The interpreter is shutting down OR
                #   - The executor that owns the worker has been collected OR
                #   - The executor that owns the worker has been shutdown.
                if _shutdown or executor is None or executor._shutdown:
                    break
                del executor
            else:
                work_item.run()
                # Delete references to object. See issue16284
                del work_item
    except BaseException:
        _base.LOGGER.critical('Exception in worker', exc_info=True)
    finally:
        if uninitialize_reference is not None:
            uninitialize = uninitialize_reference()
            if uninitialize is None:
                _base.LOGGER.critical('Uninitializer reference is empty',
                        exc_info=True)
            else:
                try:
                    uninitialize()
                except BaseException:
                    _base.LOGGER.critical('Uninitialize exception in worker',
                            exc_info=True)


class EnhancedThreadPoolExecutor(_base.Executor):
    def __init__(self, max_workers, initializer=None, uninitializer=None,
                 name=None):
        """Initializes a new EnhancedThreadPoolExecutor instance.

        Args:
            max_workers: The maximum number of threads that can be used to
                execute the given calls.
            initializer: callable, taking no arguments, that is to be called on
                each worker thread when it is started. Exceptions in the
                initializer are logged, then ignored.
            uninitializer: callable, taking no arguments, that is to be called
                on each worker thread when shut down. Exceptions in the
                uninitializer are logged, then ignored.
            name: string giving the name for this Executor.  This name
                is used as a prefix for the names of the executor's
                worker threads.  If no name is given then the executor
                class name will be used.

        """
        _remove_dead_thread_references()

        self._max_workers = max_workers
        self._work_queue = queue.Queue()
        self._threads = set()
        self._shutdown = False
        self._shutdown_lock = threading.Lock()
        self._initializer = initializer
        self._uninitializer = uninitializer
        if name is None:
            name = type(self).__name__
        self.name = name
        self._thread_counter = itertools.count(start=1)

    def submit(self, fn, *args, **kwargs):
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError('cannot schedule new futures after shutdown')

            f = _base.Future()
            w = _WorkItem(f, fn, args, kwargs)

            self._work_queue.put(w)
            self._adjust_thread_count()
            return f
    submit.__doc__ = _base.Executor.submit.__doc__

    def _adjust_thread_count(self):
        if len(self._threads) < self._max_workers:
            initializer_reference = (None if self._initializer is None
                                     else weakref.ref(self._initializer))
            uninitializer_reference = (None if self._uninitializer is None
                                     else weakref.ref(self._uninitializer))
            thread_name = "{0}Worker-{1}".format(self.name,
                                                 next(self._thread_counter))
            t = threading.Thread(target=_worker, name=thread_name,
                                 args=(weakref.ref(self), self._work_queue,
                                       initializer_reference,
                                       uninitializer_reference))
            t.daemon = True
            t.start()
            self._threads.add(t)
            _thread_references.add(weakref.ref(t))

    def shutdown(self, wait=True):
        with self._shutdown_lock:
            self._shutdown = True
        if wait:
            for t in self._threads:
                t.join()
    shutdown.__doc__ = _base.Executor.shutdown.__doc__

    def map(self, fn, *iterables, **kwargs):
        """Returns a iterator equivalent to map(fn, iter).

        Args:
            fn: A callable that will take take as many arguments as there are
                passed iterables.
            timeout: The maximum number of seconds to wait. If None, then there
                is no limit on the wait time.

        Returns:
            An iterator equivalent to: map(func, *iterables) but the calls may
            be evaluated out-of-order.

        Raises:
            TimeoutError: If the entire result iterator could not be generated
                before the given timeout.
            Exception: If fn(*args) raises for any values.
        """
        timeout = kwargs.get('timeout')
        if timeout is not None:
            end_time = timeout + time.time()

        fs = [self.submit(fn, *args) for args in zip(*iterables)]

        # Yield must be hidden in closure so that the futures are submitted
        # before the first iterator value is required.
        def result_iterator():
            try:
                for future in fs:
                    if timeout is None:
                        yield future.result()
                    else:
                        yield future.result(end_time - time.time())
            finally:
                for future in fs:
                    future.cancel()
        return result_iterator()