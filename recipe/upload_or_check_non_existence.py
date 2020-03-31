#!/usr/bin/env python
from __future__ import print_function

import contextlib
import os
import glob
import shutil
import subprocess
import click
import tempfile
import time

from binstar_client.utils import get_server_api
import binstar_client.errors
from conda_build.conda_interface import subdir as conda_subdir
from conda_build.conda_interface import get_index
import conda_build.api
import conda_build.config


def split_pkg(pkg):
    if not pkg.endswith(".tar.bz2"):
        raise RuntimeError("Can only process packages that end in .tar.bz2")
    pkg = pkg[:-8]
    plat, pkg_name = pkg.split("/")
    name_ver, build = pkg_name.rsplit('-', 1)
    name, ver = name_ver.rsplit('-', 1)
    return plat, name, ver, build


@contextlib.contextmanager
def get_temp_token(token):
    dn = tempfile.mkdtemp()
    fn = os.path.join(dn, "binstar.token")
    with open(fn, "w") as fh:
        fh.write(token)
    yield fn
    shutil.rmtree(dn)


def built_distribution_already_exists(cli, name, version, fname, owner):
    """
    Checks to see whether the built recipe (aka distribution) already
    exists on the owner/user's binstar account.

    """
    folder, basename = os.path.split(fname)
    _, platform = os.path.split(folder)
    distro_name = '{}/{}'.format(platform, basename)

    try:
        dist_info = cli.distribution(owner, name, version, distro_name)
    except binstar_client.errors.NotFound:
        dist_info = {}

    exists = bool(dist_info)
    # Unfortunately, we cannot check the md5 quality of the built distribution, as
    # this will depend on fstat information such as modification date (because
    # distributions are tar files). Therefore we can only assume that the distribution
    # just built, and the one on anaconda.org are the same.
#    if exists:
#        md5_on_binstar = dist_info.get('md5')
#        with open(fname, 'rb') as fh:
#            md5_of_build = hashlib.md5(fh.read()).hexdigest()
#
#        if md5_on_binstar != md5_of_build:
#            raise ValueError('This build ({}), and the build already on binstar '
#                             '({}) are different.'.format(md5_of_build, md5_on_binstar))
    return exists


def upload(token_fn, path, owner, channels):
    subprocess.check_call(['anaconda', '--quiet', '-t', token_fn,
                           'upload', path,
                           '--user={}'.format(owner),
                           '--channel={}'.format(channels)],
                          env=os.environ)


def distribution_exists_on_channel(binstar_cli, meta, fname, owner, channel='main'):
    """
    Determine whether a distribution exists on a specific channel.

    Note from @pelson: As far as I can see, there is no easy way to do this on binstar.

    """
    channel_url = '/'.join([owner, 'label', channel])
    fname = os.path.basename(fname)

    distributions_on_channel = get_index([channel_url],
                                         prepend=False, use_cache=False)

    try:
        on_channel = (distributions_on_channel[fname]['subdir'] ==
                      conda_subdir)
    except KeyError:
        on_channel = False

    return on_channel


def upload_or_check(recipe_dir, owner, channel, variant):
    token = os.environ.get('BINSTAR_TOKEN')

    # Azure's tokens are filled when in PR and not empty as for the other cis
    # In pr they will have a value like '$(secret-name)'
    if token and token.startswith('$('):
        token = None

    cli = get_server_api(token=token)

    # The list of built distributions
    paths = ([os.path.join('noarch', p) for p in os.listdir(
        os.path.join(conda_build.config.croot, 'noarch'))]
             + [os.path.join(conda_build.config.subdir, p) for p in os.listdir(os.path.join(
                conda_build.config.croot, conda_build.config.subdir))])
    built_distributions = [(split_pkg(path)[1], split_pkg(path)[2], path)
                           # TODO: flip this over to .conda when that format
                           #  is in flight
                           for path in paths if path.endswith('.tar.bz2')]

    # These are the ones that already exist on the owner channel's
    existing_distributions = [path for name, version, path in built_distributions
                              if built_distribution_already_exists(cli, name, version, path, owner)]
    for d in existing_distributions:
        print('Distribution {} already exists for {}'.format(d, owner))

    # These are the ones that are new to the owner channel's
    new_distributions = [path for name, version, path in built_distributions
                         if not built_distribution_already_exists(cli, name, version, path, owner)]

    # This is the actual fix where we create the token file once and reuse it for all uploads
    if token:
        with get_temp_token(cli.token) as token_fn:
            for path in new_distributions:
                upload(token_fn, path, owner, channel)
                print('Uploaded {}'.format(path))
        return True
    else:
        for path in new_distributions:
            print("Distribution {} is new for {}, but no upload is taking place "
                "because the BINSTAR_TOKEN is missing.".format(path, owner))
        return False


def retry_upload_or_check(recipe_dir, owner, channel, variant):
    # perform a backoff in case we fail.  THis should limit the failures from
    # issues with the Anaconda api
    for i in range(1, 10):
        try:
            res = upload_or_check(recipe_dir, owner, channel, variant)
            return res
        except Exception as e:
            timeout = i ** 2
            print("Failed to upload due to {}.  Trying again in {} seconds".format(e, timeout))
            time.sleep(timeout)
    raise TimeoutError("Did not manage to upload package.  Failing.")


@click.command()
@click.argument('recipe_dir',
                type=click.Path(exists=True, file_okay=False, dir_okay=True),
                )  # help='the conda recipe directory'
@click.argument('owner')  # help='the binstar owner/user'
@click.option('--channel', default='main',
              help='the anaconda label channel')
@click.option('--variant', '-m', multiple=True,
              type=click.Path(exists=True, file_okay=True, dir_okay=False),
              help="path to conda_build_config.yaml defining your base matrix")
def main(recipe_dir, owner, channel, variant):
    """
    Upload or check consistency of a built version of a conda recipe with binstar.
    Note: The existence of the BINSTAR_TOKEN environment variable determines
    whether the upload should actually take place."""
    return retry_upload_or_check(recipe_dir, owner, channel, variant)


if __name__ == '__main__':
    main()
