from __future__ import with_statement
from itertools import chain
import datetime
import sys
import warnings
import time
import threading
import time as mod_time
from redis._compat import (b, basestring, bytes, imap, iteritems, iterkeys,
                           itervalues, izip, long, nativestr, unicode,
                           safe_unicode)
from redis.connection import (ConnectionPool, UnixDomainSocketConnection,
                              SSLConnection, Token)
from redis.lock import Lock, LuaLock
from redis.exceptions import (
    ConnectionError,
    DataError,
    ExecAbortError,
    NoScriptError,
    PubSubError,
    RedisError,
    ResponseError,
    TimeoutError,
    WatchError,
)

from redis.client import dict_merge, string_keys_to_dict

def parse_job_resp(response):
    if response is None:
        return None
    print(response)
    print(type(response[0]))
    return response


class DisqueAlpha(object):
    """
    Implementation of the Redis protocol.

    This abstract class provides a Python interface to all Redis commands
    and an implementation of the Redis protocol.

    Connection and Pipeline derive from this, implementing how
    the commands are sent and received to the Redis server
    """
    RESPONSE_CALLBACKS = dict_merge(
        string_keys_to_dict(
            'GETJOB', parse_job_resp
        ),
        string_keys_to_dict(
            'QLEN ACKJOB FASTACK', int
        ),
        string_keys_to_dict(
            'ADDJOB', bytes
        ),
    )

    @classmethod
    def from_url(cls, url, db=None, **kwargs):
        """
        Return a Disque client object configured from the given URL.

        For example::

            disque://[:password]@localhost:6379/0
            unix://[:password]@/path/to/socket.sock?db=0

        There are several ways to specify a database number. The parse function
        will return the first specified option:
            1. A ``db`` querystring option, e.g. disque://localhost?db=0
            2. If using the redis:// scheme, the path argument of the url, e.g.
               redis://localhost/0
            3. The ``db`` argument to this function.

        If none of these options are specified, db=0 is used.

        Any additional querystring arguments and keyword arguments will be
        passed along to the ConnectionPool class's initializer. In the case
        of conflicting arguments, querystring arguments always win.
        """
        connection_pool = ConnectionPool.from_url(url, db=db, **kwargs)
        return cls(connection_pool=connection_pool)

    def __init__(self, host='localhost', port=7711,
                 db=0, password=None, socket_timeout=None,
                 socket_connect_timeout=None,
                 socket_keepalive=None, socket_keepalive_options=None,
                 connection_pool=None, unix_socket_path=None,
                 encoding='utf-8', encoding_errors='strict',
                 charset=None, errors=None,
                 decode_responses=False, retry_on_timeout=False,
                 ssl=False, ssl_keyfile=None, ssl_certfile=None,
                 ssl_cert_reqs=None, ssl_ca_certs=None):
        if not connection_pool:
            if charset is not None:
                warnings.warn(DeprecationWarning(
                    '"charset" is deprecated. Use "encoding" instead'))
                encoding = charset
            if errors is not None:
                warnings.warn(DeprecationWarning(
                    '"errors" is deprecated. Use "encoding_errors" instead'))
                encoding_errors = errors

            kwargs = {
                'db': db,
                'password': password,
                'socket_timeout': socket_timeout,
                'encoding': encoding,
                'encoding_errors': encoding_errors,
                'decode_responses': decode_responses,
                'retry_on_timeout': retry_on_timeout
            }
            # based on input, setup appropriate connection args
            if unix_socket_path is not None:
                kwargs.update({
                    'path': unix_socket_path,
                    'connection_class': UnixDomainSocketConnection
                })
            else:
                # TCP specific options
                kwargs.update({
                    'host': host,
                    'port': port,
                    'socket_connect_timeout': socket_connect_timeout,
                    'socket_keepalive': socket_keepalive,
                    'socket_keepalive_options': socket_keepalive_options,
                })

                if ssl:
                    kwargs.update({
                        'connection_class': SSLConnection,
                        'ssl_keyfile': ssl_keyfile,
                        'ssl_certfile': ssl_certfile,
                        'ssl_cert_reqs': ssl_cert_reqs,
                        'ssl_ca_certs': ssl_ca_certs,
                    })
            connection_pool = ConnectionPool(**kwargs)
        self.connection_pool = connection_pool
        self._use_lua_lock = None

        self.response_callbacks = self.__class__.RESPONSE_CALLBACKS.copy()

    def __repr__(self):
        return "%s<%s>" % (type(self).__name__, repr(self.connection_pool))

    def set_response_callback(self, command, callback):
        "Set a custom Response Callback"
        self.response_callbacks[command] = callback


    def transaction(self, func, *watches, **kwargs):
        """
        Convenience method for executing the callable `func` as a transaction
        while watching all keys specified in `watches`. The 'func' callable
        should expect a single argument which is a Pipeline object.
        """
        shard_hint = kwargs.pop('shard_hint', None)
        value_from_callable = kwargs.pop('value_from_callable', False)
        watch_delay = kwargs.pop('watch_delay', None)
        with self.pipeline(True, shard_hint) as pipe:
            while 1:
                try:
                    if watches:
                        pipe.watch(*watches)
                    func_value = func(pipe)
                    exec_value = pipe.execute()
                    return func_value if value_from_callable else exec_value
                except WatchError:
                    if watch_delay is not None and watch_delay > 0:
                        time.sleep(watch_delay)
                    continue

    def lock(self, name, timeout=None, sleep=0.1, blocking_timeout=None,
             lock_class=None, thread_local=True):
        """
        Return a new Lock object using key ``name`` that mimics
        the behavior of threading.Lock.

        If specified, ``timeout`` indicates a maximum life for the lock.
        By default, it will remain locked until release() is called.

        ``sleep`` indicates the amount of time to sleep per loop iteration
        when the lock is in blocking mode and another client is currently
        holding the lock.

        ``blocking_timeout`` indicates the maximum amount of time in seconds to
        spend trying to acquire the lock. A value of ``None`` indicates
        continue trying forever. ``blocking_timeout`` can be specified as a
        float or integer, both representing the number of seconds to wait.

        ``lock_class`` forces the specified lock implementation.

        ``thread_local`` indicates whether the lock token is placed in
        thread-local storage. By default, the token is placed in thread local
        storage so that a thread only sees its token, not a token set by
        another thread. Consider the following timeline:

            time: 0, thread-1 acquires `my-lock`, with a timeout of 5 seconds.
                     thread-1 sets the token to "abc"
            time: 1, thread-2 blocks trying to acquire `my-lock` using the
                     Lock instance.
            time: 5, thread-1 has not yet completed. redis expires the lock
                     key.
            time: 5, thread-2 acquired `my-lock` now that it's available.
                     thread-2 sets the token to "xyz"
            time: 6, thread-1 finishes its work and calls release(). if the
                     token is *not* stored in thread local storage, then
                     thread-1 would see the token value as "xyz" and would be
                     able to successfully release the thread-2's lock.

        In some use cases it's necessary to disable thread local storage. For
        example, if you have code where one thread acquires a lock and passes
        that lock instance to a worker thread to release later. If thread
        local storage isn't disabled in this case, the worker thread won't see
        the token set by the thread that acquired the lock. Our assumption
        is that these cases aren't common and as such default to using
        thread local storage.        """
        if lock_class is None:
            if self._use_lua_lock is None:
                # the first time .lock() is called, determine if we can use
                # Lua by attempting to register the necessary scripts
                try:
                    LuaLock.register_scripts(self)
                    self._use_lua_lock = True
                except ResponseError:
                    self._use_lua_lock = False
            lock_class = self._use_lua_lock and LuaLock or Lock
        return lock_class(self, name, timeout=timeout, sleep=sleep,
                          blocking_timeout=blocking_timeout,
                          thread_local=thread_local)

    def pubsub(self, **kwargs):
        """
        Return a Publish/Subscribe object. With this object, you can
        subscribe to channels and listen for messages that get published to
        them.
        """
        return PubSub(self.connection_pool, **kwargs)

    # COMMAND EXECUTION AND PROTOCOL PARSING
    def execute_command(self, *args, **options):
        "Execute a command and return a parsed response"
        pool = self.connection_pool
        command_name = args[0]
        connection = pool.get_connection(command_name, **options)
        try:
            connection.send_command(*args)
            return self.parse_response(connection, command_name, **options)
        except (ConnectionError, TimeoutError) as e:
            connection.disconnect()
            if not connection.retry_on_timeout and isinstance(e, TimeoutError):
                raise
            connection.send_command(*args)
            return self.parse_response(connection, command_name, **options)
        finally:
            pool.release(connection)

    def qlen(self, queue):
        return self.execute_command('QLEN', queue)

    def parse_response(self, connection, command_name, **options):
        "Parses a response from the Redis server"
        response = connection.read_response()
        if command_name in self.response_callbacks:
            return self.response_callbacks[command_name](response, **options)
        return response

    # SERVER INFORMATION
    def bgrewriteaof(self):
        "Tell the Redis server to rewrite the AOF file from data in memory."
        return self.execute_command('BGREWRITEAOF')

    def client_kill(self, address):
        "Disconnects the client at ``address`` (ip:port)"
        return self.execute_command('CLIENT KILL', address)

    def client_list(self):
        "Returns a list of currently connected clients"
        return self.execute_command('CLIENT LIST')

    def client_getname(self):
        "Returns the current connection name"
        return self.execute_command('CLIENT GETNAME')

    def client_setname(self, name):
        "Sets the current connection name"
        return self.execute_command('CLIENT SETNAME', name)

    def config_get(self, pattern="*"):
        "Return a dictionary of configuration based on the ``pattern``"
        return self.execute_command('CONFIG GET', pattern)

    def config_set(self, name, value):
        "Set config item ``name`` with ``value``"
        return self.execute_command('CONFIG SET', name, value)

    def config_resetstat(self):
        "Reset runtime statistics"
        return self.execute_command('CONFIG RESETSTAT')

    def config_rewrite(self):
        "Rewrite config file with the minimal change to reflect running config"
        return self.execute_command('CONFIG REWRITE')

    def debug_object(self, key):
        "Returns version specific meta information about a given key"
        return self.execute_command('DEBUG OBJECT', key)

    def info(self, section=None):
        """
        Returns a dictionary containing information about the Redis server

        The ``section`` option can be used to select a specific section
        of information

        The section option is not supported by older versions of Redis Server,
        and will generate ResponseError
        """
        if section is None:
            return self.execute_command('INFO')
        else:
            return self.execute_command('INFO', section)

    def ping(self):
        "Ping the Redis server"
        return self.execute_command('PING')

    def shutdown(self):
        "Shutdown the server"
        try:
            self.execute_command('SHUTDOWN')
        except ConnectionError:
            # a ConnectionError here is expected
            return
        raise RedisError("SHUTDOWN seems to have failed.")

    def slowlog_get(self, num=None):
        """
        Get the entries from the slowlog. If ``num`` is specified, get the
        most recent ``num`` items.
        """
        args = ['SLOWLOG GET']
        if num is not None:
            args.append(num)
        return self.execute_command(*args)

    def slowlog_len(self):
        "Get the number of items in the slowlog"
        return self.execute_command('SLOWLOG LEN')

    def slowlog_reset(self):
        "Remove all items in the slowlog"
        return self.execute_command('SLOWLOG RESET')

    def time(self):
        """
        Returns the server time as a 2-item tuple of ints:
        (seconds since epoch, microseconds into this second).
        """
        return self.execute_command('TIME')


    # BASIC JOB COMMANDS

    def addjob(self, queue, body, timeout_ms=0, replicate=0, delay_secs=0,
               retry_secs=-1, ttl_secs=0, maxlen=0, async=False):
        args = ['ADDJOB', queue, body, timeout_ms]
        if replicate > 0:
            args += [Token('REPLICATE'), replicate]
        if delay_secs > 0:
            args += [Token('DELAY'), delay_secs]
        if retry_secs >= 0:
            args += [Token('RETRY'), retry_secs]
        if ttl_secs > 0:
            args += [Token('TTL'), ttl_secs]
        if maxlen > 0:
            args += [Token('MAXLEN'), maxlen]
        if async:
            args += [Token('ASYNC')]

        return self.execute_command(*args)

    def getjob(self, queue, timeout_ms=0, count=1):
        return self.execute_command(
            'GETJOB', Token('TIMEOUT'), timeout_ms, Token('COUNT'), count,
            Token('FROM'), queue)