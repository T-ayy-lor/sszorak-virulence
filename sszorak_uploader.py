#!/usr/bin/env python3
"""
Raid uploader: watches your WoW combat log and, after every pull or wipe, pushes the night's data
to your GitHub Pages repository. The pages pick it up on their own.
  Sszorak: positions, Virulence casts, explosions, damage, Tempest -> nights/<log name>.json, listed in nights.json
  Twin Fangs: every cast of the spell the raid kicks, who kicked it and how fast -> fangs-nights/<log name>.json, listed in fangs.json
"""
import base64, codecs, csv, getpass, glob, json, os, re, ssl, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

# ---- settings ----
LOG_DIR = '/Applications/World of Warcraft/_retail_/Logs'
REPO = 't-ayy-lor/sszorak-virulence'      # owner/repository of your GitHub Pages site
BRANCH = 'main'
NIGHT_DIR = 'nights'                       # one file per night (per combat log)
INDEX_PATH = 'nights.json'                 # list of all nights, read by the page's night picker
TOKEN_FILE = os.path.expanduser('~/.sszorak_uploader_token')
POLL_SECONDS = 3
MIN_UPLOAD_GAP = 360        # seconds between uploads: GitHub Pages has a soft limit of 10 site builds per hour

ENC, BOSS_NPC = '3420', '-257347-'           # Sszorak encounter ID and creature ID
VIR_A, VIR_B, BOOM = '1297707', '1299899', '1300089'
TEMPEST, TEMPEST_CAST = '1287083', '1287072'   # Tempest hit/debuff and the boss's Tempest cast
SURGE_CAST, SURGE_DEBUFF = '1305959', '1305963'   # Venomous Surge: the boss's cast, and the debuff on 2 ranged players
SURGE_LEN = 9.5                                # a debuff that lasted at least this long ran out on its own (it lasts 10 s)
TELEPORT = '48020'                             # Demonic Circle: Teleport, counted per player per pull
DMG_STEP = 5                                   # seconds per damage window in each pull's timeline
FANGS_SPELL = '1308385'                        # Twin Fangs: the cast the raid has to kick
FANGS_DIR, FANGS_INDEX = 'fangs-nights', 'fangs.json'   # not 'fangs/': a folder by that name would block the /fangs page link


