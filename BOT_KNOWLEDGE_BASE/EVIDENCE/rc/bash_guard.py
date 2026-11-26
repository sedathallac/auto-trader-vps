#!/usr/bin/env python3
"""PreToolUse(Bash) kapisi. Hata durumunda KAPALI kalir (exit 2). Salt okunur analiz disindaki komutlari ve gizli dosya okumalarini engeller."""
import sys, json, re, shlex, glob, os, fnmatch

KB = "/root/BOT_KNOWLEDGE_BASE"
RC = KB + "/EVIDENCE/rc"
BINDIRS = ("/usr/bin", "/bin", "/usr/local/bin", "/usr/sbin", "/sbin")


def deny(msg):
    sys.stderr.write("ENGELLENDI (bash_guard): %s\n" % msg)
    sys.exit(2)


SECRET_RES = [r'(^|/)\.env($|\.)', r'(^|/)[^/]*\.env$', r'(^|/)environ$', r'(^|/)config[^/]*\.json$',
              r'(^|/)id_(rsa|ed25519|ecdsa|dsa|vps)[^/]*$', r'(^|/)\.ssh(/|$)', r'(^|/)\.claude(/|$)',
              r'(^|/)authorized_keys$', r'(^|/)\.netrc$', r'\.pem$', r'^/etc/shadow', r'^/proc/', r'(^|/)\.git-credentials$']
EXAMPLE = re.compile(r'(example|sample|template)')
READ_CMDS = {'cat', 'head', 'tail', 'nl', 'grep', 'egrep', 'fgrep', 'awk', 'sed', 'sort', 'uniq', 'cut', 'tr', 'diff', 'cmp',
             'sha256sum', 'md5sum', 'basename', 'dirname', 'realpath', 'readlink', 'echo', 'printf', 'true', 'pwd', 'date', 'id',
             'whoami', 'hostname', 'uname', 'free', 'uptime', 'lscpu', 'lsblk', 'getent', 'ps', 'ss', 'netstat', 'tree', 'cd'}
META_CMDS = {'ls', 'stat', 'du', 'wc', 'file', 'find'}
OTHER = {'systemctl', 'journalctl', 'docker', 'git', 'crontab', 'python3', 'bash', 'mkdir', 'touch'}
OPS = {'&&', '||', ';', '|', '&', ';;'}
SYSTEMCTL_OK = {'status', 'show', 'is-active', 'is-enabled', 'list-units', 'list-unit-files', 'list-timers', 'list-dependencies', 'list-sockets'}
DOCKER_OK = {'ps', 'images', 'version', 'info', 'logs', 'top', 'port'}
GIT_OK = {'log', 'show', 'diff', 'status', 'rev-parse', 'branch', 'ls-files', 'blame', 'describe', 'rev-list', 'shortlog', 'tag', 'cat-file', 'ls-tree', 'show-ref'}
JOURNAL_BAD = ('--vacuum', '--rotate', '--flush', '--sync', '--relinquish', '--setup-keys', '--update-catalog', '--smart-relinquish')
FIND_BAD = {'-exec', '-execdir', '-ok', '-okdir', '-delete', '-fprint', '-fprint0', '-fprintf', '-fls'}


def is_secret(tok):
    if EXAMPLE.search(tok):
        return False
    return any(re.search(p, tok) for p in SECRET_RES)


def secret_token(tok, cwd, meta):
    cands = {tok, os.path.expanduser(tok)}
    base = os.path.join(cwd, tok) if not os.path.isabs(os.path.expanduser(tok)) else os.path.expanduser(tok)
    cands.add(base)
    try:
        cands.add(os.path.realpath(base))
    except Exception:
        pass
    if any(c in tok for c in '*?['):
        for m in glob.glob(base, recursive=True)[:500]:
            cands.add(m); cands.add(os.path.realpath(m))
        pat = os.path.basename(tok)
        for nm in ('.env', '.env.local', 'id_vps', 'id_rsa', 'id_ed25519', 'environ', 'config.json', 'authorized_keys', '.netrc'):
            if (not nm.startswith('.') or pat.startswith('.')) and fnmatch.fnmatch(nm, pat):
                cands.add('/glob/' + nm)
    if any(is_secret(c) for c in cands):
        if meta:
            return False
        return True
    return False


def under(path, root):
    p = os.path.realpath(os.path.expanduser(path))
    return p == root or p.startswith(root + "/")


