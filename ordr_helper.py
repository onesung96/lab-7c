"""원랜디 자동 카운터.

워크3 메모리를 '읽기만' 해서 내 유닛 수를 세고, http://127.0.0.1:8765 전용 페이지에 보여준다.
조합식·유닛 수치는 sions 조합 도우미 글에서 받아온다(sions_cache.json 에 캐시).
새 맵 버전이 보이면 mapping.json 을 스스로 다시 만든다(mapsync.py).

게임이 멈추지 않도록: 1MB씩 나눠 읽고 중간중간 쉰다. 첫 스캔만 20초~1분, 이후엔 2초마다 조금씩.

옵션: --lan (폰에서 보기)  --no-browser  --port=NNNN  --overlay-only (게임 위 작은 창만 띄움)
"""
import ctypes, ctypes.wintypes as W, json, os, struct, subprocess, sys, threading, time, traceback, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.3.0'
REPO = 'onesung96/lab-7c'   # 새 버전 확인용 깃허브 저장소
FROZEN = getattr(sys, 'frozen', False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))   # config / cache / 기록 (쓰기 가능)
RES = getattr(sys, '_MEIPASS', HERE)                                             # 같이 묶인 페이지 + 기본 mapping
PORT = next((int(a.split('=')[1]) for a in sys.argv if a.startswith('--port=')), 8765)
if sys.stdout is None or sys.stderr is None:   # 검은 창 없는 exe: 로그는 파일로
    sys.stdout = sys.stderr = open(os.path.join(HERE, 'log.txt'), 'a', encoding='utf-8', buffering=1)
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(errors='replace')
    except Exception: pass
EXE = 'Warcraft III.exe'
VT_RVA = 0x2792E78        # CUnit vtable (RTTI .?AVCUnit@@), Reforged 3.0.0.24268 — 못 찾을 때만 쓰는 예비값
OFF_TYPE, OFF_OWNER, OFF_GONE = 0x70, 0x58, 0x274   # typeId, owner slot, 1 = 제거됨(조합 재료로 사라짐)
SZ = 0x400             # 객체에서 읽는 길이 (패치로 필드가 밀려도 넉넉하게)
CHUNK = 1 << 20           # ponytail: 1MB reads + 10ms nap every 16MB; tune if the game hitches
NAP_EVERY, NAP = 16, 0.01
TICK = 0.5                # 유닛 구역(1~2MB)은 0.5초마다 다시 읽는다
SMALL = 1 << 20           # 유닛은 64KB~1MB 짜리 작은 구역에 생긴다
SMALL_MB, BIG_MB = 24, 8  # 틱마다 추가로 훑는 양: 유닛 없던 작은 구역(빨리 한 바퀴) / 큰 구역(천천히)
PIECE = 8 << 20           # 큰 구역은 이만큼씩 잘라서 훑는다
# 위습 종류: sions 도우미엔 없어서 음수 id 로 따로 센다 (페이지가 이름을 붙임)
WISP_CODES = {'e018': -1, 'e017': -2, 'e016': -3, 'e019': -4, 'e01A': -5}

CFG_PATH = os.path.join(HERE, 'config.json')
SIONS_CACHE = os.path.join(HERE, 'sions_cache.json')
HIST_PATH = os.path.join(HERE, 'history.json')
cfg = {'owner': 1, 'auto': True, 'map_version': '2.323',
       'sions_url': 'https://sions.kr/bbs/board.php?bo_table=mk_helper&wr_id=13345'}   # mapping.json ids 는 이 글 기준
try:
    cfg.update(json.load(open(CFG_PATH, encoding='utf-8')))
except Exception:
    pass


def save_cfg():
    cfg.update(owner=state['owner'], auto=state['auto'])
    json.dump(cfg, open(CFG_PATH, 'w', encoding='utf-8'), ensure_ascii=False)


def res_or_local(name):   # 새 맵으로 다시 만든 파일이 있으면 그걸, 없으면 같이 묶인 기본 파일
    p = os.path.join(HERE, name)
    return p if os.path.exists(p) else os.path.join(RES, name)


MAPPING, CODES = {}, {}


def apply_mapping(mp):
    global MAPPING, CODES
    MAPPING = mp
    CODES = {struct.pack('<I', int.from_bytes(c.encode(), 'big')): sid for c, sid in {**mp, **WISP_CODES}.items()}
    state['managed'] = sorted(set(mp.values()))


state = {'status': '워크3 기다리는 중', 'counts': {}, 'players': {}, 'ts': 0, 'scan': 0,
         'owner': cfg['owner'], 'auto': cfg['auto'], 'managed': [], 'notice': '', 'data_ver': 1,
         'map_version': cfg['map_version'], 'version': VERSION, 'update': None, 'startup': False}
