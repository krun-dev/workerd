"""Keep Cloudflare's release version and append a per-upstream krun revision."""
import re
import subprocess

NUMBER = r'(?:0|[1-9][0-9]*)'
UPSTREAM = rf'{NUMBER}\.[0-9]{{8}}\.{NUMBER}'
VERSION = re.compile(rf'(?P<upstream>{UPSTREAM})-krun\.(?P<revision>{NUMBER})')


def parse(version):
    match = VERSION.fullmatch(version)
    if match is None:
        raise ValueError('VERSION must have the form 1.20260916.1-krun.0')
    return match['upstream'], int(match['revision'])


def upstream_version(directory):
    tags = subprocess.check_output(
        ['git', 'tag', '--points-at', 'HEAD'], cwd=directory, text=True).splitlines()
    versions = [tag[1:] for tag in tags if re.fullmatch('v' + UPSTREAM, tag)]
    if len(versions) != 1:
        raise ValueError('Pin the submodule to exactly one official release tag '
                         '(v1.YYYYMMDD.P); fetch that upstream tag if missing.')
    return versions[0]


def validate(version, upstream):
    if parse(version)[0] != upstream:
        raise ValueError(f'VERSION must use the pinned upstream version {upstream}.')


def next_version(upstream, current, tags):
    if not re.fullmatch(UPSTREAM, upstream):
        raise ValueError('Invalid upstream release version.')
    revisions = []
    # Include the pending VERSION as well as existing tags, so repeated invocations
    # increment and returning to a previously used upstream cannot reuse a number.
    for candidate in [current, *(tag[1:] for tag in tags if tag.startswith('v'))]:
        match = VERSION.fullmatch(candidate)
        if match and match['upstream'] == upstream:
            revisions.append(int(match['revision']))
    return f'{upstream}-krun.{max(revisions, default=-1) + 1}'