class Parser:
    """Reads combat-log text and keeps only Sszorak encounters."""
    def __init__(self):
        self.mark = 'ENCOUNTER_START,' + ENC + ','
        self.pulls, self.cur, self.leftover = [], None, ''
        self.names, self.pets, self.specs = {}, {}, {}
        self.finished = 0          # count of pulls that have ended

    @staticmethod
    def split(s):
        return s.split(',') if '"' not in s else next(csv.reader([s]))

    _days = {}
    @classmethod
    def ts(cls, line):
        d, rest = line.split(' ', 1)
        base = cls._days.get(d)
        if base is None:
            m, dd, y = d.split('/')
            base = cls._days[d] = datetime(int(y), int(m), int(dd), tzinfo=timezone.utc).timestamp()
        h, mi, sec = rest[:rest.index('  ')].split(':')
        sec = float(sec.split('-')[0].split('+')[0])
        return base + int(h) * 3600 + int(mi) * 60 + sec

    def start(self, line, rest):
        r = self.split(rest)
        clock = line.split(' ', 1)[1][:8].split('.')[0]
        self.cur = dict(n=len(self.pulls) + 1, t0=self.ts(line), clock=clock, date=line.split(' ', 1)[0],
                        diff=int(r[3]), size=int(r[4]), ended=False, kill=False, dur=0.0, lastT=0.0,
                        track={}, specGuid={}, aura=[], boom=[], died={}, bossGuid=None, boss=[], bossHp=None, dmg={},
                        tempest={}, tempestCasts=0, now=0.0, ports={}, surgeCasts=[], sur=[], sdeb={})
        self.pulls.append(self.cur)

    def end(self, rest):
        c = self.cur
        if c is None: return
        if rest:
            r = rest.split(',')
            c['kill'] = r[5] == '1'
            try: c['dur'] = int(r[6]) / 1000 or c['lastT']
            except ValueError: c['dur'] = c['lastT']
        else:
            c['dur'] = c['lastT']
        c['ended'] = True
        self.cur = None
        self.finished += 1

    @staticmethod
    def add_pt(arr, t, x, y):
        if arr and t - arr[-1][0] < 0.1: return
        arr.append((t, x, y))

    def credit(self, c, r, val):
        if not val > 0: return
        try:
            if int(r[7], 16) & 0x10: return          # only damage to non-friendly units
        except ValueError:
            return
        src = r[1]
        owner = src if src.startswith('Player-') else self.pets.get(src)
        if not owner or not owner.startswith('Player-'): return
        d = c['dmg'].setdefault(owner, {'all': 0.0, 'boss': 0.0, 'b': []})
        d['all'] += val
        i = int(c['now'] // DMG_STEP)                # damage per DMG_STEP-second window, for the timeline graph
        if i >= len(d['b']): d['b'].extend([0.0] * (i + 1 - len(d['b'])))
        d['b'][i] += val
        if BOSS_NPC in r[5]: d['boss'] += val

    @staticmethod
    def tempest(c, g, amount, direct, t):
        d = c['tempest'].setdefault(g, {'hits': 0, 'dmg': 0.0, 't': []})
        if direct:
            d['hits'] += 1
            d['t'].append(round(t, 1))
        d['dmg'] += max(amount, 0)

    def event(self, line, rest):
        c = self.cur
        t = self.ts(line) - c['t0']
        c['now'] = t
        c['lastT'] = t
        ev = rest[:rest.index(',')]
        if ev == 'COMBATANT_INFO':
            r = rest.split(',', 27)
            try: c['specGuid'][r[1]] = int(r[25])
            except (ValueError, IndexError): pass
            return
        if ev == 'UNIT_DIED':
            r = self.split(rest)
            if len(r) > 5 and r[5].startswith('Player-') and r[5] not in c['died']: c['died'][r[5]] = t
            return
        swing = ev.startswith('SWING_')
        spell = ev.startswith('SPELL_') or ev.startswith('RANGE_')
        if not swing and not spell: return
        if ev in ('SPELL_AURA_APPLIED', 'SPELL_AURA_REMOVED'):
            if SURGE_DEBUFF in rest:
                r = self.split(rest)
                if r[9] == SURGE_DEBUFF and r[5].startswith('Player-'):
                    self.names[r[5]] = r[6].split('-')[0]
                    if ev == 'SPELL_AURA_APPLIED':
                        rec = {'g': r[5], 'on': t, 'off': None}
                        c['sdeb'][r[5]] = rec; c['sur'].append(rec)
                    else:
                        rec = c['sdeb'].pop(r[5], None)
                        if rec: rec['off'] = t
                return
            if VIR_A not in rest and VIR_B not in rest: return
            r = self.split(rest)
            if r[9] in (VIR_A, VIR_B) and r[5].startswith('Player-'):
                c['aura'].append((t, ev[11:], r[9], r[5]))
                self.names[r[5]] = r[6].split('-')[0]
            return
        if ev.startswith('SPELL_AURA') or ev in ('SPELL_ABSORBED', 'SWING_DAMAGE_LANDED', 'SPELL_CAST_START'): return
        r = self.split(rest)
        if len(r) > 2 and r[1].startswith('Player-'): self.names[r[1]] = r[2].split('-')[0]
        if len(r) > 6 and r[5].startswith('Player-'): self.names[r[5]] = r[6].split('-')[0]
        if not c['bossGuid'] and len(r) > 5:
            if BOSS_NPC in r[1]: c['bossGuid'] = r[1]
            elif BOSS_NPC in r[5]: c['bossGuid'] = r[5]
        if ev == 'SPELL_CAST_SUCCESS' and r[9] == TEMPEST_CAST and BOSS_NPC in r[1]:
            c['tempestCasts'] += 1
        if ev == 'SPELL_CAST_SUCCESS' and r[9] == SURGE_CAST and BOSS_NPC in r[1]:
            c['surgeCasts'].append(t)
        if ev == 'SPELL_CAST_SUCCESS' and r[9] == TELEPORT and r[1].startswith('Player-'):
            c['ports'][r[1]] = c['ports'].get(r[1], 0) + 1
        if ev == 'SPELL_SUMMON':
            if r[1].startswith('Player-'): self.pets[r[5]] = r[1]
            elif r[1] in self.pets: self.pets[r[5]] = self.pets[r[1]]
            return
        k = 9 if swing else 12
        x = y = None
        if len(r) > k + 17 and '-' in r[k]:
            try:
                x, y = float(r[k + 14]), float(r[k + 15])
            except ValueError:
                x = y = None
            info, own = r[k], r[k + 1]
            if own and own != '0000000000000000' and own.startswith('Player-') and not info.startswith('Player-'):
                self.pets[info] = own
            if x is not None:
                if info.startswith('Player-'):
                    self.add_pt(c['track'].setdefault(info, []), t, x, y)
                elif info == c['bossGuid']:
                    self.add_pt(c['boss'], t, x, y)
                    try:
                        hp, mx = float(r[k + 2]), float(r[k + 3])
                        if mx > 0: c['bossHp'] = (hp, mx)
                    except ValueError:
                        pass
        if ev in ('SPELL_DAMAGE', 'SPELL_PERIODIC_DAMAGE', 'RANGE_DAMAGE', 'SWING_DAMAGE'):
            if ev == 'SPELL_DAMAGE' and r[9] == BOOM and r[5].startswith('Player-') and x is not None:
                c['boom'].append((t, r[5], x, y))
            try:
                amt, over, ab = float(r[k + 19]), float(r[k + 21]), float(r[k + 25])
            except (ValueError, IndexError):
                return
            self.credit(c, r, amt - max(over, 0) + max(ab, 0))
            if r[9] == TEMPEST and r[5].startswith('Player-'):
                self.tempest(c, r[5], amt - max(over, 0) + max(ab, 0), ev == 'SPELL_DAMAGE', t)
        elif ev.endswith('_MISSED'):
            m = 9 if swing else 12
            if len(r) > m + 2 and r[m] == 'ABSORB':
                try: self.credit(c, r, float(r[m + 2]))
                except ValueError: pass
            if not swing and r[9] == TEMPEST and r[5].startswith('Player-'):
                amt = 0.0
                if len(r) > m + 2 and r[m] == 'ABSORB':
                    try: amt = float(r[m + 2])
                    except ValueError: pass
                self.tempest(c, r[5], amt, ev == 'SPELL_MISSED', t)

    def line(self, l):
        l = l.rstrip('\r')
        sep = l.find('  ')
        if sep < 0: return
        rest = l[sep + 2:]
        if self.cur is None:
            if rest.startswith(self.mark): self.start(l, rest)
            return
        if rest.startswith('ENCOUNTER_END,'):
            self.end(rest); return
        if rest.startswith('ENCOUNTER_START,'):
            self.end(None)
            if rest.startswith(self.mark): self.start(l, rest)
            return
        try:
            self.event(l, rest)
        except (IndexError, ValueError):
            pass

    def feed(self, text):
        text = self.leftover + text
        end = text.rfind('\n')
        if end < 0:
            self.leftover = text; return
        pos = 0
        while pos < end:
            if self.cur is None:
                m = text.find(self.mark, pos)
                if m < 0 or m >= end:
                    break
                pos = text.rfind('\n', 0, m) + 1
            nl = text.find('\n', pos)
            if nl < 0 or nl > end: nl = end
            self.line(text[pos:nl])
            pos = nl + 1
        self.leftover = text[end + 1:]

    # ---- output in the page's data shape ----
    @staticmethod
    def pos_at(tr, t):
        if not tr: return None
        lo, hi = 0, len(tr)
        while lo < hi:
            mid = (lo + hi) // 2
            if tr[mid][0] < t: lo = mid + 1
            else: hi = mid
        a = tr[lo - 1] if lo > 0 else None
        b = tr[lo] if lo < len(tr) else None
        if a and b and b[0] - a[0] < 1.6 and b[0] != a[0]:
            f = (t - a[0]) / (b[0] - a[0])
            return [round(a[1] + (b[1] - a[1]) * f, 2), round(a[2] + (b[2] - a[2]) * f, 2)]
        cand = [p for p in (a, b) if p]
        best = min(cand, key=lambda p: abs(p[0] - t)) if cand else None
        return [round(best[1], 2), round(best[2], 2)] if best and abs(best[0] - t) <= 1.0 else None

    @staticmethod
    def surge_groups(p):
        """Venomous Surges in a pull: each boss cast and the debuffs it put on players (applied a few seconds after)."""
        groups = []
        for rec in sorted(p['sur'], key=lambda d: d['on']):
            cast = max((t for t in p['surgeCasts'] if 0 <= rec['on'] - t <= 8), default=None)
            g = groups[-1] if groups else None
            if g and ((cast is not None and g['cast'] == cast) or (cast is None and g['cast'] is None and rec['on'] - g['deb'][-1]['on'] <= 4)):
                g['deb'].append(rec)
            else:
                groups.append({'cast': cast, 't': cast if cast is not None else rec['on'], 'deb': [rec]})
        return groups

    def build_surges(self, p, nm):
        out = []
        for g in self.surge_groups(p):
            deb = []
            for d in g['deb']:
                tr, off = p['track'].get(d['g'], []), d['off']
                died = p['died'].get(d['g'])
                if off is None: end = 'open'
                elif off - d['on'] >= SURGE_LEN: end = 'expired'
                elif died is not None and abs(died - off) <= 0.6: end = 'died'
                else: end = 'early'
                stop = off if off is not None else p['lastT']
                trail = [self.pos_at(tr, d['on'] + k * 0.5) for k in range(int((stop - d['on']) / 0.5) + 1)]
                deb.append({'n': nm(d['g']), 'on': round(d['on'] - g['t'], 2), 'off': round(stop - g['t'], 2), 'end': end,
                            'pos': self.pos_at(tr, stop), 'tr': trail})
            out.append({'t': round(g['t'], 2), 'deb': deb})
        return out

    def build(self, log_name):
        nm = lambda g: self.names.get(g, g)
        for p in self.pulls:
            for g, sp in p['specGuid'].items():
                if g in self.names: self.specs[self.names[g]] = sp
        pulls = []
        for p in self.pulls:
            if p['ended'] and (p['dur'] or 0) < 2: continue     # instant resets
            casts = []
            for t, ev, sid, g in sorted(p['aura']):
                if ev != 'APPLIED': continue
                if not casts or t - casts[-1]['t'] > 10: casts.append({'t': t, 'assign': {}, 'extra': []})
                c = casts[-1]
                if t - c['t'] <= 1.5:
                    c['assign'].setdefault(g, 'A' if sid == VIR_A else 'B')
                else:
                    c['extra'].append([round(t - c['t'], 2), nm(g), 'A' if sid == VIR_A else 'B'])
            out = []
            for c in casts:
                players = {}
                for g, typ in c['assign'].items():
                    tr = p['track'].get(g)
                    if not tr: continue
                    dt = p['died'].get(g)
                    s = []
                    for k in range(27):
                        t = c['t'] + k * 0.25
                        s.append(None if (dt is not None and t > dt + 0.05) or t > p['lastT'] + 0.05 else self.pos_at(tr, t))
                    players[nm(g)] = {'type': typ, 's': s}
                out.append({'t': round(c['t'], 2), 'boss': self.pos_at(p['boss'], c['t'] + 5), 'players': players, 'extra': c['extra'],
                            'booms': [[round(b[0] - c['t'], 2), nm(b[1]), round(b[2], 2), round(b[3], 2)] for b in p['boom'] if 0 <= b[0] - c['t'] <= 10],
                            'deaths': [[round(t - c['t'], 2), nm(g)] for g, t in p['died'].items() if 0 <= t - c['t'] <= 10]})
            pulls.append({'n': len(pulls) + 1, 'clock': p['clock'], 'date': p['date'], 'diff': p['diff'],
                          'dur': round(p['dur'] if p['ended'] else p['lastT'], 1), 'kill': p['kill'], 'live': not p['ended'],
                          'hp': (p['bossHp'][0] / p['bossHp'][1]) if p['bossHp'] else None, 'casts': out,
                          'dmg': {nm(g): {'all': round(d['all']), 'boss': round(d['boss']), 'b': [round(v) for v in d['b']]} for g, d in p['dmg'].items()},
                          'dmgStep': DMG_STEP,
                          'tempest': {nm(g): {'hits': d['hits'], 'dmg': round(d['dmg']), 't': d['t']} for g, d in p['tempest'].items()},
                          'tempestCasts': p['tempestCasts'],
                          'ports': {nm(g): n for g, n in p['ports'].items()},
                          'surges': self.build_surges(p, nm)})
        return {'step': 0.25, 'log': log_name, 'updated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'pulls': pulls, 'specs': dict(self.specs)}


class FangsParser:
    """Records every cast of FANGS_SPELL and the kick that stopped it, in whatever encounter it shows up in
    (so it works before we know Twin Fangs' encounter ID). Encounters without that cast are dropped."""
    KEYS = re.compile('ENCOUNTER_START|ENCOUNTER_END|COMBATANT_INFO|' + FANGS_SPELL)

    def __init__(self):
        self.pulls, self.cur, self.leftover = [], None, ''
        self.names, self.specs = {}, {}
        self.finished = 0          # count of kept pulls that have ended

    def feed(self, text):
        text = self.leftover + text
        end = text.rfind('\n')
        if end < 0:
            self.leftover = text; return
        last = -1
        for m in self.KEYS.finditer(text, 0, end):          # only lines that can matter; everything else is skipped
            a = text.rfind('\n', 0, m.start()) + 1
            if a == last: continue
            last = a
            b = text.find('\n', m.start())
            try: self.line(text[a:b if b >= 0 else end])
            except (IndexError, ValueError): pass
        self.leftover = text[end + 1:]

    def close(self, r):
        c = self.cur
        if r: c['kill'], c['dur'] = r[5] == '1', int(r[6]) / 1000
        else: c['dur'] = c['lastT']
        c['ended'] = True
        if c['casts']:
            self.pulls.append(c); self.finished += 1
        self.cur = None

    def line(self, l):
        l = l.rstrip('\r')
        sep = l.find('  ')
        if sep < 0: return
        rest = l[sep + 2:]
        ev = rest[:rest.find(',')]
        if ev == 'ENCOUNTER_START':
            if self.cur: self.close(None)
            r = Parser.split(rest)
            self.cur = dict(enc=r[1], encName=r[2], t0=Parser.ts(l), clock=l.split(' ', 1)[1][:8].split('.')[0], date=l.split(' ', 1)[0],
                            diff=int(r[3]), dur=0.0, kill=False, ended=False, lastT=0.0, casts=[], open={}, specGuid={})
            return
        c = self.cur
        if c is None: return
        t = Parser.ts(l) - c['t0']; c['lastT'] = t
        if ev == 'ENCOUNTER_END':
            self.close(rest.split(',')); return
        if ev == 'COMBATANT_INFO':
            r = rest.split(',', 27); c['specGuid'][r[1]] = int(r[25]); return
        r = Parser.split(rest)
        if ev == 'SPELL_CAST_START' and r[9] == FANGS_SPELL:
            cast = {'t': t, 'src': r[1], 'name': r[2], 'kick': None, 'res': None}
            c['casts'].append(cast); c['open'][r[1]] = cast
        elif ev == 'SPELL_INTERRUPT' and FANGS_SPELL in (r[12] if len(r) > 12 else '', r[29] if len(r) > 29 else ''):
            cast = c['open'].pop(r[5], None)
            who = r[2].split('-')[0]
            if r[1].startswith('Player-'): self.names[r[1]] = who
            if cast:
                cast['kick'] = {'by': who, 'pet': not r[1].startswith('Player-'), 'spell': r[10], 'd': t - cast['t']}
                cast['res'] = 'kicked'
        elif ev == 'SPELL_CAST_SUCCESS' and r[9] == FANGS_SPELL:
            cast = c['open'].pop(r[1], None)
            if cast: cast['res'] = 'completed'
        elif ev == 'SPELL_CAST_FAILED' and r[9] == FANGS_SPELL:
            cast = c['open'].pop(r[1], None)
            if cast: cast['res'] = 'stopped'

    def build(self, log_name):
        pulls = self.pulls + ([self.cur] if self.cur and self.cur['casts'] else [])
        for p in pulls:
            for g, sp in p['specGuid'].items():
                if g in self.names: self.specs[self.names[g]] = sp
        out, count = [], {}
        for p in pulls:
            count[p['enc']] = count.get(p['enc'], 0) + 1
            out.append({'n': count[p['enc']], 'enc': p['enc'], 'encName': p['encName'], 'clock': p['clock'], 'date': p['date'], 'diff': p['diff'],
                        'dur': round(p['dur'] if p['ended'] else p['lastT'], 1), 'kill': p['kill'], 'live': not p['ended'],
                        'casts': [{'t': round(k['t'], 2), 'caster': k['name'], 'src': k['src'].rsplit('-', 1)[-1],
                                   'kick': dict(k['kick'], d=round(k['kick']['d'], 3)) if k['kick'] else None, 'res': k['res']} for k in p['casts']]})
        return {'log': log_name, 'spell': int(FANGS_SPELL), 'updated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'pulls': out, 'specs': dict(self.specs)}


# ---- GitHub ----
def get_token():
    tok = os.environ.get('GITHUB_TOKEN')
    if tok: return tok.strip()
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f: return f.read().strip()
    print('Paste your GitHub token (it will not show as you type), then press Return:')
    tok = getpass.getpass('> ').strip()
    with open(TOKEN_FILE, 'w') as f: f.write(tok)
    os.chmod(TOKEN_FILE, 0o600)
    print(f'Saved to {TOKEN_FILE}')
    return tok

def gh(method, url, token, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                                          'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'sszorak-uploader'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b'{}')

def fmt_size(n):
    return f'{n / 1048576:.1f} MB' if n >= 1048576 else f'{n / 1024:.0f} KB' if n >= 1024 else f'{n} B'

def bar(done, total, label='', width=28):
    """Draw a one-line progress bar in Terminal (redrawn in place)."""
    frac = min(1.0, done / total) if total else 1.0
    filled = int(width * frac)
    sys.stdout.write(f'\r  [{"#" * filled}{"-" * (width - filled)}] {frac * 100:5.1f}%  {label}\033[K')
    sys.stdout.flush()

class Uploader:
    """Publishes the night file and nights.json together as ONE commit (one Pages build)."""
    def __init__(self, token):
        self.token = token

    def api(self, method, path, body=None):
        return gh(method, f'https://api.github.com/repos/{REPO}/{path}', self.token, body)

    def publish(self, parts, message):
        """parts: list of (night_file, data, index_path, index_entry). Everything goes up as one commit."""
        texts = [(f, json.dumps(d, separators=(',', ':'), ensure_ascii=False)) for f, d, _, _ in parts]
        steps = 5 + len(parts) * 2
        for attempt in range(3):
            bar(0, steps, 'Checking the latest commit')
            head = self.api('GET', f'git/ref/heads/{BRANCH}')['object']['sha']
            bar(1, steps, 'Reading the latest commit')
            base_tree = self.api('GET', f'git/commits/{head}')['tree']['sha']
            step = 2
            for _, _, ip, entry in parts:
                bar(step, steps, f'Reading {ip}'); step += 1
                try:
                    cur = self.api('GET', f'contents/{ip}?ref={head}')
                    idx = json.loads(base64.b64decode(cur['content']).decode())
                except urllib.error.HTTPError as e:
                    if e.code != 404: raise
                    idx = None
                texts.append((ip, json.dumps(merge_index(idx, entry), indent=1, ensure_ascii=False)))
            entries, total = [], 0
            for path, text in texts[:len(parts) * 2]:
                bar(min(step, steps - 3), steps, f'Uploading {path} ({fmt_size(len(text.encode()))})'); step += 1
                total += len(text.encode())
                blob = self.api('POST', 'git/blobs', {'content': text, 'encoding': 'utf-8'})
                entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
            bar(steps - 3, steps, 'Building the file tree')
            tree = self.api('POST', 'git/trees', {'base_tree': base_tree, 'tree': entries})
            bar(steps - 2, steps, 'Creating the commit')
            commit = self.api('POST', 'git/commits', {'message': message, 'tree': tree['sha'], 'parents': [head]})
            bar(steps - 1, steps, 'Publishing the commit')
            try:
                self.api('PATCH', f'git/refs/heads/{BRANCH}', {'sha': commit['sha'], 'force': False})
                bar(steps, steps, f'Done · {fmt_size(total)} uploaded')
                print()
                return
            except urllib.error.HTTPError as e:
                if e.code == 422 and attempt < 2:            # someone else committed meanwhile; rebuild on top of it
                    print(); print(f'[{stamp()}] GitHub has a newer commit; rebuilding on top of it')
                    texts = texts[:len(parts)]
                    continue
                raise


def date_key(d):
    m, dd, y = (d or '01/01/1970').split('/')
    return f'{y}{int(m):02d}{int(dd):02d}'

def night_entry(data, night_file):
    pulls = data['pulls']
    first = pulls[0] if pulls else {}
    hps = [p['hp'] for p in pulls if p.get('hp') is not None and not p['kill']]   # Twin Fangs pulls carry no hp
    return {'file': night_file, 'log': data['log'], 'date': first.get('date'), 'diff': first.get('diff'),
            'pulls': len(pulls), 'kill': any(p['kill'] for p in pulls), 'best': min(hps) if hps else None,
            'updated': data['updated']}

def merge_index(idx, entry):
    idx = idx if isinstance(idx, dict) and 'nights' in idx else {'nights': []}
    idx['nights'] = [e for e in idx['nights'] if e.get('file') != entry['file']] + [entry]
    idx['nights'].sort(key=lambda e: (date_key(e.get('date')), e.get('log', '')))
    return idx


def newest_log():
    files = glob.glob(os.path.join(LOG_DIR, 'WoWCombatLog*.txt'))
    return max(files, key=os.path.getmtime) if files else None

def stamp(): return datetime.now().strftime('%H:%M:%S')

def main():
    dry = '--dry-run' in sys.argv           # write the night file and nights.json locally instead of uploading
    out_dir = sys.argv[sys.argv.index('--out') + 1] if '--out' in sys.argv else os.path.dirname(os.path.abspath(__file__))
    once = '--once' in sys.argv             # read the log once, upload, and exit
    if '--log' in sys.argv: path_override = sys.argv[sys.argv.index('--log') + 1]
    else: path_override = None
    global ENC, BOSS_NPC
    if '--enc' in sys.argv: ENC = sys.argv[sys.argv.index('--enc') + 1]
    if '--boss' in sys.argv: BOSS_NPC = '-' + sys.argv[sys.argv.index('--boss') + 1] + '-'

    up = None if dry else Uploader(get_token())
    if up:
        try: gh('GET', f'https://api.github.com/repos/{REPO}', up.token)
        except urllib.error.HTTPError as e:
            sys.exit(f'GitHub said {e.code} for {REPO}. Check the token has Contents read and write on that repository.')
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLError):
                sys.exit('Python could not verify GitHub\'s certificate. If you installed Python from python.org, '
                         'open Applications > Python 3.x and run "Install Certificates.command", then try again.')
            sys.exit(f'Could not reach GitHub: {e.reason}')
        print(f'[{stamp()}] Connected to github.com/{REPO}')

    path, fh, parser, fangs, seen_finished, pending, last_upload = None, None, None, None, None, False, 0.0

    dec, pos = None, 0
    print(f'[{stamp()}] Watching {LOG_DIR} for Sszorak and Twin Fangs pulls. Press Control-C to stop.')


    def send():
        """Build the night from everything read so far and publish it. Returns True on success."""
        log_name = os.path.basename(path).replace('Archive-', '', 1)
        stem = os.path.splitext(log_name)[0]                 # logs moved by the Warcraft Logs uploader app lose their Archive- prefix
        parts, whats = [], []
        if parser.pulls:
            data = parser.build(log_name)
            last = data['pulls'][-1]
            whats.append(f"Sszorak pull {last['n']} " + ('kill' if last['kill'] else 'in progress' if last['live'] else
                         f"wipe at {last['hp'] * 100:.1f}%" if last['hp'] is not None else 'wipe'))
            f = f'{NIGHT_DIR}/{stem}.json'; parts.append((f, data, INDEX_PATH, night_entry(data, f)))
        fdata = fangs.build(log_name)
        if fdata['pulls']:
            last = fdata['pulls'][-1]
            whats.append(f"Twin Fangs pull {last['n']} " + ('kill' if last['kill'] else 'in progress' if last['live'] else 'wipe'))
            f = f'{FANGS_DIR}/{stem}.json'; parts.append((f, fdata, FANGS_INDEX, night_entry(fdata, f)))
        if not parts: return True
        what = ', '.join(whats)
        if dry:
            for f, d, ip, entry in parts:
                os.makedirs(os.path.join(out_dir, os.path.dirname(f)), exist_ok=True)
                with open(os.path.join(out_dir, f), 'w') as fh_: json.dump(d, fh_, separators=(',', ':'), ensure_ascii=False)
                ipp = os.path.join(out_dir, ip)
                idx = json.load(open(ipp)) if os.path.exists(ipp) else None
                with open(ipp, 'w') as fh_: json.dump(merge_index(idx, entry), fh_, indent=1, ensure_ascii=False)
                print(f'[{stamp()}] Wrote {f} ({fmt_size(os.path.getsize(os.path.join(out_dir, f)))}) and {ip} in {out_dir} ({len(d["pulls"])} pulls)')
            print(f'[{stamp()}] Latest: {what}')
            return True
        size = sum(len(json.dumps(d, separators=(',', ':'), ensure_ascii=False).encode()) for _, d, _, _ in parts)
        print(f'[{stamp()}] Uploading {", ".join(f for f, _, _, _ in parts)} ({fmt_size(size)}): {what}')
        try:
            up.publish(parts, f'Raid data: {what}')
            print(f'[{stamp()}] Uploaded. The site updates in a minute or two.')
            return True
        except Exception as e:
            print(); print(f'[{stamp()}] Upload failed, will try again shortly: {e}')
            return False

    try:
        while True:
            newest = path_override or newest_log()
            if newest and newest != path:
                if fh: fh.close()
                path, fh, parser, fangs = newest, open(newest, 'rb'), Parser(), FangsParser()
                dec, pos = codecs.getincrementaldecoder('utf-8')(errors='replace'), 0
                seen_finished, pending = None, False
                print(f'[{stamp()}] Reading {os.path.basename(path)} ({fmt_size(os.path.getsize(path))})')
            if fh:
                total = os.path.getsize(path)
                show = total - pos > (32 << 20)            # only draw a bar when there's a lot to read
                while True:
                    chunk = fh.read(8 << 20)
                    if not chunk: break
                    pos += len(chunk)
                    text = dec.decode(chunk)
                    parser.feed(text); fangs.feed(text)
                    if show: bar(pos, total, f'Reading log  {fmt_size(pos)} of {fmt_size(total)}')
                if show:
                    n = sum(1 for p in parser.pulls if not p['ended'] or (p['dur'] or 0) >= 2)   # same pulls the site shows
                    nf = len(fangs.pulls) + (1 if fangs.cur and fangs.cur['casts'] else 0)
                    print(f'\n[{stamp()}] Found {n} Sszorak pull{"" if n == 1 else "s"} and {nf} Twin Fangs pull{"" if nf == 1 else "s"} so far')
                # a pull ended (or first look at a log with Sszorak pulls): queue an upload
                wait = MIN_UPLOAD_GAP - (time.time() - last_upload)
                # only a finished pull queues an upload; a pull that just started doesn't use up the 6-minute slot
                done = (parser.finished, fangs.finished)
                if sum(done) > 0 and done != seen_finished:
                    seen_finished, pending = done, True
                    if not (dry or once) and wait > 0:
                        print(f'[{stamp()}] Pull ended; uploading in {int(wait // 60)} min {int(wait % 60)} s '
                              '(at most one upload every 6 minutes, to stay under GitHub Pages\' build limit).')
                if pending and (dry or once or wait <= 0):
                    if send():
                        pending, last_upload = False, time.time()
                    else:
                        last_upload = time.time() - MIN_UPLOAD_GAP + 30   # retry in 30 seconds
            if once: break
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        if pending and fh:
            print(f'\n[{stamp()}] Sending the last update before stopping...')
            send()
        raise

if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: print('\nStopped.')
