from pathlib import Path
import importlib.util
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bootstrap_llmcall', ROOT / 'scripts/bootstrap_llmcall.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def test_environment_is_exported_for_subsequent_steps(tmp_path):
    env = {'PATH': '', 'GITHUB_PATH': str(tmp_path / 'path'),
           'GITHUB_ENV': str(tmp_path / 'env')}
    bootstrap.prepare_environment(env)
    assert env['ARXIV_ASSISTANT_LLM_BACKEND'] == 'llmcall'
    assert 'ARXIV_ASSISTANT_LLM_BACKEND=llmcall' in (tmp_path / 'env').read_text()


def test_explicit_bad_source_is_not_silently_ignored(tmp_path):
    with pytest.raises(ValueError):
        bootstrap.source_path({'LLMCALL_SRC': str(tmp_path)})


def test_source_must_be_installable_not_a_site_packages_parent(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[build-system]')
    assert bootstrap.source_path({'LLMCALL_SRC': str(tmp_path)}) == tmp_path


def test_each_model_job_initializes_the_transport():
    count = 0
    for path in (ROOT / '.github/workflows').glob('*.y*ml'):
        document = yaml.safe_load(path.read_text(encoding='utf-8'))
        for job in document.get('jobs', {}).values():
            steps = job.get('steps', [])
            model_steps = [i for i,s in enumerate(steps)
                           if s.get('env', {}).get('ARXIV_ASSISTANT_LLM_BACKEND') == 'llmcall']
            for index in model_steps:
                count += 1
                earlier = steps[:index]
                assert any('bootstrap_llmcall.py' in s.get('run', '')
                           and not s.get('continue-on-error') for s in earlier), path.name
    assert count >= 8
