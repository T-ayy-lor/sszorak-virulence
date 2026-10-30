#!/usr/bin/env python3
"""
Raid uploader: watches your WoW combat log and, after every pull or wipe, pushes the night's data
to your GitHub Pages repository. The pages pick it up on their own.
  Sszorak: positions, Virulence casts, explosions, damage, Tempest -> nights/<log name>.json, listed in nights.json
  Twin Fangs: positions, Eternal Venom, globules, Coiling Ichor, kicks, Ravenous Feast soaks, damage, healing, deaths
              -> fangs-nights/<log name>.json, listed in fangs.json
"""
import base64, codecs, csv, getpass, glob, json, os, re, select, ssl, sys, time, urllib.error, urllib.request
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
SETUP_PATH = 'fangs-setup.txt'                 # Twin Fangs kick order and soak groups, read by the page
WOW_DIR = os.path.dirname(LOG_DIR)             # .../_retail_; Northern Sky Raid Tools saves its notes under WTF/Account/*/SavedVariables
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
    """Mythic The Twin Fangs (Vexhul and Ithraz). Any boss encounter with those NPCs in it is kept; everything the
    Twin Fangs page needs is recorded: positions, Eternal Venom stacks and what caused them, Caustic Globules
    (Caustic Deluge sets, soakers, CC, where they ruptured), Coiling Ichor drops, Visceral Burst kicks,
    Ravenous Feast / Tainted Blood hits, damage, healing and deaths."""
    BOSSES = {'257361': 'Vexhul', '257368': 'Ithraz'}
    VENOM, GLOBULE, DELUGE = '1290336', '1289993', '1289192'
    EMERGENCE, SPIT, ICHOR = '1291404', '1291478', '1290809'
    BULWARK, ROUSE, BURST = '1303378', '1308356', '1308385'
    FEAST, TAINTED = '1290516', '1310099'
    OK_VENOM = {GLOBULE: 'Caustic Globule', EMERGENCE: 'Venomous Emergence', SPIT: 'Corrosive Spit'}
    # CC auras (cast ID / applied aura ID where they differ) from the raid's classes
    CC = {'221562': 'Asphyxiate', '207167': 'Blinding Sleet', '46968': 'Shockwave', '132168': 'Shockwave', '107570': 'Storm Bolt',
          '132169': 'Storm Bolt', '5246': 'Intimidating Shout', '179057': 'Chaos Nova', '1234195': 'Void Nova', '217832': 'Imprison',
          '207684': 'Sigil of Misery', '207685': 'Sigil of Misery', '211881': 'Fel Eruption', '119381': 'Leg Sweep', '115078': 'Paralysis',
          '853': 'Hammer of Justice', '115750': 'Blinding Light', '105421': 'Blinding Light', '20066': 'Repentance', '1833': 'Cheap Shot',
          '408': 'Kidney Shot', '1776': 'Gouge', '2094': 'Blind', '6770': 'Sap', '360806': 'Sleep Walk', '8122': 'Psychic Scream',
          '64044': 'Psychic Horror', '192058': 'Capacitor Totem', '118905': 'Capacitor Totem', '51514': 'Hex', '5211': 'Mighty Bash',
          '99': 'Incapacitating Roar', '33786': 'Cyclone', '109248': 'Binding Shot', '117526': 'Binding Shot', '19577': 'Intimidation',
          '24394': 'Intimidation', '187650': 'Freezing Trap', '3355': 'Freezing Trap', '213691': 'Scatter Shot', '31661': "Dragon's Breath",
          '118': 'Polymorph', '113724': 'Ring of Frost', '82691': 'Ring of Frost', '30283': 'Shadowfury', '5484': 'Howl of Terror',
          '5782': 'Fear', '118699': 'Fear', '6789': 'Mortal Coil', '89766': 'Axe Toss'}

    def __init__(self):
        self.pulls, self.cur, self.leftover = [], None, ''
        self.names, self.specs, self.pets = {}, {}, {}
        self.finished = 0          # count of kept pulls that have ended

    def has_data(self): return bool(self.pulls) or bool(self.cur and self.cur['twin'])

    def feed(self, text):
        text = self.leftover + text
        end = text.rfind('\n')
        if end < 0:
            self.leftover = text; return
        pos = 0
        while pos < end:
            if self.cur is None:                 # skip to the next boss pull that isn't Sszorak
                m = text.find('ENCOUNTER_START,', pos)
                if m < 0 or m >= end: break
                pos = text.rfind('\n', 0, m) + 1
            nl = text.find('\n', pos)
            if nl < 0 or nl > end: nl = end
            try: self.line(text[pos:nl])
            except (IndexError, ValueError): pass
            pos = nl + 1
        self.leftover = text[end + 1:]

    def start(self, l, rest):
        r = Parser.split(rest)
        if r[1] == ENC: self.cur = None; return              # Sszorak has its own parser
        when = l.split(' ', 1)
        self.cur = dict(enc=r[1], encName=r[2], t0=Parser.ts(l), clock=when[1][:8].split('.')[0], date=when[0], diff=int(r[3]),
                        twin='twin fangs' in r[2].lower(), dur=0.0, kill=False, ended=False, lastT=0.0,
                        track={}, ctrack={}, specGuid={}, died=[], dmg={}, heal={}, hp={},
                        hits={}, stacks={}, venom=[], deluges=[], rouses=[], glob={}, globHits=[], cc=[], bulwark=set(),
                        ichor={}, ichorOut=[], casts=[], open={}, feasts=[], feastHits=[], tainted=[])

    def close(self, r):
        c = self.cur
        if r: c['kill'], c['dur'] = r[5] == '1', int(r[6]) / 1000
        else: c['dur'] = c['lastT']
        c['ended'] = True
        if c['twin']:
            self.pulls.append(c); self.finished += 1
        self.cur = None

    def owner(self, g):
        return g if g.startswith('Player-') else self.pets.get(g)

    def line(self, l):
        l = l.rstrip('\r')
        sep = l.find('  ')
        if sep < 0: return
        rest = l[sep + 2:]
        ev = rest[:rest.find(',')]
        if ev == 'ENCOUNTER_START':
            if self.cur: self.close(None)
            self.start(l, rest); return
        c = self.cur
        if c is None: return
        t = Parser.ts(l) - c['t0']; c['lastT'] = t
        if ev == 'ENCOUNTER_END': self.close(rest.split(',')); return
        if ev == 'COMBATANT_INFO':
            r = rest.split(',', 27)
            try: c['specGuid'][r[1]] = int(r[25])
            except (ValueError, IndexError): pass
            return
        r = Parser.split(rest)
        if ev == 'UNIT_DIED':
            if len(r) > 5 and r[5].startswith('Player-'): c['died'].append((t, r[5]))
            return
        if not (ev.startswith('SPELL_') or ev.startswith('SWING_') or ev.startswith('RANGE_')): return
        src, dst = r[1], r[5]
        if src.startswith('Player-'): self.names[src] = r[2].split('-')[0]
        if dst.startswith('Player-'): self.names[dst] = r[6].split('-')[0]
        if not c['twin'] and any(('-' + k + '-') in src or ('-' + k + '-') in dst for k in self.BOSSES): c['twin'] = True
        swing = ev.startswith('SWING_')
        sid = '' if swing else r[9]
        if ev == 'SPELL_SUMMON':
            o = self.owner(src)
            if o: self.pets[dst] = o
            return
        # positions from the advanced-logging block
        k = 9 if swing else 12
        if len(r) > k + 17 and '-' in r[k]:
            info = r[k]
            own = r[k + 1]
            if own and own.startswith('Player-') and not info.startswith('Player-'): self.pets[info] = own
            try:
                x, y = float(r[k + 14]), float(r[k + 15])
                if info.startswith('Player-'): Parser.add_pt(c['track'].setdefault(info, []), t, x, y)
                else:
                    tr = c['ctrack'].setdefault(info, [])
                    if not tr or t - tr[-1][0] >= 0.25: tr.append((t, x, y))
                    for npc, nm in self.BOSSES.items():
                        if '-' + npc + '-' in info:
                            hp, mx = float(r[k + 2]), float(r[k + 3])
                            if mx > 0: c['hp'][nm] = hp / mx
            except ValueError: pass
        # ---- auras
        if ev.startswith('SPELL_AURA'):
            if sid == self.VENOM and dst.startswith('Player-'):
                prev = c['stacks'].get(dst, 0)
                if ev == 'SPELL_AURA_APPLIED': n = 1
                elif ev == 'SPELL_AURA_APPLIED_DOSE': n = int(r[-1]) if r[-1].isdigit() else prev + 1
                elif ev == 'SPELL_AURA_REMOVED_DOSE': n = int(r[-1]) if r[-1].isdigit() else max(prev - 1, 0)
                elif ev == 'SPELL_AURA_REMOVED': n = 0
                else: return
                c['stacks'][dst] = n
                if n > prev:
                    c['venom'].append({'t': t, 'g': dst, 'n': n, 'auraSrc': r[2]})
            elif sid == self.ICHOR and dst.startswith('Player-'):
                if ev == 'SPELL_AURA_APPLIED': c['ichor'][dst] = t
                elif ev == 'SPELL_AURA_REMOVED':
                    on = c['ichor'].pop(dst, None); c['ichorOut'].append({'g': dst, 'on': on, 'off': t})
            elif sid == self.BULWARK and not dst.startswith('Player-'):
                c['bulwark'].add(dst)
            elif ev == 'SPELL_AURA_APPLIED' and sid in self.CC and not dst.startswith('Player-'):
                o = self.owner(src)
                if o: c['cc'].append((t, o, dst, self.CC[sid]))
            return
        # ---- casts
        if ev == 'SPELL_CAST_SUCCESS' or ev == 'SPELL_CAST_START':
            if sid == self.DELUGE and ev == 'SPELL_CAST_SUCCESS': c['deluges'].append(t)
            elif sid == self.ROUSE and ev == 'SPELL_CAST_SUCCESS': c['rouses'].append(t)
            elif sid == self.FEAST and ev == 'SPELL_CAST_START': c['feasts'].append(t)
            elif sid == self.GLOBULE and not src.startswith('Player-'): c['glob'].setdefault(src, {'first': t})
            if sid == self.BURST and ev == 'SPELL_CAST_START':
                cast = {'t': t, 'src': src, 'kick': None, 'res': None}
                c['casts'].append(cast); c['open'][src] = cast
            elif sid == self.BURST and ev == 'SPELL_CAST_SUCCESS':
                cast = c['open'].pop(src, None)
                if cast: cast['res'] = 'completed'
            return
        if ev == 'SPELL_CAST_FAILED' and sid == self.BURST:
            cast = c['open'].pop(src, None)
            if cast: cast['res'] = 'stopped'
            return
        if ev == 'SPELL_INTERRUPT' and self.BURST in (r[12] if len(r) > 12 else '', r[29] if len(r) > 29 else ''):
            cast = c['open'].pop(dst, None)
            if cast:
                o = self.owner(src) or src
                cast['kick'] = {'g': o, 'name': r[2].split('-')[0], 'pet': not src.startswith('Player-'), 'spell': r[10], 'd': t - cast['t']}
                cast['res'] = 'kicked'
            return
        # ---- damage and healing
        if ev.endswith('_DAMAGE') or ev.endswith('_MISSED'):
            if dst.startswith('Player-') and not src.startswith('Player-'):
                c['hits'].setdefault(dst, []).append((t, sid, 'Melee' if swing else r[10]))
                if sid == self.GLOBULE: c['globHits'].append((t, src, dst))
                elif sid == self.FEAST: c['feastHits'].append((t, dst))
                elif sid == self.TAINTED: c['tainted'].append((t, dst))
            if ev.endswith('_DAMAGE'):
                try: val = float(r[k + 19]) - max(float(r[k + 21]), 0) + max(float(r[k + 25]), 0)
                except (ValueError, IndexError): val = 0.0
            elif len(r) > k + 2 and r[k] == 'ABSORB':
                try: val = float(r[k + 2])
                except ValueError: val = 0.0
            else: val = 0.0
            if val > 0:
                try: hostile_dst = not (int(r[7], 16) & 0x10)
                except ValueError: hostile_dst = False
                o = self.owner(src)
                if hostile_dst and o: c['dmg'][o] = c['dmg'].get(o, 0) + val
            return
        if ev in ('SPELL_HEAL', 'SPELL_PERIODIC_HEAL'):
            try: val = float(r[k + 19]) - max(float(r[k + 21]), 0)
            except (ValueError, IndexError): val = 0.0
            o = self.owner(src)
            if o and val > 0: c['heal'][o] = c['heal'].get(o, 0) + val
            return
        if ev == 'SPELL_ABSORBED':           # shields: credited to whoever cast the shield
            a = 9 if (r[9].startswith('Player-') or r[9].startswith('Creature-') or r[9].startswith('Pet-')) else 12
            try:
                o = self.owner(r[a]); val = float(r[a + 7])
                if o and val > 0: c['heal'][o] = c['heal'].get(o, 0) + val
            except (ValueError, IndexError): pass

    def build(self, log_name):
        nm = lambda g: self.names.get(g, g)
        pulls = self.pulls + ([self.cur] if self.cur and self.cur['twin'] else [])
        for p in pulls:
            for g, sp in p['specGuid'].items():
                if g in self.names: self.specs[self.names[g]] = sp
        out = []
        for n, p in enumerate(pulls, 1):
            stop = p['dur'] if p['ended'] else p['lastT']
            pos = lambda g, t: Parser.pos_at(p['track'].get(g, []), t)
            def cpos(g, t):
                tr = p['ctrack'].get(g) or []
                prior = [q for q in tr if q[0] <= t + 0.5]
                q = prior[-1] if prior else (tr[0] if tr else None)
                return [round(q[1], 2), round(q[2], 2)] if q else None
            setno = lambda t: sum(1 for d in p['deluges'] if d <= t + 0.5)
            # Eternal Venom stacks and what caused each one
            venom = []
            for v in p['venom']:
                recent = [h for h in p['hits'].get(v['g'], []) if -0.3 <= v['t'] - h[0] <= 1.0]
                okh = [h for h in recent if h[1] in self.OK_VENOM]
                near = lambda hs: min(hs, key=lambda h: abs(v['t'] - h[0]))
                if okh: src, ok = self.OK_VENOM[near(okh)[1]], True
                elif recent: src, ok = near(recent)[2], False
                else: src, ok = (v['auraSrc'] if v['auraSrc'] not in ('nil', '') else 'Unknown'), False
                venom.append({'t': round(v['t'], 2), 'n': nm(v['g']), 'st': v['n'], 'src': src, 'ok': ok, 'pos': pos(v['g'], v['t'])})
            # Caustic Globules: each globule's burst, who soaked it, where it ruptured, who CC'd it
            bursts = []                       # one burst per globule rupture: its damage hits arrive within a few seconds
            for t, src, dst in sorted(p['globHits']):
                b = next((b for b in reversed(bursts) if b['src'] == src and t - b['t'] <= 3.0), None)
                if not b:
                    b = {'t': t, 'src': src, 'soak': set()}; bursts.append(b)
                b['soak'].add(dst)
            boss = lambda g: any('-' + k + '-' in g for k in self.BOSSES)
            globules, seen = [], set()
            for i, b in enumerate(bursts):
                g = b['src']
                spots = [q for q in (pos(s, b['t']) for s in b['soak']) if q]
                at = None if boss(g) else cpos(g, b['t'])
                if not at and spots: at = [round(sum(q[0] for q in spots) / len(spots), 2), round(sum(q[1] for q in spots) / len(spots), 2)]
                born = p['glob'].get(g, {}).get('first')
                if born is None and p['ctrack'].get(g) and not boss(g): born = p['ctrack'][g][0][0]
                cc = [] if boss(g) else [[round(t, 2), nm(o), sp] for t, o, d, sp in p['cc'] if d == g]
                seen.add(g)
                globules.append({'id': g.rsplit('-', 1)[-1] + ('' if not boss(g) else f'#{i}'), 'set': setno(born if born is not None else b['t']),
                                 'rupt': round(b['t'], 2), 'pos': at, 'soak': sorted(nm(s) for s in b['soak']), 'cc': cc})
            for g in (set(p['bulwark']) | set(p['glob']) | {d for _, _, d, _ in p['cc'] if d in p['bulwark'] or d in p['glob']}) - seen:
                born = p['glob'].get(g, {}).get('first') or (p['ctrack'][g][0][0] if p['ctrack'].get(g) else 0)
                globules.append({'id': g.rsplit('-', 1)[-1], 'set': setno(born), 'rupt': None, 'pos': None, 'soak': [],
                                 'cc': [[round(t, 2), nm(o), sp] for t, o, d, sp in p['cc'] if d == g]})
            globules.sort(key=lambda x: (x['rupt'] is None, x['rupt'] or 0))
            # Coiling Ichor: where each player stood when it ran out
            died = {}
            for t, g in p['died']: died.setdefault(g, t)
            ichor = []
            for d in p['ichorOut'] + [{'g': g, 'on': on, 'off': None} for g, on in p['ichor'].items()]:
                off = d['off'] if d['off'] is not None else stop
                dt = died.get(d['g'])
                end = 'open' if d['off'] is None else 'died' if dt is not None and abs(dt - off) <= 0.6 else 'expired'
                ichor.append({'n': nm(d['g']), 'on': round(d['on'], 2) if d['on'] is not None else None, 'off': round(off, 2), 'end': end, 'pos': pos(d['g'], off)})
            # Visceral Burst casts (Broodlings of Ithraz) and kicks
            brood = [{'t': round(x['t'], 2), 'src': x['src'].rsplit('-', 1)[-1], 'pos': cpos(x['src'], x['t']), 'res': x['res'],
                      'kick': dict(by=nm(x['kick']['g']), pet=x['kick']['pet'], spell=x['kick']['spell'], d=round(x['kick']['d'], 3)) if x['kick'] else None}
                     for x in p['casts']]
            # Ravenous Feast: up to 3 slams per cast (a slam's hits land together, so hits under 0.8 s apart are one slam),
            # plus every Tainted Blood hit until the next Ravenous Feast; the page checks each soaker got one after their slam
            slams = []
            for t, g in sorted(p['feastHits']):
                if not slams or t - slams[-1]['t1'] > 0.8: slams.append({'t': t, 't1': t, 'hit': set()})
                slams[-1]['t1'] = t; slams[-1]['hit'].add(g)
            feasts = []
            starts = sorted(p['feasts'])
            for s_ in slams:
                f = max((i for i, ft in enumerate(starts) if ft <= s_['t'] + 0.5), default=None)
                key = f if f is not None else -1
                if not feasts or feasts[-1]['key'] != key or len(feasts[-1]['slams']) >= 3:
                    feasts.append({'key': key, 't': starts[f] if f is not None else s_['t'], 'slams': []})
                feasts[-1]['slams'].append({'t': round(s_['t'], 2), 'hit': sorted(nm(g) for g in s_['hit'])})
            for k, f in enumerate(feasts):
                end = feasts[k + 1]['t'] if k + 1 < len(feasts) else f['t'] + 60
                f['tb'] = [[round(t, 2), nm(g)] for t, g in sorted(p['tainted']) if f['t'] <= t < end]
                f['t'] = round(f['t'], 2); f.pop('key')
            # deaths: what hit them last, and when they were back up (a battle rez)
            deaths = []
            for t, g in sorted(p['died']):
                last = [h for h in p['hits'].get(g, []) if t - 5 <= h[0] <= t + 0.2]
                back = next((q[0] for q in p['track'].get(g, []) if q[0] > t + 1.0), None)
                deaths.append([round(t, 2), nm(g), last[-1][2] if last else None, round(back, 2) if back is not None else None])
            # where the bosses stood and the middle of the raid (medians), for the kick quadrants
            med = lambda a: sorted(a)[len(a) // 2] if a else None
            bpos = {}
            for npc, bn in self.BOSSES.items():
                pts = [q for g, tr in p['ctrack'].items() if '-' + npc + '-' in g for q in tr]
                if pts: bpos[bn] = [round(med([q[1] for q in pts]), 2), round(med([q[2] for q in pts]), 2)]
            allp = [q for tr in p['track'].values() for q in tr]
            rc = [round(med([q[1] for q in allp]), 2), round(med([q[2] for q in allp]), 2)] if allp else None
            stacks_max = {}
            for v in p['venom']: stacks_max[nm(v['g'])] = max(stacks_max.get(nm(v['g']), 0), v['n'])
            out.append({'n': n, 'enc': p['enc'], 'encName': p['encName'], 'clock': p['clock'], 'date': p['date'], 'diff': p['diff'],
                        'dur': round(stop, 1), 'kill': p['kill'], 'live': not p['ended'], 'hp': {k: round(v, 4) for k, v in p['hp'].items()},
                        'deaths': deaths, 'bpos': bpos, 'rc': rc,
                        'deluges': [round(t, 2) for t in p['deluges']], 'rouses': [round(t, 2) for t in p['rouses']],
                        'venom': venom, 'stacks': stacks_max, 'globules': globules, 'ichor': ichor, 'brood': brood, 'feasts': feasts,
                        'dmg': {nm(g): round(v) for g, v in p['dmg'].items()}, 'heal': {nm(g): round(v) for g, v in p['heal'].items()}})
        return {'log': log_name, 'updated': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'pulls': out, 'specs': dict(self.specs)}


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
    # lowest boss health on a wipe (Twin Fangs pulls carry one health per boss: use the higher of the two)
    hps = [max(p['hp'].values()) if isinstance(p.get('hp'), dict) and p['hp'] else p.get('hp') for p in pulls if not p['kill']]
    hps = [h for h in hps if isinstance(h, (int, float))]
    return {'file': night_file, 'log': data['log'], 'date': first.get('date'), 'diff': first.get('diff'),
            'pulls': len(pulls), 'kill': any(p['kill'] for p in pulls), 'best': min(hps) if hps else None,
            'updated': data['updated']}

def merge_index(idx, entry):
    idx = idx if isinstance(idx, dict) and 'nights' in idx else {'nights': []}
    idx['nights'] = [e for e in idx['nights'] if e.get('file') != entry['file']] + [entry]
    idx['nights'].sort(key=lambda e: (date_key(e.get('date')), e.get('log', '')))
    return idx


# ---- "notechange": copy the kick order and soak groups from the Northern Sky Raid Tools note into fangs-setup.txt ----
def nsrt_strings(path):
    """Every string value in a SavedVariables file, as (table path, key, text). WoW writes these files on /reload or logout."""
    b = open(path, 'rb').read()
    out, stack, key, i, n = [], [], None, 0, len(b)
    var = re.compile(rb'([A-Za-z_]\w*)\s*=\s*'); bkey = re.compile(rb'\[\s*"((?:[^"\\]|\\.)*)"\s*\]\s*=\s*'); nkey = re.compile(rb'\[\s*([^\]"]+?)\s*\]\s*=\s*')   # [1], [1.5], [true]
    esc = {ord('n'): b'\n', ord('t'): b'\t', ord('r'): b'\r', ord('a'): b'\a', ord('b'): b'\b', ord('f'): b'\f', ord('v'): b'\v'}
    while i < n:
        c = b[i]
        if c in b' \t\r\n,;': i += 1; continue
        if b.startswith(b'--', i):
            j = b.find(b'\n', i); i = n if j < 0 else j; continue
        m = bkey.match(b, i) or nkey.match(b, i) or (var.match(b, i) if not stack else None)
        if m: key = m.group(1).decode('utf-8', 'replace'); i = m.end(); continue
        if c == ord('{'): stack.append(key); key = None; i += 1; continue
        if c == ord('}'):
            if stack: stack.pop()
            key = None; i += 1; continue
        if c == ord('"'):
            j, buf = i + 1, bytearray()
            while j < n and b[j] != ord('"'):
                if b[j] == ord('\\') and j + 1 < n:
                    d = b[j + 1]
                    if 48 <= d <= 57:
                        k = j + 1
                        while k < n and k < j + 4 and 48 <= b[k] <= 57: k += 1
                        buf.append(int(b[j + 1:k]) & 255); j = k; continue
                    buf += esc.get(d, bytes([d])); j += 2; continue
                buf.append(b[j]); j += 1
            out.append((tuple(stack), key, buf.decode('utf-8', 'replace'))); key = None; i = j + 1; continue
        while i < n and b[i] not in b',}\n': i += 1          # numbers, true, false, nil
        key = None
    return out

def find_fangs_note(sv_override=None):
    files = [sv_override] if sv_override else glob.glob(os.path.join(WOW_DIR, 'WTF', 'Account', '*', 'SavedVariables', 'NorthernSkyRaidTools.lua'))
    files = [f for f in files if f and os.path.exists(f)]
    if not files: return None, None, 'Could not find NorthernSkyRaidTools.lua under ' + os.path.join(WOW_DIR, 'WTF', 'Account')
    path = max(files, key=os.path.getmtime)
    strings = nsrt_strings(path)
    twin = lambda t: 'EncounterID:3421' in t or re.search(r'(?i)melee kick|soak (first|second|third)', t)
    # the note the raid leader shared, then any saved note for the Twin Fangs
    picks = [t for p, k, t in strings if p == ('NSRT',) and k == 'StoredSharedReminder' and twin(t)] + \
            [t for p, k, t in strings if p == ('NSRT', 'Reminders') and twin(t)]
    if not picks: return path, None, 'No Twin Fangs note in ' + path + ' (in WoW, type /reload after the note changes so WoW saves it)'
    return path, picks[0], None

QKEYS = [('melee left', 'Melee left'), ('front melee', 'Front melee'), ('melee right', 'Melee right'), ('ranged left', 'Ranged left'), ('ranged right', 'Ranged right')]
NOT_NAMES = {'everyone', 'melee', 'ranged', 'tank', 'healer', 'damager', 'dps'}

def parse_fangs_note(note):
    """Kick order per quadrant, backup kickers and soak groups from the note's reminder lines."""
    kick, backup, groups, warn = {}, [], {}, []
    first = lambda m: m.group(1).strip() if m else None
    for line in note.split('\n'):
        tag, text = first(re.search(r'tag:([^;]+)', line)), first(re.search(r'text:([^;]+)', line))
        if not tag or not text: continue
        tm = re.search(r'time:(-?[\d.]+)', line); t = float(tm.group(1)) if tm else 0.0
        names = [x for x in tag.split() if x.lower() not in NOT_NAMES and not re.fullmatch(r'(?i)group\d|\d+', x)]
        if not names: continue
        m = re.match(r'(?i)(left|right|front)\s+(melee|range|ranged)\s+kick\s*(\d+)', text)
        if m:
            side, kind = m.group(1).lower(), 'melee' if m.group(2).lower() == 'melee' else 'ranged'
            q = 'front melee' if side == 'front' else f'{kind} {side}'
            slot, prev = int(m.group(3)), kick.setdefault(q, {}).get(int(m.group(3)))
            if prev is None or t < prev[0]:
                if prev and prev[1] != names[0]: warn.append(f'{q} kick {slot}: {names[0]} at {t:g}s, {prev[1]} later; using {names[0]}')
                kick[q][slot] = (t, names[0])
            elif prev[1] != names[0]: warn.append(f'{q} kick {slot}: {prev[1]} first, {names[0]} at {t:g}s; using {prev[1]}')
            continue
        if re.match(r'(?i)backup\s+kick', text):
            backup += [x for x in names if x not in backup]; continue
        m = re.match(r'(?i)soak\s+(first|second|third|1|2|3)\b', text)
        if m:
            g = {'first': 1, 'second': 2, 'third': 3}.get(m.group(1).lower()) or int(m.group(1))
            for x in names:
                if x in groups and groups[x] != g: warn.append(f'{x} is in soak group {groups[x]} and {g}; using {groups[x]}')
                else: groups[x] = g
    out = {q: [n for _, (_, n) in sorted(kick[q].items())] for q in kick}
    if backup: out['backup'] = backup
    for g in (1, 2, 3):
        ns = [x for x, v in groups.items() if v == g]
        if ns: out[f'group {g}'] = ns
    return out, warn

def read_setup(text):
    """key -> names from fangs-setup.txt (same keys the page accepts)."""
    alias = {'meleeleft': 'melee left', 'leftmelee': 'melee left', 'ml': 'melee left', 'meleeright': 'melee right', 'rightmelee': 'melee right', 'mr': 'melee right',
             'frontmelee': 'front melee', 'meleefront': 'front melee', 'front': 'front melee', 'fm': 'front melee',
             'rangedleft': 'ranged left', 'leftranged': 'ranged left', 'leftrange': 'ranged left', 'rangeleft': 'ranged left', 'rl': 'ranged left',
             'rangedright': 'ranged right', 'rightranged': 'ranged right', 'rightrange': 'ranged right', 'rangeright': 'ranged right', 'rr': 'ranged right',
             'backup': 'backup', 'backupkicker': 'backup', 'backups': 'backup', 'group1': 'group 1', 'g1': 'group 1', 'group2': 'group 2', 'g2': 'group 2', 'group3': 'group 3', 'g3': 'group 3'}
    out = {}
    for raw in (text or '').split('\n'):
        line = raw.split('#', 1)[0].strip()
        if ':' not in line: continue
        k, v = line.split(':', 1)
        k = alias.get(re.sub(r'\s+', '', k.lower()))
        if k: out[k] = [x.strip() for x in v.split(',') if x.strip()]
    return out

def write_setup(cfg):
    lines = ['# Twin Fangs setup. Save, then commit and push in GitHub Desktop, or type "notechange" in the uploader window',
             '# to copy it from the Northern Sky Raid Tools note (type /reload in WoW first so the note is saved).',
             "# Names are separated by commas. Capitals and accents don't matter (Shenron = Shènròn).",
             '# Anything after a # is ignored.', '',
             '# Rouse the Brood kick order: casts take turns down the list (1st cast = 1st name, 2nd = 2nd...). Dead players are skipped.',
             "# Front melee is the tank side, past the bosses. Backup kicks on anyone's turn without counting as out of turn."]
    lines += [f'{k}: {", ".join(cfg.get(k, []))}' for k, _ in QKEYS] + [f'backup: {", ".join(cfg.get("backup", []))}', '',
              '# Ravenous Feast soak groups: group 1 soaks first, group 2 second, group 3 third.']
    lines += [f'group {g}: {", ".join(cfg.get(f"group {g}", []))}' for g in (1, 2, 3)]
    return '\n'.join(lines) + '\n'

def note_change(up, dry, out_dir, sv_override=None):
    """Read the note, merge it into fangs-setup.txt, and publish it (or write it to out_dir with --dry-run)."""
    path, note, err = find_fangs_note(sv_override)
    if err: print(f'[{stamp()}] {err}'); return
    found, warn = parse_fangs_note(note)
    if not found: print(f'[{stamp()}] The Twin Fangs note has no kick or soak lines to copy.'); return
    when = datetime.fromtimestamp(os.path.getmtime(path)).strftime('%I:%M %p').lstrip('0')
    print(f'[{stamp()}] Read the Twin Fangs note (WoW saved it at {when}; type /reload in WoW first if it changed after that).')
    for w in warn: print(f'           note: {w}')
    if dry:
        cur_path = os.path.join(out_dir, SETUP_PATH)
        cur_text = open(cur_path, encoding='utf-8').read() if os.path.exists(cur_path) else ''
        sha = None
    else:
        try:
            cur = up.api('GET', f'contents/{SETUP_PATH}?ref={BRANCH}'); sha = cur['sha']
            cur_text = base64.b64decode(cur['content']).decode('utf-8')
        except urllib.error.HTTPError as e:
            if e.code != 404: print(f'[{stamp()}] Could not read {SETUP_PATH} from GitHub: {e}'); return
            cur_text, sha = '', None
    old = read_setup(cur_text)
    new = dict(old); new.update(found)                     # anything the note doesn't mention stays as it was
    changes = [(k, old.get(k, []), new.get(k, [])) for k in [q for q, _ in QKEYS] + ['backup', 'group 1', 'group 2', 'group 3'] if old.get(k, []) != new.get(k, [])]
    if not changes: print(f'[{stamp()}] {SETUP_PATH} already matches the note. Nothing to change.'); return
    for k, a, b in changes:
        print(f'           {k}: {", ".join(a) or "—"}  ->  {", ".join(b) or "—"}')
    text = write_setup(new)
    if dry:
        with open(os.path.join(out_dir, SETUP_PATH), 'w', encoding='utf-8') as f: f.write(text)
        print(f'[{stamp()}] Wrote {SETUP_PATH} in {out_dir}.'); return
    body = {'message': 'Twin Fangs setup from the raid note', 'content': base64.b64encode(text.encode('utf-8')).decode(), 'branch': BRANCH}
    if sha: body['sha'] = sha
    try:
        up.api('PUT', f'contents/{SETUP_PATH}', body)
        print(f'[{stamp()}] Saved {SETUP_PATH} to GitHub. The page shows it in a minute or two; in GitHub Desktop, Fetch then Pull to get it locally.')
    except Exception as e:
        print(f'[{stamp()}] Could not save {SETUP_PATH} to GitHub: {e}')


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
    sv_override = sys.argv[sys.argv.index('--sv') + 1] if '--sv' in sys.argv else None
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

    if '--notechange' in sys.argv:
        note_change(up, dry, out_dir, sv_override); return
    path, fh, parser, fangs, seen_finished, pending, last_upload = None, None, None, None, None, False, 0.0

    dec, pos = None, 0
    print(f'[{stamp()}] Watching {LOG_DIR} for Sszorak and Twin Fangs pulls. Press Control-C to stop.')
    print(f'[{stamp()}] Type "push" and press Return to upload right now instead of waiting.')
    print(f'[{stamp()}] Type "notechange" and press Return to copy kicks and soak groups from the raid note into {SETUP_PATH}.')


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
            cmd = ''
            if sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
                cmd = sys.stdin.readline().strip().lower()
                if cmd == 'notechange':
                    note_change(up, dry, out_dir, sv_override); cmd = ''
                elif cmd and cmd != 'push':
                    print(f'[{stamp()}] Commands: "push" uploads now, "notechange" copies the raid note into {SETUP_PATH}.'); cmd = ''
                elif cmd == 'push' and not fh:
                    print(f'[{stamp()}] Nothing to upload yet: no combat log found.'); cmd = ''
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
                    nf = len(fangs.pulls) + (1 if fangs.cur and fangs.cur['twin'] else 0)
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
                # typed in this window: "push" uploads now, skipping the 6-minute wait
                push = False
                if cmd == 'push':
                    if parser.pulls or fangs.has_data(): push = True
                    else: print(f'[{stamp()}] Nothing to upload yet: no Sszorak or Twin Fangs pulls in this log.')
                if push or (pending and (dry or once or wait <= 0)):
                    if push: print(f'[{stamp()}] Uploading now (push).')
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
