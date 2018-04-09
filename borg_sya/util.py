from io import BytesIO
import logging
import os
import socket
import subprocess
import sys
from subprocess import Popen
from threading import Thread
from yaml import YAMLObject, add_path_resolver
from yaml.loader import SafeLoader
from yaml.nodes import ScalarNode, MappingNode, SequenceNode


def which(command):
    for d in os.environ['PATH'].split(':'):
        try:
            for binary in os.listdir(d):
                if binary == command:
                    return os.path.join(d, command)
        except OSError:
            pass
    raise RuntimeError(f"Command not found: {command}.")


def isexec(path):
    if os.path.isfile(path):
        return os.access(path, os.X_OK)


class LockInUse(Exception):
    pass


class ProcessLock():
    """This reentrant lock class comes from this very elegant way of having a
    pid lock in order to prevent multiple instances from running on the same
    host.
    http://stackoverflow.com/a/7758075
    """

    def __init__(self, process_name):
        self._recursion_level = 0
        self._pname = process_name

    def __enter__(self):
        self.acquire()

    def __exit__(self, type, value, traceback):
        self.release()

    def acquire(self):
        if not self._recursion_level:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                # The bind address is the one of an abstract UNIX socket (begins
                # with a null byte) followed by an address which exists in the
                # abstract socket namespace (Linux only). See unix(7).
                self._socket.bind('\0' + self._pname)
            except socket.error:
                raise LockInUse
        self._recursion_level += 1

    def release(self):
        self._recursion_level -= 1
        if not self._recursion_level:
            self._socket.close()


class LazyReentrantContextmanager():
    def __init__(self):
        self.nesting_level = 0
        self.lazy = False
        self.entered = False

    def __call__(self, *, lazy=False):
        self.lazy = lazy
        return(self)

    def _enter(self):
        raise NotImplementedError()

    def _exit(self, type, value, traceback):
        raise NotImplementedError()

    def __enter__(self):
        if self.lazy:
            # Only actually enter at the next invocation. This still increments
            # the nesting_level so that cleanup will nevertheless occur at this
            # outer level.
            self.lazy = False
        elif not self.entered:
            self._enter()
            self.entered = True
        self.nesting_level += 1

    def __exit__(self, type, value, traceback):
        self.nesting_level -= 1
        if self.entered and self.nesting_level == 0:
            self._exit(type, value, traceback)
            self.entered = False


class Script(YAMLObject):
    """A YAML object with a tag to be set by subclasses that reads a scalar
    node and returns a callable that executes the node's text.
    """

    yaml_loader = SafeLoader

    def __init__(self, script):
        self.script = script

    @staticmethod
    def run_popen(cmdline, env, dryrun, **popen_args):
        if dryrun:
            logging.info(f"$ {cmdline if isinstance(cmdline, str) else ' '.join(cmdline)}")
            return
        # https://stackoverflow.com/questions/4984428/python-subprocess-get-childrens-output-to-file-and-terminal
        # https://stackoverflow.com/questions/17190221/subprocess-popen-cloning-stdout-and-stderr-both-to-terminal-and-variables
        def tee(infile, *files):
            def fanout(infile, *files):
                while True:
                    d = infile.readline(128)
                    if len(d):
                        for f, flush in files:
                            f.write(d)
                            if flush:
                                f.flush()
                    else:
                        break
                infile.close()
            t = Thread(target=fanout, args=(infile, *files))
            t.daemon = True
            t.start()
            return(t)

        p = Popen(cmdline, env=env,
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  **popen_args)
        out = BytesIO()
        err = BytesIO()
        t_out = tee(p.stdout, (sys.stdout.buffer, True), (out, False))
        t_err = tee(p.stderr, (sys.stderr.buffer, True), (err, False))
        t_out.join()
        t_err.join()
        p.wait()
        # TODO: Are there encoding issues here? (or above?)
        out = out.getvalue().decode('utf8')
        err = err.getvalue().decode('utf8')

        if p.returncode:
            raise RuntimeError(f"{cmdline} returned {p.returncode}:\n{err}")
        return(out + err)

    @classmethod
    def run(cls, script, args=None, env=None, dryrun=False, confdir=None):
        raise NotImplementedError()

    def __call__(self, **kwargs):
        self.run(self.script, **kwargs)

    @classmethod
    def from_yaml(cls, loader, node):
        """Load a scalar (i.e. a string if the configuration file is valid)
        """
        script = loader.construct_scalar(node)
        return(cls(script))

    @classmethod
    def to_yaml(cls, dumper, data):
        raise NotImplementedError()

    def __str__(self):
        return(f"{self.__class__.__name__}(\n'{self.script}')")


class ExternalScript(Script):
    """
    """
    yaml_tag = '!external_script'

    @classmethod
    def run(cls, script, args=None, env=None, dryrun=False, capture_out=True,
            confdir=None):
        if script:
            if not os.path.isabs(script):
                script = os.path.join(confdir, script)
            if not os.path.isfile(script):
                raise RuntimeError()

            if isexec(script):
                cmdline = [script]
                if args is not None:
                    cmdline.extend(args)
                return(cls.run_popen(cmdline, env, dryrun))
            else:
                raise RuntimeError(f"{path} exists, but cannot be "
                                   f"executed by the current user.")


class ShellScript(Script):
    """
    """
    yaml_tag = '!sh'

    @classmethod
    def run(cls, script, args=None, env=None, dryrun=False, confdir=None):
        if args:
            # logging.debug("ShellScript doesn't support `args`.")
            pass
        cls.run_popen(script, env=env, dryrun=dryrun, shell=True)


class PythonScript(Script):
    """
    """
    yaml_tag = '!python'

    @classmethod
    def run(cls, script, args=None, env=None, dryrun=False, confdir=None):
        if script:
            if args or env:
                raise NotImplementedError()

            if dryrun:
                logging.info(
                        f">>> {'... '.join(script.splitlines(keepends=True))}")
            else:
                # Propagate exceptions
                return(exec(script))


def register_yaml_resolvers():
    seq = [(SequenceNode, None)]
    for a, b in [('tasks', 'pre'),
                 ('tasks', 'post'),
                 ('repositories', 'mount'),
                 ('repositories', 'umount')]:
        path = [(MappingNode, a),
                (MappingNode, None),  # name
                (MappingNode, b),
                ]
        add_path_resolver('!external_script', path, ScalarNode,
                          Loader=SafeLoader)
        add_path_resolver('!external_script', path + seq, ScalarNode,
                          Loader=SafeLoader)
register_yaml_resolvers()