apply_mapping(json.load(open(res_or_local('mapping.json'), encoding='utf-8')))
SIONS = b'{}'
summary = {}           # 페이지가 계산해서 보내 주는 요약 (작은 창·판 기록용)
game = {'start': None, 'timeline': [], 'tl_ts': 0, 'sum_ts': 0}
last_hot_bases = set()   # 지난 판에 유닛이 있던 메모리 구역: 다음 판 첫 스캔에서 먼저 본다


def load_sions():
    import mapsync
    try:
        data = mapsync.sions_data(cfg['sions_url'])
        json.dump(data, open(SIONS_CACHE, 'w', encoding='utf-8'), ensure_ascii=False)
        return data
    except Exception as e:
        print('sions 조합식 받기 실패, 저장본 사용:', e)
        try:
            return json.load(open(res_or_local('sions_cache.json'), encoding='utf-8'))
        except Exception:   # 배포본엔 sions 데이터를 넣지 않는다 (sions 사용자들이 만든 자료) -> 인터넷 연결 필요
            state['notice'] = 'sions 조합 데이터를 못 받았어요. 인터넷 연결을 확인하고 프로그램을 다시 켜 주세요'
            return {'groups': []}


def check_map_update():
    """Maps/Download 에 더 새로운 원랜디 맵이 있으면 짝짓기 + 조합식을 새 버전으로 바꾼다."""
    global SIONS
    import mapsync
    try:
        ver, path = mapsync.latest_map()
        if not ver or ver == cfg['map_version']:
            return
        state['notice'] = f'새 원랜디 {ver} 발견 — 유닛 짝짓는 중…'
        url = mapsync.find_sions_post(ver)
        if not url:
            state['notice'] = f'원랜디 {ver}: sions에 이 버전 조합 글이 아직 없어서 {cfg["map_version"]} 데이터를 써요'
            return
        data = mapsync.sions_data(url)
        mp = mapsync.build_mapping(mapsync.map_units(path), data)
        if len(mp) < 100:
            raise ValueError(f'짝지은 유닛이 {len(mp)}개뿐')
        json.dump(mp, open(os.path.join(HERE, 'mapping.json'), 'w', encoding='utf-8'), ensure_ascii=False)
        json.dump(data, open(SIONS_CACHE, 'w', encoding='utf-8'), ensure_ascii=False)
        cfg.update(map_version=ver, sions_url=url); save_cfg()
        SIONS = json.dumps(data, ensure_ascii=False).encode()
        apply_mapping(mp)
        state.update(map_version=ver, data_ver=state['data_ver'] + 1, notice=f'원랜디 {ver} 반영 완료 (유닛 {len(mp)}개)')
        print(state['notice'])
    except Exception as e:
        state['notice'] = f'새 맵 반영 실패, 이전 데이터 사용: {e}'
        traceback.print_exc()


# ───────── 판 기록 ─────────
def humans(players):
    return {o: n for o, n in players.items() if o < 24 and o != 7}


def end_game():
    if game['start'] and summary.get('units'):
        try:
            hist = json.load(open(HIST_PATH, encoding='utf-8'))
        except Exception:
            hist = []
        hist.append({'start': time.strftime('%m-%d %H:%M', time.localtime(game['start'])),
                     'minutes': round((time.time() - game['start']) / 60), 'map': state['map_version'], **summary,
                     'timeline': game['timeline']})
        json.dump(hist[-50:], open(HIST_PATH, 'w', encoding='utf-8'), ensure_ascii=False)
    game.update(start=None, timeline=[], tl_ts=0)


# ───────── 메모리 (읽기 전용) ─────────
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
psapi = ctypes.WinDLL('psapi', use_last_error=True)


class MBI(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_ulonglong), ('AllocationBase', ctypes.c_ulonglong),
                ('AllocationProtect', W.DWORD), ('PartitionId', W.WORD), ('RegionSize', ctypes.c_ulonglong),
                ('State', W.DWORD), ('Protect', W.DWORD), ('Type', W.DWORD)]


