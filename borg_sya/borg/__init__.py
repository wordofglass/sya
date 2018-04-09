from functools import wraps
import json
import logging
from queue import Queue
import signal
from subprocess import Popen, PIPE
import sys
from threading import Condition, Thread

from .defs import (BorgError, )

# TODO:
# - logging (component-wise, hierarchical)
# - clear separation between borg adapter, config parsing, logging, UI
# - human-readable output (click!). Maybe re-use borgs own message formatting? Or
#   directly display the JSON?


try:
    BINARY = which('borg')
except RuntimeError as e:
    sys.exit(str(e))
_log = logging.getLogger('borg')


class Repository():
    def __init__(self, name, path, borg,
                 compression=None, remote_path=None,
                 ):
        self.name = name
        self.path = path
        self.compression = compression
        self.remote_path = remote_path

    def borg_args(self, create=False):
        args = []
        if self.remote_path:
            args.extend(['--remote-path', self.remote_path])

        if create and self.compression:
            args.extend(['--compression', self.compression])

        return(args)

    @property
    def borg_env(self):
        env = {}
        if self.passphrase:
            env['BORG_PASSPHRASE'] = self.passphrase

        return(env)

    def __str__(self):
        """Used to construct the commandline arguments for borg, do not change!
        """
        return(self.path)


def _while_running(while_running=True):
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            if (    (while_running and self._running) or
                    (not while_running and not self._running)
                    ):
                return func(*args, **kwargs)
            else:
                raise RuntimeError()
        return wrapper
    return decorator


