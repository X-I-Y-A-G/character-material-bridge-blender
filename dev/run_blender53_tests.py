"""Run a Blender script with isolated preferences, extensions and workspace."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def run(blender, script, output, extra=()):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cmb53-') as temporary:
        env = os.environ.copy()
        for key in ('PYTHONPATH', 'PYTHONHOME', 'RURI_RIPPERHOOK_BIN'):
            env.pop(key, None)
        for key, name in (('BLENDER_USER_CONFIG', 'config'), ('BLENDER_USER_SCRIPTS', 'scripts'),
                          ('BLENDER_USER_EXTENSIONS', 'extensions'), ('BLENDER_USER_DATAFILES', 'datafiles'),
                          ('RURI_RIPPER_WORKSPACE', 'workspace')):
            env[key] = str(Path(temporary) / name)
        env['PYTHONIOENCODING'] = 'utf-8'
        env['CMB_TEST_OUT'] = str(output)
        command = [str(blender), '--background', '--factory-startup', '--disable-autoexec',
                   '--threads', '2', '--python-exit-code', '1', '--python', str(Path(script).resolve())]
        if extra:
            command += ['--', *extra]
        log_path = output / (Path(script).stem + '.log')
        with log_path.open('wb') as log:
            result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        print(log_path.read_text(encoding='utf-8', errors='replace')[-12000:])
        return result.returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blender', required=True)
    parser.add_argument('--script', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('extra', nargs='*')
    options = parser.parse_args()
    raise SystemExit(run(options.blender, options.script, options.out, options.extra))