def check_segment(seg, cwd, whole):
    if not seg:
        deny("bos komut parcasi")
    name = seg[0]
    if '/' in name:
        if os.path.dirname(name) not in BINDIRS:
            deny("yol ile komut calistirma yasak: %s" % name)
        name = os.path.basename(name)
    if '=' in name:
        deny("ortam degiskeni atamali komut yasak")
    args = seg[1:]
    if name not in READ_CMDS | META_CMDS | OTHER:
        deny("izinli komut listesinde degil: %s" % name)
    meta = name in META_CMDS
    if name == 'find' and any(a in FIND_BAD for a in args):
        deny("find ile calistirma/silme/yazma yasak")
    if name == 'awk' and re.search(r'system\s*\(|getline|\|\s*"|print\s*>|printf\s*>', ' '.join(args)):
        deny("awk ile komut/yazma yasak")
    if name == 'sed':
        if not any(a == '-n' or (a.startswith('-') and not a.startswith('--') and 'n' in a) for a in args):
            deny("sed yalnizca -n ile")
        if any(a.startswith('-i') or a == '--in-place' for a in args) or any(re.search(r'(^|[;{}\s0-9$/])[we]\b', a) for a in args if not a.startswith('-')):
            deny("sed ile yazma/calistirma yasak")
    if name in ('grep', 'egrep', 'fgrep'):
        rec = any((a.startswith('-') and not a.startswith('--') and re.search('[rR]', a)) or a in ('--recursive', '--dereference-recursive') for a in args)
        if rec and not (any(a.startswith('--exclude=') and '.env' in a for a in args) and any(a.startswith('--exclude=') and 'config' in a for a in args)):
            deny("ozyinelemeli grep icin --exclude='.env*' ve --exclude='config*.json' zorunlu")
    if name == 'systemctl':
        sub = next((a for a in args if not a.startswith('-')), '')
        if sub not in SYSTEMCTL_OK or re.search('environment', ' '.join(args), re.I):
            deny("systemctl yalnizca izinli alt komutlar (cat/Environment yasak)")
    if name == 'journalctl' and any(a.startswith(JOURNAL_BAD) for a in args):
        deny("journalctl yazan secenek yasak")
    if name == 'docker':
        sub = args[0] if args else ''
        if sub in ('volume', 'network'):
            if args[1:2] != ['ls']:
                deny("docker volume/network yalnizca ls")
        elif sub not in DOCKER_OK:
            deny("docker alt komutu izinli degil: %s" % sub)
    if name == 'git':
        gargs, skip = [], False
        for a in args:
            if skip:
                skip = False; continue
            if a == '-C':
                skip = True; continue
            gargs.append(a)
        args = gargs
        sub = next((a for a in args if not a.startswith('-')), '')
        if sub not in GIT_OK or any(a == '-c' or a.startswith('--exec-path') for a in args):
            deny("git alt komutu izinli degil: %s" % sub)
        if sub == 'tag' and any(not a.startswith('-') for a in args[args.index('tag') + 1:]):
            deny("git tag olusturma yasak")
        if sub == 'branch' and any(not a.startswith('-') for a in args[args.index('branch') + 1:]):
            deny("git branch olusturma yasak")
    if name == 'crontab' and args != ['-l']:
        deny("crontab yalnizca -l")
    if name == 'python3' and not (args[:1] == ['--version'] or (args and args[0].startswith(KB + "/EVIDENCE/tools/kb_"))):
        deny("python3 yalnizca KB/EVIDENCE/tools/kb_*.py")
    if name == 'bash' and not ((args[:1] == ['-n'] and len(args) == 2) or (args and args[0] == KB + "/EVIDENCE/kb_collect.sh")):
        deny("bash yalnizca 'bash -n <dosya>' veya KB/EVIDENCE/kb_collect.sh")
    if name in ('mkdir', 'touch'):
        for a in args:
            if not a.startswith('-') and (not under(a, KB) or under(a, RC)):
                deny("%s yalnizca KB altinda (rc hariç)" % name)
    for a in args:
        if a.startswith('-') and len(a) > 1 and not a.startswith('--'):
            continue
        if secret_token(a, cwd, meta):
            deny("gizli dosya/deger okumasi yasak: %s" % a)


def check(cmd, cwd):
    if '\n' in cmd or '\r' in cmd:
        deny("cok satirli komut yasak")
    if re.search(r'\$\(|`|<\(|>\(|<<|\$\{|\\x[0-9a-fA-F]{2}', cmd):
        deny("komut/surec degistirme, heredoc veya kacis dizisi yasak")
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    toks = list(lex)
    segs, cur, i = [], [], 0
    while i < len(toks):
        t = toks[i]
        if t in OPS:
            segs.append(cur); cur = []
        elif t in ('>', '>>'):
            tgt = toks[i + 1] if i + 1 < len(toks) else ''
            if not (tgt == '/dev/null' or (under(tgt, KB) and not under(tgt, RC))):
                deny("yonlendirme yalnizca KB altina (rc hariç) veya /dev/null: %r" % tgt)
            i += 1
        elif t == '>&':
            i += 1
        elif t in ('2', '1') and i + 1 < len(toks) and toks[i + 1] in ('>', '>>', '>&'):
            pass
        elif set(t) <= set('<>|&;()'):
            deny("desteklenmeyen kabuk operatoru: %s" % t)
        else:
            cur.append(t)
        i += 1
    segs.append(cur)
    for s in segs:
        check_segment(s, cwd, cmd)


try:
    data = json.load(sys.stdin)
    command = data["tool_input"]["command"]
    cwd = data.get("cwd") or os.getcwd()
except Exception as e:
    deny("girdi okunamadi (kapali-hata): %r" % (e,))
try:
    check(command, cwd)
except SystemExit:
    raise
except Exception as e:
    deny("beklenmeyen hata (kapali-hata): %r" % (e,))
sys.exit(0)