class Borg():

    def __init__(self, dryrun, verbose):
        self.dryrun = dryrun
        self.verbose = verbose
        self._running = False

    def _readerthread(self, fh, name, buf, condition):
        for line in fh:
            with condition:
                buf.put((name, line))
                condition.notify()
        fh.close()
        # with condition:
        #     buf.put('finished', None)

    def _communicate_linewise(self, p, stdout=True, stderr=True):
        buf = Queue()
        # nthreads = 0
        threads = []
        new_msg = Condition()
        if stdout:
            stdout_thread = Thread(target=self._readerthread,
                                   args=(p.stdout, 'stdout', buf, new_msg)
                                   )
            stdout_thread.daemon = True
            stdout_thread.start()
            # nthread += 1
            threads.append(stdout_thread)
        if stderr:
            stderr_thread = Thread(target=self._readerthread,
                                   args=(p.stderr, 'stderr', buf, new_msg)
                                   )
            stderr_thread.daemon = True
            stderr_thread.start()
            # nthread += 1
            threads.append(stderr_thread)

        # while nthreads:
        while all(t.is_alive() for t in threads):
            with new_msg:
                new_msg.wait_for(lambda: not buf.empty())
                source, msg = buf.get()
                if source == 'stdout':
                    yield (msg, None)
                elif source == 'stderr':
                    yield (None, msg)
                # elif source == 'finished':
                #     nthreads -= 1

    @_while_running(False)
    def _run(self, command, options, env=None, progress=True, output=None):
        commandline = [BINARY, '--log-json', '--json']
        if progress:
            commandline.append('--progress')
        if self.verbose:
            options.insert(0, '--verbose')

        commandline.append(command)
        commandline.extend(options)

        self._p = p = Popen(commandline, env=env,
                            stdout=PIPE, stderr=PIPE,
                            )

        self._running = True

        try:
            for stdout, stderr in self._communicate_linewise(p):
                if output and stdout is not None:
                    output.append(stdout)
                else:  # stderr is not None
                    yield self.parse_json(stderr)
        except Exception:
            # ?
            raise

        self._running = False

        return(output)

    def parse_json(self, msg):
        msg = json.loads(msg)

        if msg.type == 'log_message':
            if hasattr(msg, 'msgid') and msg.msgid:
                if msg.msgid in self._ERROR_MESSAGE_IDS:
                    e = BorgError(**msg)
                    _log(e)
                    raise e

        return msg

    @_while_running()
    def _signal(self, sig):
        if not self._running:
            raise RuntimeError()
        self._p.send_signal(sig)

    def _interrupt(self):
        self._signal(signal.SIGINT)

    def _terminate(self):
        self._signal(signal.SIGTERM)

    @_while_running()
    def _reply(self, answer):
        """ Answer a prompt.
        """
        raise NotImplementedError()

    def _yes(self):
        self._reply('YES')

    def _no(self):
        self._reply('NO')

    def init(self):
        raise NotImplementedError()

    def create(self, repo, includes, excludes=[],
               prefix='{hostname}', progress_cb=None,
               stats=False):
        if not includes:
            raise ValueError('No paths given to include in the archive!')

        options = repo.borg_args(create=True)
        if stats:
            # actually, this is already implied by --json
            options.append('--stats')
        for e in excludes:
            options.extend(['--exclude', e])
        options.append(f'{repo}::{prefix}')
        options.extend(includes)

        with repo:
            for msg in self._run('create', options, bool(progress_cb)):
                if msg.type == 'log_message':
                    if hasattr(msg, 'msgid') and msg.msgid:
                        if msg.msgid in self._PROMPT_MESSAGE_IDS:
                            raise RuntimeError()
                        else:
                            # Debug messages, ...
                            pass
                elif msg.type in ['progress_message', 'pogress_percent']:
                    raise NotImplementedError()
                    progress_cb(msg)

    def mount(self, repo, archive=None, mountpoint='/mnt', foreground=False):
        raise NotImplementedError()
        options = repo.borg_args()
        if foreground:
            options.append('--foreground')

        if archive:
            target = f'{repo}::{archive}'
        else:
            target = str(repo)

        with repo:
            for msg in self._run('mount', options):
                if msg.type == 'log_message':
                    if hasattr(msg, 'msgid') and msg.msgid:
                        if msg.msgid in self._PROMPT_MESSAGE_IDS:
                            raise RuntimeError()
                        else:
                            # Debug messages, ...
                            pass

    def umount(self):
        raise NotImplementedError()

    def extract(self):
        raise NotImplementedError()

    def list(self, repo,
             prefix=None, glob=None, first=0, last=0,
             # TODO: support exclude patterns.
             sort_by='', additional_keys=[], pandas=True):
        # NOTE: This can list either repo contents (archives) or archive
        # contents (files). Respect that, maybe even split in separate methods
        # (since e.g. repos should have the 'short' option to only return the
        # prefix, while only archives should have the pandas option(?)).
        options = repo.borg_args()

        if prefix and glob:
            raise ValueError("Cannot combine archive matching by prefix and "
                             "glob pattern!")
        if prefix:
            options.extend(['--prefix', prefix])
        if glob:
            options.extend(['--glob-archives', glob])

        if short:
            # default format: 'prefix     Mon, 2017-05-22 02:52:37'
            # --short format: 'prefix'
            pass

        if sort_by:
            if sort_by in 'timestamp name id'.split():
                options.extend(['--sort-by', sort_by])
            elif all(s in 'timestamp name id'.split() for s in sort_by):
                options.extend(['--sort-by', ','.join(sort_by)])
            else:
                raise ValueError("Invalid sorting criterion {sort_by} for "
                                 "file listing!")

        if first:
            options.extend(['--first', str(first)])
        if last:
            options.extend(['--last', str(last)])

        if additional_keys:
            # TODO: validate?
            options.extend(['--format',
                            ' '.join(f'{{{k}}}' for k in additional_keys)
                            ])

        output = []
        with repo:
            for msg in self._run('list', options, output):
                if msg.type == 'log_message':
                    if hasattr(msg, 'msgid') and msg.msgid:
                        if msg.msgid in self._PROMPT_MESSAGE_IDS:
                            raise RuntimeError()
                        else:
                            # Debug messages, ...
                            pass

        output = (json.loads(line) for line in output)
        if pandas:
            import pandas as pd
            return pd.DataFrame.from_records(
                    output,
                    # TODO: set dtype for all fields that could occur (defaults
                    # or additional_keys)
                    # dtype=...,
                    )
        else:
            return list(output)

    def info(self):
        raise NotImplementedError()

    def delete(self):
        raise NotImplementedError()

    def prune(self, repo, keep, prefix=None, verbose=True):
        if not keep:
            raise ValueError('No archives to keep given for pruning!')
        options = repo.borg_args()

        if verbose:
            options.extend(['--list', '--stats'])
        for interval, number in keep.items():
            options.extend([f'--keep-{interval}', str(number)])
        if prefix:
            options.extend(['--prefix', prefix])
        options.append(f"{self.repo}")

        with repo:
            for msg in self._run('prune', options):
                if msg.type == 'log_message':
                    if hasattr(msg, 'msgid') and msg.msgid:
                        if msg.msgid in self._PROMPT_MESSAGE_IDS:
                            raise RuntimeError()
                        else:
                            # Debug messages, ...
                            pass

    def recreate(self):
        raise NotImplementedError()
