"""Make the selected job interpreter and subsequent steps use llmcall explicitly."""
from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import subprocess
import sys


def source_path(environ=os.environ):
    explicit = environ.get('LLMCALL_SRC')
    if explicit:
        path = Path(explicit).expanduser()
        if not any((path / marker).is_file() for marker in ('pyproject.toml', 'setup.py')):
            raise ValueError('LLMCALL_SRC must name an installable source directory')
        return path
    candidate = Path('/workspace/tools/llmcall')
    if any((candidate / marker).is_file() for marker in ('pyproject.toml', 'setup.py')):
        return candidate
    return None


def prepare_environment(environ=os.environ):
    paths = [str(p) for p in (Path.home() / '.local/bin', Path('/workspace/tools/bin'))
             if p.is_dir()]
    environ['PATH'] = os.pathsep.join(paths + [environ.get('PATH', '')])
    environ['ARXIV_ASSISTANT_LLM_BACKEND'] = 'llmcall'
    if environ.get('GITHUB_PATH'):
        with open(environ['GITHUB_PATH'], 'a', encoding='utf-8') as stream:
            for path in paths:
                stream.write(path + '\n')
    if environ.get('GITHUB_ENV'):
        with open(environ['GITHUB_ENV'], 'a', encoding='utf-8') as stream:
            stream.write('ARXIV_ASSISTANT_LLM_BACKEND=llmcall\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    prepare_environment()
    source = source_path()
    if source:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', str(source)], check=True)
    importlib.invalidate_caches()
    try:
        import llmcall
    except ImportError as error:
        raise SystemExit('llmcall unavailable: install it or configure LLMCALL_SRC') from error
    print('llmcall imported by the job interpreter; backend=llmcall')
    if args.probe:
        result = llmcall.call('Reply with exactly: OK', timeout=180, effort='max')
        if not result or result.text.strip() != 'OK':
            raise SystemExit('llmcall capability probe failed; no fallback backend was selected')
        print('llmcall live probe: OK, provider=' + str(result.provider))


if __name__ == '__main__':
    main()
