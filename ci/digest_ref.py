#!/usr/bin/env python3
"""Resolve a pushed Docker image to a registry-specific immutable digest."""

import json
import re
import subprocess
import sys


def select_digest(tag, digest_refs):
    if not re.fullmatch(r'docker-hosted\.hamainsurance\.net/attendance-web:[A-Za-z0-9_.-]{1,100}', tag):
        raise ValueError('Invalid or unapproved image name')
    repository = tag.rsplit(':', 1)[0]
    valid = [ref for ref in digest_refs
             if isinstance(ref, str) and re.fullmatch(re.escape(repository) + r'@sha256:[a-f0-9]{64}', ref)]
    if len(valid) != 1:
        raise ValueError('Image digest is missing or ambiguous: abort deployment')
    return valid[0]


def main():
    if len(sys.argv) != 2:
        return 2
    result = subprocess.run(['docker', 'image', 'inspect', sys.argv[1],
                             '--format', '{{json .RepoDigests}}'],
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Unable to inspect pushed Docker image')
    print(select_digest(sys.argv[1], json.loads(result.stdout)))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, json.JSONDecodeError) as error:
        print('STOP:', str(error), file=sys.stderr)
        raise SystemExit(1)
