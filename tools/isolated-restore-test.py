#!/usr/bin/env python3
"""Restore Hermes in a disposable Linux mount/PID/network namespace.

No host configuration changes, deletion, package installation, or external
model calls. Source runtime is bind-mounted read-only. Private test files
and logs are retained; only aggregate results are printed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


PROBE = r'''
import fcntl, hashlib, http.server, inspect, io, json, os, socket, sqlite3
import struct, subprocess, sys, threading, time, zipfile
from pathlib import Path

home = Path(os.environ['HOME'])
runtime = home / '.hermes/hermes-agent'
out = Path('/output')
py = sys.executable
report = {'success': False, 'isolated_import': False, 'databases_ok': False,
          'cli_startup_ok': False, 'offline_chat_ok': False,
          'live_provider_tested': False, 'external_network_available': False}

def save():
    (out / 'result.json').write_text(json.dumps(report, indent=2))

def command(name, args, timeout=180):
    with open(out / (name + '.log'), 'wb') as log:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, timeout=timeout,
                                cwd=runtime)
    if result.returncode:
        raise RuntimeError(name + ' failed; see private log ' + name + '.log')

def db_counts(path):
    conn = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('SQLite integrity check failed')
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: conn.execute('SELECT count(*) FROM "' + n.replace('"', '""') + '"').fetchone()[0]
                for n in names}
    finally:
        conn.close()

try:
    if os.readlink('/proc/self/ns/net') == os.environ['TEST_HOST_NET_NS']:
        raise RuntimeError('Network namespace was not isolated')
    for path in (runtime, Path('/usr'), Path('/etc')):
        if not (os.statvfs(path).f_flag & os.ST_RDONLY):
            raise RuntimeError('Required read-only mount is writable')
    if (home / '.hermes/config.yaml').exists():
        raise RuntimeError('Test home was not empty')
    # Only loopback is enabled, entirely inside the private network namespace.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        req = struct.pack('16sH14s', b'lo', 0, b'')
        flags = struct.unpack('16sH14s', fcntl.ioctl(s, 0x8913, req))[1]
        fcntl.ioctl(s, 0x8914, struct.pack('16sH14s', b'lo', flags | 1, b''))
    with socket.socket() as s:
        s.settimeout(1)
        if s.connect_ex(('1.1.1.1', 443)) == 0:
            raise RuntimeError('Unexpected external network access')
    report['namespace_isolation_ok'] = True

    # Use the installed import implementation; suppress its service-revival
    # hook in this test process only. No source file or live config is edited.
    importer = out / 'import-runner.py'
    importer.write_text(
        "import runpy,sys\n"
        "import hermes_cli.backup as b\n"
        "if not hasattr(b,'_revive_gateway_after_import'): raise RuntimeError('Unknown import service hook; stop safely')\n"
        "b._revive_gateway_after_import=lambda *a,**k:None\n"
        "sys.argv=['hermes','import','/lab/input.zip','--force']\n"
        "runpy.run_module('hermes_cli.main',run_name='__main__')\n")
    command('import', [py, str(importer)], timeout=240)
    report['isolated_import'] = True

    with zipfile.ZipFile('/lab/input.zip') as z:
        if z.testzip() is not None:
            raise RuntimeError('ZIP CRC failed')
        members = z.namelist()
        roots = [n[:-len('config.yaml')] for n in members
                 if n == 'config.yaml' or n.endswith('/config.yaml')]
        if not roots:
            raise RuntimeError('Archive config.yaml not found')
        prefix = min(roots, key=len)
        for name in ('config.yaml', 'SOUL.md', 'USER.md'):
            src = z.read(prefix + name)
            dst = (home / '.hermes' / name).read_bytes()
            if hashlib.sha256(src).digest() != hashlib.sha256(dst).digest():
                raise RuntimeError('Restored ' + name + ' differs from archive')
        report['config_and_personality_match'] = True
        count = 0
        before_chat = None
        for name in ('state.db', 'memory_store.db'):
            src = out / ('source-' + name)
            src.write_bytes(z.read(prefix + name))
            source_counts = db_counts(src)
            restored_counts = db_counts(home / '.hermes' / name)
            if source_counts != restored_counts:
                raise RuntimeError('Restored database row counts differ: ' + name)
            if name == 'state.db':
                before_chat = restored_counts
            count += 1
        report['databases_checked'] = count
        report['databases_ok'] = True

    command('sessions', [py, '-m', 'hermes_cli.main', 'sessions', 'list'])
    command('chat-help', [py, '-m', 'hermes_cli.main', 'chat', '--help'])
    report['cli_startup_ok'] = True

    marker = 'SELINA_RESTORE_OFFLINE_OK'
    class Handler(http.server.BaseHTTPRequestHandler):
        calls = 0
        def log_message(self, *args):
            pass
        def do_GET(self):
            body = json.dumps({'object':'list','data':[{'id':'restore-test','object':'model','owned_by':'local-test'}]}).encode()
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            if length > 16 * 1024 * 1024:
                self.send_error(413); return
            request = json.loads(self.rfile.read(length))
            Handler.calls += 1
            if request.get('stream'):
                chunk = {'id':'chatcmpl-restore-test','object':'chat.completion.chunk',
                         'created':int(time.time()),'model':'restore-test',
                         'choices':[{'index':0,'delta':{'role':'assistant','content':marker},'finish_reason':None}]}
                final = dict(chunk, choices=[{'index':0,'delta':{},'finish_reason':'stop'}])
                body = ('data: '+json.dumps(chunk)+'\n\ndata: '+json.dumps(final)+'\n\ndata: [DONE]\n\n').encode()
                content_type = 'text/event-stream'
            else:
                body = json.dumps({'id':'chatcmpl-restore-test','object':'chat.completion',
                    'created':int(time.time()),'model':'restore-test',
                    'choices':[{'index':0,'message':{'role':'assistant','content':marker},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':8,'completion_tokens':8,'total_tokens':16}}).encode()
                content_type = 'application/json'
            self.send_response(200); self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    chat = out / 'chat-runner.py'
    chat.write_text(
        "import inspect\nfrom cli import main\n"
        "params=dict(query='Reply with the local restore test response.',oneshot=True,"
        "model='restore-test',provider='custom',api_key='local-test-no-real-credentials',"
        "base_url='http://127.0.0.1:" + str(server.server_port) + "/v1',max_turns=1,quiet=True)\n"
        "signature=inspect.signature(main)\n"
        "required={'query','model','provider','api_key','base_url'}\n"
        "if not required.issubset(signature.parameters): raise RuntimeError('Unsupported installed CLI interface')\n"
        "main(**{k:v for k,v in params.items() if k in signature.parameters})\n")
    command('offline-chat', [py, str(chat)], timeout=240)
    output = (out / 'offline-chat.log').read_text(errors='replace')
    if Handler.calls < 1 or marker not in output:
        raise RuntimeError('Offline chat round trip not confirmed')
    server.shutdown()
    after_chat = db_counts(home / '.hermes/state.db')
    if not any(after_chat.get(n, 0) > before_chat.get(n, 0) for n in ('sessions', 'messages')):
        raise RuntimeError('Offline chat was not persisted in restored state.db')
    report['offline_chat_ok'] = True
    report['test_session_saved'] = True
    report['success'] = True
except Exception as e:
    report['error'] = str(e)
finally:
    save()
sys.exit(0 if report['success'] else 1)
'''


def checked(args):
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def namespace_setup(config_path):
    cfg = json.loads(Path(config_path).read_text())
    lab, home, runtime, py = map(Path, (cfg['lab'], cfg['home'], cfg['runtime'], cfg['python']))
    root = lab / 'rootfs'
    root.mkdir()
    checked(['mount', '--make-rprivate', '/'])

    def mount(source, target, readonly=True):
        source, target = Path(source), Path(target)
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=False)
        checked(['mount', '--bind', str(source), str(target)])
        if readonly:
            checked(['mount', '-o', 'remount,bind,ro', str(target)])

    for name in ('usr', 'etc', 'bin', 'sbin', 'lib', 'lib64', 'opt'):
        src = Path('/') / name
        if src.is_symlink():
            (root / name).symlink_to(os.readlink(src))
        elif src.exists():
            mount(src, root / name)
    for name in ('tmp', 'run', 'proc', 'dev', 'lab', 'output'):
        (root / name).mkdir(exist_ok=True)
    mount(lab / 'test-home', root / home.relative_to('/'), readonly=False)
    mount(runtime, root / runtime.relative_to('/'))
    resolved_python = py.resolve()
    if str(resolved_python).startswith(str(home) + '/') and not resolved_python.is_relative_to(runtime):
        base = resolved_python.parent.parent
        mount(base, root / base.relative_to('/'))
    mount(cfg['archive'], root / 'lab/input.zip')
    mount(lab / 'probe.py', root / 'lab/probe.py')
    mount(lab / 'output', root / 'output', readonly=False)
    for name in ('null', 'zero', 'random', 'urandom'):
        mount('/dev/' + name, root / 'dev' / name, readonly=False)
    (root / 'dev/fd').symlink_to('/proc/self/fd')
    checked(['mount', '-t', 'proc', 'proc', str(root / 'proc')])
    os.chroot(root)
    os.chdir(runtime)
    os.environ.clear()
    os.environ.update({
        'HOME': str(home), 'HERMES_HOME': str(home / '.hermes'),
        'USER': cfg['username'], 'LOGNAME': cfg['username'],
        'PATH': '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
        'PYTHONPATH': str(runtime), 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONUNBUFFERED': '1', 'LANG': 'C.UTF-8', 'TERM': 'dumb',
        'XDG_CONFIG_HOME': str(home / '.config'),
        'XDG_CACHE_HOME': str(home / '.cache'),
        'XDG_DATA_HOME': str(home / '.local/share'),
        'XDG_RUNTIME_DIR': '/run', 'NO_PROXY': '*', 'no_proxy': '*',
        'SYSTEMD_OFFLINE': '1', 'TEST_HOST_NET_NS': cfg['host_net_ns'],
    })
    os.execv(str(py), [str(py), '/lab/probe.py'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--namespace-config', type=Path)
    args = parser.parse_args()
    if args.namespace_config:
        namespace_setup(args.namespace_config)
        return
    if sys.platform != 'linux' or not args.archive:
        parser.error('Run on Linux with --archive pointing to a cloud-restored ZIP')
    archive = args.archive.expanduser().resolve(strict=True)
    home = Path.home().resolve()
    runtime = home / '.hermes/hermes-agent'
    py = next((runtime / p for p in ('.hermes/bin/python', '.venv/bin/python')
               if (runtime / p).is_file()), None)
    if not py or not (runtime / 'hermes_cli/main.py').is_file():
        raise SystemExit('Installed Hermes runtime not found; nothing changed.')
    for name in ('unshare', 'mount'):
        if not shutil.which(name):
            raise SystemExit('Required command missing: ' + name)
    os.umask(0o077)
    lab = Path(tempfile.mkdtemp(prefix='selina-isolated-restore-', dir=home))
    (lab / 'test-home').mkdir(mode=0o700)
    (lab / 'output').mkdir(mode=0o700)
    (lab / 'probe.py').write_text(PROBE)
    cfg = {'lab': str(lab), 'home': str(home), 'runtime': str(runtime),
           'python': str(py), 'archive': str(archive),
           'username': home.name, 'host_net_ns': os.readlink('/proc/self/ns/net'),
           'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    config_path = lab / 'namespace.json'
    config_path.write_text(json.dumps(cfg))
    print('New private test directory:', lab, flush=True)
    cmd = ['unshare', '--user', '--map-root-user', '--mount', '--net',
           '--pid', '--fork', '--kill-child=SIGKILL', '--ipc', '--uts',
           sys.executable, str(Path(__file__).resolve()),
           '--namespace-config', str(config_path)]
    with open(lab / 'namespace.log', 'wb') as log:
        try:
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=900)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 124
    report_path = lab / 'output/result.json'
    if report_path.is_file():
        print(report_path.read_text())
    else:
        print('Isolation setup failed before the probe. Private log:', lab / 'namespace.log')
    print('No files deleted. Test home and logs retained:', lab)
    sys.exit(code)


if __name__ == '__main__':
    main()