k32.OpenProcess.restype = W.HANDLE
k32.ReadProcessMemory.argtypes = [W.HANDLE, ctypes.c_ulonglong, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualQueryEx.argtypes = [W.HANDLE, ctypes.c_ulonglong, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.CloseHandle.argtypes = [W.HANDLE]
k32.GetExitCodeProcess.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
psapi.EnumProcessModulesEx.argtypes = [W.HANDLE, ctypes.POINTER(ctypes.c_void_p), W.DWORD, ctypes.POINTER(W.DWORD), W.DWORD]


def find_pid():
    out = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {EXE}', '/FO', 'CSV', '/NH'],
                         capture_output=True, text=True, creationflags=0x08000000).stdout
    for line in out.splitlines():
        parts = line.strip('"').split('","')
        if len(parts) > 1 and parts[0].lower() == EXE.lower():
            return int(parts[1])
    return None


class Game:
    def __init__(self, pid):
        self.pid = pid
        self.h = k32.OpenProcess(0x10 | 0x400, False, pid)   # VM_READ | QUERY_INFORMATION only
        if not self.h:
            raise OSError(f'OpenProcess {ctypes.get_last_error()}')
        mods = (ctypes.c_void_p * 1)()
        need = W.DWORD()
        psapi.EnumProcessModulesEx(self.h, mods, ctypes.sizeof(mods), ctypes.byref(need), 3)
        info = (ctypes.c_ulonglong * 3)()  # MODULEINFO: base, size(+pad), entry
        psapi.GetModuleInformation(W.HANDLE(self.h), ctypes.c_void_p(mods[0]), info, ctypes.sizeof(info))
        self.naps, self.objs, self.samples = 0, 0, []
        self.vt = struct.pack('<Q', self.find_vtable(mods[0], info[1] & 0xFFFFFFFF) or mods[0] + VT_RVA)

    def find_vtable(self, base, size):
        """CUnit vtable via MSVC RTTI, so a WC3 patch doesn't break us."""
        img = bytearray()
        for off in range(0, size, CHUNK):
            n = min(CHUNK, size - off)
            img += self.read(base + off, n) or bytes(n)
            time.sleep(0.002)
        i = img.find(b'.?AVCUnit@@\0')
        if i < 0:
            return None
        td = struct.pack('<I', i - 0x10)
        j = img.find(td)
        while j >= 0:
            col = j - 12
            if col >= 0 and struct.unpack_from('<II', img, col)[0] == 1 and struct.unpack_from('<I', img, col + 20)[0] == col:
                k = img.find(struct.pack('<Q', base + col))
                if k >= 0:
                    return base + k + 8
            j = img.find(td, j + 1)
        return None

    def close(self):
        k32.CloseHandle(self.h)

    def alive(self):   # tasklist 를 매번 돌리면 0.2초씩 걸린다 -> 핸들로 바로 확인
        code = W.DWORD()
        return bool(k32.GetExitCodeProcess(self.h, ctypes.byref(code))) and code.value == 259

    def read(self, addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t()
        ok = k32.ReadProcessMemory(self.h, addr, buf, n, ctypes.byref(got))
        return buf.raw[:got.value] if ok or got.value else None

    def regions(self):
        out, addr, mbi = set(), 0, MBI()
        while k32.VirtualQueryEx(self.h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            if mbi.State == 0x1000 and mbi.Type != 0x1000000 and mbi.Protect & 0x44 and not mbi.Protect & 0x100:
                out.add((mbi.BaseAddress, mbi.RegionSize))
            addr = mbi.BaseAddress + mbi.RegionSize
            if addr >= 0x7FFFFFFFFFFF:
                break
        return out

    def scan(self, base, size, found, detail=None):
        hit = False
        for off in range(0, size, CHUNK):
            b = self.read(base + off, min(CHUNK, size - off))
            self.naps += 1
            if self.naps % NAP_EVERY == 0:
                time.sleep(NAP)
            if not b:
                continue
            i = b.find(self.vt)
            while i >= 0:
                if i % 8 == 0:
                    o = b[i:i + SZ] if i + SZ <= len(b) else self.read(base + off + i, SZ)
                    if o and len(o) == SZ:
                        hit = True  # any unit object (dummies too): new units land in these pools later
                        self.objs += 1
                        if len(self.samples) < 400: self.samples.append(o)
                        sid = CODES.get(o[OFF_TYPE:OFF_TYPE + 4])
                        if detail is not None:
                            detail.append((struct.unpack_from('<I', o, OFF_OWNER)[0], o[OFF_TYPE:OFF_TYPE + 4][::-1].decode('latin1', 'replace'),
                                           struct.unpack_from('<I', o, OFF_GONE)[0], sid, hex(base + off + i)))
                        if sid is not None and struct.unpack_from('<I', o, OFF_GONE)[0] == 0:
                            found.append((struct.unpack_from('<I', o, OFF_OWNER)[0], sid))
                i = b.find(self.vt, i + 1)
        return hit


def fix_offsets(samples):
    """유닛 객체는 많은데 아는 유닛 코드가 하나도 안 잡히면: 코드가 가장 많이 들어 있는 위치를 typeId 로 다시 잡는다.
    주인·제거 표시 위치는 typeId 에서 같은 거리만큼 옮겨졌다고 가정한다(64비트 1.36 기준 -0x18, +0x204)."""
    global OFF_TYPE, OFF_OWNER, OFF_GONE
    best = max(range(0, SZ - 4, 4), key=lambda off: sum(o[off:off + 4] in CODES for o in samples))
    hits = sum(o[best:best + 4] in CODES for o in samples)
    if hits < len(samples) * 0.1 or best == OFF_TYPE:
        state['notice'] = '워크3 업데이트로 메모리 구조가 바뀐 것 같아요. 유닛을 못 찾고 있어요 (Claude에게 알려 주세요)'
        return False
    shift = best - OFF_TYPE
    OFF_TYPE, OFF_OWNER, OFF_GONE = best, OFF_OWNER + shift, OFF_GONE + shift
    state['notice'] = f'워크3 업데이트 감지: 유닛 위치를 다시 찾았어요 (+{shift:#x}). 숫자가 이상하면 알려 주세요'
    print(state['notice'])
    return True


def publish(found):
    counts, players = {}, {}
    for owner, sid in found:
        if sid >= 0:
            players[owner] = players.get(owner, 0) + 1
    if state['auto']:
        # ponytail: slot 7 = this map's showcase computer (200+ units), 24+ = neutral. One human candidate -> that's me.
        # With 2+ humans we can't tell who is local, so the last chosen slot stays.
        cand = [o for o, n in humans(players).items() if n < 150]
        if len(cand) == 1 and cand[0] != state['owner']:
            state['owner'] = cand[0]; save_cfg()
    for owner, sid in found:
        if owner == state['owner']:
            counts[sid] = counts.get(sid, 0) + 1
    if humans(players) and not game['start']:
        game['start'] = time.time()
    state.update(counts=counts, players=players, ts=time.time())


def write_diag(g, regions):
    """'있는데 안 잡혀요' 신고: 지금 메모리에 있는 유닛 객체를 전부(작은 구역 전체) 적어 둔다."""
    name = state.pop('diag_req')
    detail = []
    for r in sorted(regions):
        if r[1] <= SMALL * 4:
            g.scan(*r, [], detail)
    me = state['owner']
    out = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'reported': name, 'owner': me, 'players': state['players'],
           'offsets': [OFF_TYPE, OFF_OWNER, OFF_GONE], 'counts_shown': state['counts'], 'map': state['map_version'],
           # 내 슬롯의 모든 유닛 객체: [코드, 제거표시, 짝지어진 sions id, 주소]
           'mine': [[c, gone, sid, addr] for o, c, gone, sid, addr in detail if o == me],
           # 슬롯별 코드 개수 (살아 있는 것만)
           'by_owner': {str(o): {} for o in {d[0] for d in detail}}}
    for o, c, gone, sid, addr in detail:
        if gone == 0:
            out['by_owner'][str(o)][c] = out['by_owner'][str(o)].get(c, 0) + 1
    path = os.path.join(HERE, time.strftime('diag-%m%d-%H%M%S.json'))
    json.dump(out, open(path, 'w', encoding='utf-8'), ensure_ascii=False)
    state['notice'] = f'신고 기록을 남겼어요: {os.path.basename(path)} (Claude에게 이 파일을 알려 주세요)'


def tracker():
    while True:
        pid = find_pid()
        if not pid:
            end_game()
            state.update(status='워크3 기다리는 중', counts={}, players={}); time.sleep(5); continue
        try:
            g = Game(pid)
        except OSError as e:
            state['status'] = f'워크3 접근 실패: {e}'; time.sleep(5); continue
        try:
            state['status'] = '첫 스캔 중'
            seen = g.regions()
            hot, found = set(), []
            total, done, shown = sum(sz for _, sz in seen) or 1, 0, False
            # 유닛은 64KB~1MB 짜리 작은 구역에 모여 있다: 지난 판 구역 -> 작은 구역 -> 큰 구역 순으로 훑고,
            # 작은 구역을 다 보면 바로 화면에 보여 준다 (나머지는 뒤에서 계속)
            for r in sorted(seen, key=lambda r: (r[0] not in last_hot_bases, r[1])):
                if not shown and r[1] > (2 << 20) and found:
                    publish(found); state['status'], shown = '연결됨 (나머지 확인 중)', True
                if g.scan(*r, found):
                    hot.add(r)
                done += r[1]; state['scan'] = round(done * 100 / total)
            last_hot_bases.clear(); last_hot_bases.update(b for b, _ in hot)
            if not found and g.objs > 100 and fix_offsets(g.samples):   # 워크3 패치로 필드 위치가 바뀐 경우
                found = []
                for r in sorted(hot):
                    g.scan(*r, found)
            publish(found)
            state['status'] = '연결됨'
            small_q, big_q, fresh, empty_since = [], [], {}, None
            while g.alive():
                # 로비 / 로딩 / 판 끝: 유닛이 6초 넘게 없으면 판이 새로 시작될 때 전체를 다시 훑는다
                empty_since = (empty_since or time.time()) if not state['players'] else None
                if empty_since and time.time() - empty_since > 6:
                    end_game()
                    state['status'] = '게임 시작 기다리는 중'
                    break
                now = g.regions()
                for r in now - seen:
                    fresh[r] = 30  # 새 메모리 구역은 첫 유닛이 몇 틱 늦게 들어올 수 있다
                fresh = {r: n - 1 for r, n in fresh.items() if n > 0 and r in now}
                todo = (hot & now) | set(fresh)
                seen = now
                # 유닛이 없던 작은 구역: 새 유닛이 여기 생기면 놓치므로 몇 초마다 한 바퀴 돈다
                if not small_q:
                    small_q = sorted(r for r in now - hot if r[1] <= SMALL)
                budget = SMALL_MB << 20
                while small_q and budget > 0:
                    r = small_q.pop()
                    if r in now:
                        todo.add(r); budget -= r[1]
                # 큰 구역은 잘라서 천천히 (유닛이 있을 가능성이 낮다)
                if not big_q:
                    big_q = [(r, off) for r in sorted(now - hot) if r[1] > SMALL for off in range(0, r[1], PIECE)]
                pieces, budget = [], BIG_MB << 20
                while big_q and budget > 0:
                    r, off = big_q.pop()
                    if r in now and r not in todo:
                        pieces.append((r, off)); budget -= PIECE
                found, newhot = [], set()
                for r in todo:
                    if g.scan(*r, found):
                        newhot.add(r)
                for r, off in pieces:
                    part = []
                    if g.scan(r[0] + off, min(PIECE + SZ, r[1] - off), part):
                        newhot.add(r)   # 다음 틱부터 구역 전체를 매번 읽는다
                        found += part
                hot = newhot
                publish(found)
                if state.get('diag_req'):
                    write_diag(g, now)
                time.sleep(TICK)
        except Exception as e:
            state['status'] = f'오류: {e!r}'
            traceback.print_exc()
            time.sleep(3)
        finally:
            g.close()


# ───────── 게임 위 작은 창 (항상 위) ─────────
def overlay():
    """항상 위에 떠 있는 반투명 창. 페이지가 보내 준 요약(/summary)을 1초마다 그린다.
    제목줄: 끌어서 옮기기 · A-/A+ 글자 · ◐ 투명도 · 간단/전체 · ✕ 닫기. 본문은 마우스 휠로 스크롤.
    단축키: Ctrl+Shift+O 클릭 통과(게임에 클릭 전달) 켜기/끄기, Ctrl+Shift+H 숨기기/보이기."""
    import tkinter as tk
    OV_PATH = os.path.join(HERE, 'overlay.json')
    oc = {'x': 24, 'y': 140, 'font': 9, 'alpha': 0.9, 'full': True}
    try:
        oc.update(json.load(open(OV_PATH, encoding='utf-8')))
    except Exception:
        pass
    save = lambda: json.dump(oc, open(OV_PATH, 'w', encoding='utf-8'))
    BG, BAR, DIM, FG = '#12141a', '#1d2230', '#8a92a0', '#e8eaf0'
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes('-topmost', True)
    root.attributes('-alpha', oc['alpha'])
    root.configure(bg=BG)
    root.geometry(f"+{oc['x']}+{oc['y']}")
    bar = tk.Frame(root, bg=BAR)
    bar.pack(fill='x')
    title = tk.Label(bar, text='원랜디 도우미', bg=BAR, fg=FG, font=('Malgun Gothic', 9, 'bold'), padx=8, pady=3)
    title.pack(side='left')

    def button(text, fn, color=DIM):
        w = tk.Label(bar, text=text, bg=BAR, fg=color, padx=5, cursor='hand2')
        w.pack(side='right'); w.bind('<Button-1>', lambda e: fn())
        return w

    last = {'s': {}, 'ready': set(), 'new': {}}
    button('✕', root.destroy)
    tog = button('간단히' if oc['full'] else '전체', lambda: (oc.update(full=not oc['full']), tog.config(text='간단히' if oc['full'] else '전체'), save(), draw(last['s'])), '#6aa8ff')
    button('◐', lambda: (oc.update(alpha={0.9: 0.75, 0.75: 0.6, 0.6: 0.95}.get(oc['alpha'], 0.9)), root.attributes('-alpha', oc['alpha']), save()))
    button('A+', lambda: (oc.update(font=min(oc['font'] + 1, 14)), save(), restyle()))
    button('A-', lambda: (oc.update(font=max(oc['font'] - 1, 7)), save(), restyle()))
    drag = {}

    def drag_end(e):
        oc.update(x=root.winfo_x(), y=root.winfo_y()); save()
    for w in (bar, title):
        w.bind('<ButtonPress-1>', lambda e: drag.update(x=e.x_root - root.winfo_x(), y=e.y_root - root.winfo_y()))
        w.bind('<B1-Motion>', lambda e: root.geometry(f'+{e.x_root - drag["x"]}+{e.y_root - drag["y"]}'))
        w.bind('<ButtonRelease-1>', drag_end)
    txt = tk.Text(root, bg=BG, fg=FG, bd=0, padx=10, pady=6, cursor='arrow', spacing1=1, highlightthickness=0, wrap='word')
    txt.pack(fill='both', expand=True)
    txt.bind('<MouseWheel>', lambda e: (txt.yview_scroll(-e.delta // 120, 'units'), 'break')[1])

    def restyle():
        f = oc['font']
        txt.config(font=('Malgun Gothic', f), width=60)   # 글자 수 기준이라 글자가 커지면 창도 같이 커진다
        for tag, kw in {'dim': {'foreground': DIM}, 'ok': {'foreground': '#4cc38a'}, 'warn': {'foreground': '#f0b43c'},
                        'bad': {'foreground': '#ef5b5b'}, 'gold': {'foreground': '#ffd166'}, 'b': {'font': ('Malgun Gothic', f, 'bold')},
                        'h': {'font': ('Malgun Gothic', f + 1, 'bold')}, 'fg': {'foreground': FG}, 'new': {'background': '#3d5a2a', 'foreground': '#ffffff'}}.items():
            txt.tag_configure(tag, **kw)
        draw(last['s'])

    def color(c):
        txt.tag_configure(c, foreground=c)
        return c

    # 클릭 통과: 창에 WS_EX_TRANSPARENT 를 켜면 마우스가 뒤의 게임으로 간다
    user32 = ctypes.windll.user32
    user32.GetParent.restype = ctypes.c_void_p
    user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    flags = {'click': False, 'hidden': False, 'keys': set()}

    def set_click(on):
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        ex = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, (ex | 0x80000 | 0x20) if on else (ex & ~0x20))
        flags['click'] = on
        draw(last['s'])

    def hotkeys():   # Ctrl+Shift+O / Ctrl+Shift+H — 키 상태만 확인한다 (키보드 후킹 아님)
        down = lambda vk: user32.GetAsyncKeyState(vk) & 0x8000
        combo = down(0x11) and down(0x10)
        for key, vk in (('O', 0x4F), ('H', 0x48)):
            pressed = combo and down(vk)
            if pressed and key not in flags['keys']:
                if key == 'O':
                    set_click(not flags['click'])
                else:
                    flags['hidden'] = not flags['hidden']
                    root.withdraw() if flags['hidden'] else root.deiconify()
            (flags['keys'].add if pressed else flags['keys'].discard)(key)
        root.after(80, hotkeys)

    def draw(s):
        y = txt.yview()[0]
        txt.config(state='normal'); txt.delete('1.0', 'end')
        put = lambda t, *tags: txt.insert('end', t, tags)
        now, info, n_tags = time.time(), s.get('info') or {}, [0]

        def unit(text, key, *tags):   # 유닛 이름: 누르면 맨 위에 조합식이 펼쳐진다 (다시 누르면 닫힘)
            if key not in info:
                return put(text, *tags)
            tag = f'u{n_tags[0]}'; n_tags[0] += 1
            txt.insert('end', text, tags + (tag,))
            txt.tag_bind(tag, '<Button-1>', lambda e, k=key: (flags.update(sel=None if flags.get('sel') == k else k), draw(last['s'])))

        def units(names, grade, tag, sep_tag):
            for i, n in enumerate(names):   # 이름만, 또는 [이름, 꼬리표("7", "80%", "2단계")]
                base, label = (n, '') if isinstance(n, str) else (n[0], ' ' + str(n[1]))
                unit(base + label, f'{base}|{grade}', 'new' if tag == 'ok' and last['new'].get(base, 0) > now else tag)
                put(', ' if i < len(names) - 1 else '\n', sep_tag)

        title.config(text=f"원랜디 도우미 · {s.get('status', '')} · {s.get('mode', '')}" + (' · 클릭 통과 중(Ctrl+Shift+O)' if flags['click'] else ''))
        sel = flags.get('sel')
        if sel and sel in info:
            put(f"▣ {sel.replace('|', ' · ')}\n", 'gold', 'b')
            for line in info[sel].split('\n'):
                put('   ' + line + '\n')
            put('\n')
        for k, v, t in s.get('stats', []):
            r = v / t if t else 1
            put(f'{k} ', 'b'); put(f'{v}/{t}   ', 'ok' if r >= 1 else 'warn' if r >= 0.6 else 'bad')
        put('\n')
        warns = s.get('warns') or []
        put(('  ·  '.join(warns) if warns else '목표치 다 채움') + '\n', 'bad' if warns else 'ok')
        if s.get('plan'):
            put('▶ 계획 ', 'gold', 'b'); put(s['plan']['steps'], 'gold'); put(f"   ({s['plan']['result']})\n", 'dim')
        for r in s.get('recs', [])[: (6 if oc['full'] else 3)]:
            put('★ ', 'gold'); unit(r['n'], f"{r['n']}|{r['g']}", 'b'); put(f" {r['g']} ", color(r['gc'])); put(f"{r['gains']}  "); put(r['st'], 'ok' if r['st'] == '바로 가능' else 'warn')
            put((f"  {r['why']}" if r.get('why') and oc['full'] else ''), 'gold')
            put((f"  부족: {r['lack']}" if r['lack'] and oc['full'] else '') + '\n', 'dim')
        for p in s.get('pins', []):
            put('찜 ', 'warn'); put(f"{p['n']} · {p['st']}\n")
        if not oc['full']:
            if s.get('ready'):
                put('지금 가능: ', 'ok'); put(', '.join(s['ready']) + '\n', 'ok')
        else:
            for g in s.get('grades', []):
                put(f"\n{g['g']}", color(g['gc']), 'h'); put('\n')
                if g['have']: put('  보유 ', 'dim'); units(g['have'], g['g'], 'fg', 'fg')
                if g['ready']: put('  가능 ', 'dim'); units(g['ready'], g['g'], 'ok', 'ok')
                if g['near']: put('  가까움 ', 'dim'); units(g['near'], g['g'], 'warn', 'warn')
        if s.get('age', 0) > 15 and s.get('stats'):
            put('\n도우미 페이지가 닫혀 있어서 갱신이 멈췄어요 — 브라우저에서 127.0.0.1:8765 를 열어 두세요\n', 'bad')
        lines = int(txt.index('end-1c').split('.')[0])
        txt.config(height=min(max(lines, 4), 42), state='disabled')
        txt.yview_moveto(y)

    def poll():
        try:
            s = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/summary', timeout=2).read())
            flags['fail'] = 0
            if not s.get('stats'):
                s['status'] = s.get('status', '') + ' (도우미 페이지를 열어 두세요)'
        except Exception:
            s = {'status': '프로그램 연결 안 됨'}
            flags['fail'] = flags.get('fail', 0) + 1
            if flags['fail'] >= 8:   # 본 프로그램이 꺼지면 작은 창도 같이 닫힌다
                root.destroy(); return
        ready = {n for g in s.get('grades', []) for n in g.get('ready', [])} | set(s.get('ready', []))
        if last['s']:
            for n in ready - last['ready']:
                last['new'][n] = time.time() + 4   # 새로 가능해진 조합: 4초 동안 강조
        last['ready'] = ready
        s2 = {k: v for k, v in s.items() if k != 'age'}
        if s2 != {k: v for k, v in last['s'].items() if k != 'age'} or any(t > time.time() - 1 for t in last['new'].values()) or s.get('age', 0) > 15:
            last['s'] = s; draw(s)
        root.after(1000, poll)
    restyle()
    poll()
    hotkeys()
    root.mainloop()


# ───────── 자동 실행 / 새 버전 확인 ─────────
STARTUP = os.path.join(os.environ.get('APPDATA', ''), r'Microsoft\Windows\Start Menu\Programs\Startup', 'ordr-helper.vbs')


def set_startup(on):
    if on:
        if FROZEN:
            cmd = f'""{sys.executable}"" --no-browser'
        else:
            pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
            cmd = f'""{pyw}"" ""{os.path.abspath(__file__)}"" --no-browser'
        open(STARTUP, 'w', encoding='utf-16').write(f'CreateObject("WScript.Shell").Run "{cmd}", 0\n')   # 창 없이 실행
    elif os.path.exists(STARTUP):
        os.remove(STARTUP)
    state['startup'] = os.path.exists(STARTUP)


def check_update():
    while True:
        try:
            req = urllib.request.Request(f'https://api.github.com/repos/{REPO}/releases/latest', headers={'User-Agent': 'ordr-helper'})
            rel = json.loads(urllib.request.urlopen(req, timeout=10).read())
            tag = rel.get('tag_name', '').lstrip('v')
            if tag and tuple(map(int, tag.split('.'))) > tuple(map(int, VERSION.split('.'))):
                state['update'] = {'ver': tag, 'url': rel.get('html_url')}
        except Exception:
            pass
        time.sleep(6 * 3600)


def spawn_overlay():
    args = [sys.executable] + ([] if FROZEN else [os.path.abspath(__file__)]) + ['--overlay-only', f'--port={PORT}']
    subprocess.Popen(args, creationflags=0x08000000)


# ───────── 웹 ─────────
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def do_OPTIONS(self):
        self._send(204, b'', 'text/plain')

    def do_GET(self):
        path, _, q = self.path.partition('?')
        if path == '/counts':
            self._json(state)
        elif path == '/owner' and q.startswith('n='):
            if q[2:] == 'auto':
                state['auto'] = True
            else:
                state['owner'], state['auto'] = int(q[2:]), False
            save_cfg()
            self._send(200, b'ok', 'text/plain')
        elif path == '/data':
            self._send(200, SIONS, 'application/json; charset=utf-8')
        elif path == '/summary':
            self._json({**summary, 'status': state['status'], 'age': round(time.time() - game['sum_ts'])})
        elif path == '/startup' and q in ('on=1', 'on=0'):
            set_startup(q == 'on=1'); self._json({'startup': state['startup']})
        elif path == '/diag':
            import urllib.parse
            state['diag_req'] = urllib.parse.unquote(q[5:]) if q.startswith('name=') else '?'
            self._send(200, b'ok', 'text/plain')
        elif path == '/quit':
            self._send(200, b'bye', 'text/plain'); threading.Timer(0.3, lambda: os._exit(0)).start()
        elif path == '/history':
            try:
                self._json(json.load(open(HIST_PATH, encoding='utf-8'))[::-1])
            except Exception:
                self._json([])
        elif path in ('/', '/index.html'):
            self._send(200, open(os.path.join(RES, 'index.html'), 'rb').read(), 'text/html; charset=utf-8')
        else:
            self._send(404, b'', 'text/plain')

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if self.path == '/summary':
            summary.clear(); summary.update(json.loads(body or b'{}'))
            now = time.time(); game['sum_ts'] = now
            if game['start'] and now - game['tl_ts'] >= 30 and summary.get('stats'):   # 판 다시보기용: 30초마다 한 점
                game['timeline'].append({'m': round((now - game['start']) / 60, 1), 'n': summary.get('units', 0),
                                         's': {k: v for k, v, _t in summary['stats']}})
                game['tl_ts'] = now
            self._send(200, b'ok', 'text/plain')
        elif self.path == '/overlay':
            spawn_overlay()
            self._send(200, b'ok', 'text/plain')
        else:
            self._send(404, b'', 'text/plain')

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    if '--overlay-only' in sys.argv:
        overlay(); sys.exit()
    SIONS = json.dumps(load_sions(), ensure_ascii=False).encode()
    threading.Thread(target=check_map_update, daemon=True).start()
    threading.Thread(target=check_update, daemon=True).start()
    state['startup'] = os.path.exists(STARTUP)
    threading.Thread(target=tracker, daemon=True).start()
    lan = '--lan' in sys.argv   # 폰/다른 PC 에서 보기: 같은 와이파이 안에서만
    srv = ThreadingHTTPServer(('0.0.0.0' if lan else '127.0.0.1', PORT), Handler)
    print(f'원랜디 자동 카운터 실행 중 - 브라우저에서 http://127.0.0.1:{PORT} 열기  (끄려면 이 창을 닫으세요)')
    if lan:
        import socket
        ip = socket.gethostbyname(socket.gethostname())
        print(f'폰에서 보기: 같은 와이파이에서 http://{ip}:{PORT}/?mini  (윈도우 방화벽이 물으면 "개인 네트워크" 허용)')
    if '--no-browser' not in sys.argv:
        import webbrowser; webbrowser.open(f'http://127.0.0.1:{PORT}')
    last = None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        while True:
            s = f"[{state['status']}{' ' + str(state['scan']) + '%' if state['status'] == '첫 스캔 중' else ''}] 내 슬롯={state['owner'] + 1}번{'(자동)' if state['auto'] else ''} 유닛={sum(n for k, n in state['counts'].items() if k >= 0)} 플레이어별={state['players']}"
            if s != last:
                print(time.strftime('%H:%M:%S'), s, flush=True); last = s
            time.sleep(1)
    except KeyboardInterrupt:
        pass
