import os
import pytest
import random
import string
import subprocess
import tempfile
import yaml

from borg_sya import Task, Repository

def random_strings(number, length=6):
    """Return a number of unique random strings to be used as names for
    tasks and repositories.
    """
    res = set()
    while len(res) < number:
        res.add(''.join(
            random.choices(
                string.ascii_uppercase + string.digits,
                k=length))
            )
    return list(res)


@pytest.fixture
def make_config():
    def _make_config(
            ntasks=1, create_repo=True,
            verbose=False,
            ):
        cfg = {'sya': dict(),
               'repositories': dict(),
               'tasks': dict(),
               }
        if verbose:
            cfg['sya']['verbose'] = True

        with tempfile.TemporaryDirectory() as confdir:
            rname = random_strings(1)[0]
            repo = Repository(
                    name=rname,
                    )
            cfg['repositories'][rname] = repo.to_yaml()
            if create_repo:
                os.mkdir(os.path.join(confdir, 'backup', rname)

            tnames = random_strings(ntasks)
            for name in tnames:
                task = Task(
                    name=name,
                    cx=None,
                    repo=repo,
                    enabled=True,
                    prefix=name,
                    keep={
                        hourly: 24,
                        },
                    includes=[],  # TODO: Include some data for backup tests in
                    # the tests module and copy that to tempdir, then list the
                    # directories here.
                    include_file=None,
                    exclude_file=None,
                    pre=None,
                    pre_desc='',
                    post=None,
                    post_desc='',
                )
                cfg['tasks'][name] = task.to_yaml()

            fh, fn = tempfile.mkstemp(b'.yaml')
            os.close(fh)

            with open(fn, 'wb') as fh:
                fh.write(yaml.dump(cfg, encoding='utf-8'))
            
            yield confdir, cfg

    return _make_config