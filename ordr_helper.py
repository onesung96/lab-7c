"""원랜디 자동 카운터.

워크3 메모리를 '읽기만' 해서 내 유닛 수를 세고, http://127.0.0.1:8765 전용 페이지에 보여준다.
조합식·유닛 수치는 sions 조합 도우미 글에서 받아온다(sions_cache.json 에 캐시).
새 맵 버전이 보이면 mapping.json 을 스스로 다시 만든다(mapsync.py).

게임이 멈추지 않도록: 1MB씩 나눠 읽고 중간중간 쉰다. 첫 스캔만 20초~1분, 이후엔 2초마다 조금씩.

옵션: --lan (폰에서 보기)  --no-browser  --port=NNNN  --overlay-only (게임 위 작은 창만 띄움)
"""
import ctypes, ctypes.wintypes as W, json, os, struct, subprocess, sys, threading, time, traceback, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.6.3'
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
TICK = 0.5                # (예비 방식) 유닛 구역은 0.5초마다 다시 읽는다
LIST_TICK = 0.25          # 게임의 유닛 목록을 읽는 주기
SMALL = 1 << 20           # 유닛은 64KB~1MB 짜리 작은 구역에 생긴다
SMALL_MB, BIG_MB = 32, 16  # 틱마다 추가로 훑는 양: 유닛 없던 작은 구역(빨리 한 바퀴) / 큰 구역(천천히)
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
         'map_version': cfg['map_version'], 'version': VERSION, 'frozen': FROZEN, 'update': None, 'startup': False}
apply_mapping(json.load(open(res_or_local('mapping.json'), encoding='utf-8')))
SIONS = b'{}'
summary = {}           # 페이지가 계산해서 보내 주는 요약 (작은 창·판 기록용)
game = {'start': None, 'timeline': [], 'tl_ts': 0, 'sum_ts': 0, 'names': []}   # names: [(슬롯, 이름)] 사람만 이름이 있다
last_hot_bases = set()   # 지난 판에 유닛이 있던 메모리 구역: 다음 판 첫 스캔에서 먼저 본다
last_world = {'base': None}   # 유닛 목록을 가진 물체가 있던 구역: 다음에 먼저 본다


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


PID_TEST = next((int(a.split('=')[1]) for a in sys.argv if a.startswith('--pid=')), None)   # 시험용: 가짜 게임 프로세스에 붙기


def find_pid():
    if PID_TEST:
        h = k32.OpenProcess(0x1000, False, PID_TEST)
        code = W.DWORD()
        ok = h and k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value == 259
        if h: k32.CloseHandle(h)
        return PID_TEST if ok else None
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
        if not mods[0]:
            k32.CloseHandle(self.h)
            raise OSError('게임이 아직 켜지는 중')
        info = (ctypes.c_ulonglong * 3)()  # MODULEINFO: base, size(+pad), entry
        psapi.GetModuleInformation(W.HANDLE(self.h), ctypes.c_void_p(mods[0]), info, ctypes.sizeof(info))
        self.naps, self.objs, self.samples = 0, 0, []
        base, size = mods[0], info[1] & 0xFFFFFFFF
        img = bytearray()
        for off in range(0, size, 0x10000):   # 64KB씩: 1MB씩 읽으면 못 읽는 페이지가 하나만 섞여도 그 1MB 가 통째로 비어 버린다
            n = min(0x10000, size - off)
            d = self.read(base + off, n)
            if d is None or len(d) < n:       # 일부가 못 읽는 페이지 -> 4KB 단위로 다시
                d = b''.join((self.read(base + off + q, min(0x1000, n - q)) or b'').ljust(min(0x1000, n - q), b'\0') for q in range(0, n, 0x1000))
            img += d
        self.vt = struct.pack('<Q', self.vtable_of(img, base, b'CUnit') or base + VT_RVA)
        wf = self.vtable_of(img, base, b'CWorldFrameWar3')
        self.wf_vt = struct.pack('<Q', wf) if wf else None
        pl = self.vtable_of(img, base, b'CPlayerWar3')
        self.pl_vt = struct.pack('<Q', pl) if pl else None
        self.world = None        # (CWorldFrameWar3 주소, 유닛 목록 {개수, 주소, 용량} 의 위치)

    @staticmethod
    def vtable_of(img, base, name):
        """클래스 이름 -> vtable 주소 (MSVC RTTI). 주소를 박아 두지 않아서 워크3 패치에도 다시 찾는다.
        상속이 여러 갈래면 vtable 도 여러 개라, 물체 맨 앞에 놓이는 것(offset 0)을 고른다."""
        i = img.find(b'.?AV' + name + b'@@\0')
        if i < 0:
            return None
        td, any_vt = struct.pack('<I', i - 0x10), None
        j = img.find(td)
        while j >= 0:
            col = j - 12
            if col >= 0 and struct.unpack_from('<I', img, col)[0] == 1 and struct.unpack_from('<I', img, col + 20)[0] == col:
                k = img.find(struct.pack('<Q', base + col))
                if k >= 0:
                    if struct.unpack_from('<I', img, col + 4)[0] == 0:
                        return base + k + 8
                    any_vt = any_vt or base + k + 8
            j = img.find(td, j + 1)
        return any_vt

    def find_unit_list(self, obj):
        """CWorldFrameWar3 안에서 {개수, 목록 주소, 용량} 을 찾는다: 목록이 가리키는 것이 거의 다 유닛이어야 한다.
        위치(지금 버전은 +0xc08)를 박아 두지 않고 매번 찾아서 패치로 밀려도 된다."""
        d = self.read(obj, 0x2000)
        if not d or d[:8] != self.wf_vt:
            return None
        best = None
        for off in range(8, len(d) - 24, 8):
            cnt, ptr, cap = struct.unpack_from('<I4xQI', d, off)
            if not (16 <= cnt <= cap <= 200000 and 0x10000 < ptr < 0x7FFFFFFFFFFF and ptr % 8 == 0):
                continue
            a = self.read(ptr, min(cnt, 48) * 8)
            if not a or len(a) < 8:
                continue
            ps = struct.unpack(f'<{len(a) // 8}Q', a)
            ok = sum(1 for q in ps if 0x10000 < q < 0x7FFFFFFFFFFF and self.read(q, 8) == self.vt)
            if ok >= len(ps) * 0.8 and (best is None or cnt > best[0]):
                best = (cnt, off)
        return best and best[1]

    def find_world(self):
        """게임의 유닛 목록을 가진 CWorldFrameWar3 을 찾는다. 4MB 이하 구역만, 지난번에 있던 구역부터."""
        regs = sorted((r for r in self.regions() if r[1] <= 4 << 20), key=lambda r: (r[0] != last_world['base'], r[1]))
        total, done = sum(sz for _, sz in regs) or 1, 0
        for base, size in regs:
            d = self.read(base, size)
            done += size; state['scan'] = round(done * 100 / total)
            self.naps += 1
            if self.naps % 64 == 0:      # 작은 구역 64개마다 잠깐 쉰다
                time.sleep(NAP)
            if not d:
                continue
            i = d.find(self.wf_vt)
            while i >= 0:
                if i % 8 == 0:
                    off = self.find_unit_list(base + i)
                    if off:
                        last_world['base'] = base
                        return (base + i, off)
                i = d.find(self.wf_vt, i + 1)
        return None

    def world_units(self):
        """게임이 가진 유닛 목록을 그대로 읽는다 -> [(주인, sions id)], 목록이 사라졌으면 None.
        메모리를 훑지 않아서 빠짐이 없고, 한 번에 수백 KB 만 읽는다."""
        obj, off = self.world
        d = self.read(obj, off + 24)
        if not d or len(d) < off + 24 or d[:8] != self.wf_vt:
            return None
        cnt, ptr, cap = struct.unpack_from('<I4xQI', d, off)
        if cnt > cap or cap > 200000:
            return None
        found, self.samples = [], []
        if not cnt:
            return found
        a = self.read(ptr, cnt * 8)
        if not a or len(a) < cnt * 8:
            return None
        for q in struct.unpack(f'<{cnt}Q', a):
            o = self.read(q, SZ)
            if not o or len(o) < SZ or o[:8] != self.vt:
                continue
            if len(self.samples) < 400: self.samples.append(o)
            sid = CODES.get(o[OFF_TYPE:OFF_TYPE + 4])
            if sid is not None and struct.unpack_from('<I', o, OFF_GONE)[0] == 0:
                found.append((struct.unpack_from('<I', o, OFF_OWNER)[0], sid))
        return found

    def find_names(self):
        """플레이어 이름 [(슬롯, 이름)]. CPlayerWar3: 슬롯 번호 +0x6a, 이름 주소 +0xa0, 길이 +0xa8 (3.0.0.24268).
        사람 플레이어만 이름이 있다. 1MB 이하 구역만 훑어서 6초쯤 걸린다 -> 판마다 한 번만."""
        out, naps = [], 0
        for base, size in sorted(self.regions()):
            if size > 1 << 20 or not self.pl_vt:
                continue
            d = self.read(base, size)
            naps += 1
            if naps % 64 == 0:
                time.sleep(NAP)
            i = d.find(self.pl_vt) if d else -1
            while i >= 0:
                if i % 8 == 0 and i + 0xb0 <= len(d):
                    ptr, n = struct.unpack_from('<QQ', d, i + 0xa0)
                    nm = self.read(ptr, n) if 0 < n <= 64 else None
                    if nm and len(nm) == n and d[i + 0x6a] < 24:
                        out.append((d[i + 0x6a], nm.decode('utf-8', 'replace')))
                i = d.find(self.pl_vt, i + 1)
        return out

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
                        self.objs += 1
                        if len(self.samples) < 400: self.samples.append(o)
                        if not o[OFF_TYPE:OFF_TYPE + 4].isalnum():   # 유닛 코드는 영문·숫자 4글자: 아니면 유닛 흔적이 아님
                            i = b.find(self.vt, i + 1); continue
                        hit = True  # 더미 유닛 포함: 새 유닛이 이 풀에 생긴다
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


def my_slot(cand):
    """유닛을 가진 사람 슬롯들 중 내 슬롯. 모르면 None (마지막으로 고른 슬롯이 그대로 남는다).
    ponytail: 게임 안의 '내 번호' 칸을 못 찾아서 이름으로 가린다. 혼자 하는 판에서 내 이름을 배우고,
    여럿이 하는 판에선 그 이름이 붙은 슬롯을 고른다. 닉네임을 바꾸면 혼자 한 판 뒤에 다시 맞는다."""
    names = game['names']
    multi, me = len({n for _, n in names}) > 1, cfg.get('my_name')
    if multi:
        mine = [o for o in cand if (o, me) in names]
        return mine[0] if me and len(mine) == 1 else None
    if len(cand) != 1:
        return None
    if names and names[0][1] != me and (cand[0], names[0][1]) in names:   # 사람이 나뿐인 판: 이 이름이 나다
        cfg['my_name'] = names[0][1]; save_cfg()
    return cand[0]


def read_names(g):
    for _ in range(6):   # 이름이 아직 안 채워졌으면 조금 뒤 다시
        try:
            names = g.find_names()
        except Exception:
            traceback.print_exc(); return
        if names or not g.alive():
            game['names'] = names
            state['names'] = {o: n for o, n in names}
            return
        time.sleep(20)


def publish(found):
    counts, players = {}, {}
    for owner, sid in found:
        if sid >= 0:
            players[owner] = players.get(owner, 0) + 1
    # ponytail: slot 7 = this map's showcase computer (200+ units), 24+ = neutral.
    pick = my_slot([o for o, n in humans(players).items() if n < 150])
    if state['auto'] and pick is not None and pick != state['owner']:
        state['owner'] = pick; save_cfg()
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


def pieces_of(regions):
    """구역을 '주소 기준' 1MB 격자로 자른 조각 {시작 주소: 길이}.
    매 틱 다시 읽는 단위는 구역이 아니라 조각이다(큰 구역 전체를 매번 읽으면 한 바퀴에 수십 초).
    주소 기준 격자라서 구역이 커지거나 옆 구역과 합쳐져도 조각 주소가 안 바뀐다."""
    out = {}
    for base, size in regions:
        a, end = base, base + size
        while a < end:
            nxt = min(end, (a // CHUNK + 1) * CHUNK)
            out[a] = nxt - a
            a = nxt
    return out


def run_list_mode(g):
    """게임의 유닛 목록을 직접 읽는 방식. 목록이 없어지면(판 끝·프로세스 종료) 돌아간다."""
    state['status'] = '연결됨'
    empty_since, bad = None, 0
    game['names'] = []
    threading.Thread(target=read_names, args=(g,), daemon=True).start()   # 6초쯤 걸려서 추적과 따로
    while g.alive():
        t0 = time.time()
        found = g.world_units()
        if found is None:
            return
        if not found and len(g.samples) > 100:          # 유닛은 많은데 아는 코드가 없다 = 워크3 패치로 필드가 밀림
            bad += 1
            if bad > 8 and not fix_offsets(g.samples):
                return
        else:
            bad = 0
        publish(found)
        state.update(tick_ms=round((time.time() - t0) * 1000), hot_mb=0, mode='list')
        if not state['players']:
            empty_since = empty_since or time.time()
            if time.time() - empty_since > 6:
                end_game(); state['status'] = '게임 시작 기다리는 중'
        else:
            empty_since, state['status'] = None, '연결됨'
        if state.get('diag_req'):
            write_diag(g, g.regions())
        time.sleep(LIST_TICK if state['players'] else 1.0)


def tracker():
    while True:
        pid = find_pid()
        if not pid:
            end_game()
            state.update(status='워크3 기다리는 중', counts={}, players={}); time.sleep(5); continue
        try:
            g = Game(pid)
        except Exception as e:   # 어떤 이유로든 못 붙으면 잠깐 뒤 다시 (스레드가 죽으면 안 된다)
            state['status'] = f'워크3 접근 실패: {e}'; time.sleep(3); continue
        try:
            # 1순위: 게임이 가진 유닛 목록을 찾아서 그것만 읽는다
            while g.wf_vt and g.alive():
                if state['status'] not in ('게임 시작 기다리는 중',):
                    state['status'] = '첫 스캔 중'
                g.world = g.find_world()
                if g.world:
                    # 프로그램을 켜 둔 채로 새 맵을 받았을 수도 있다 -> 판마다 다시 확인 (추적은 안 멈추게 따로)
                    threading.Thread(target=check_map_update, daemon=True).start()
                    run_list_mode(g)
                else:                                   # 로비·로딩: 아직 판이 없다
                    end_game()
                    state.update(status='게임 시작 기다리는 중', counts={}, players={})
                    time.sleep(6)
            if not g.alive():
                continue
            # 예비: 클래스를 못 찾는 버전이면 메모리를 훑는 예전 방식
            state.update(status='첫 스캔 중', mode='scan')

            def full_scan():
                regs = g.regions()
                hot, found, bases = set(), [], set()
                total, done, shown = sum(sz for _, sz in regs) or 1, 0, False
                # 유닛은 작은 구역에 모여 있다: 지난 판 구역 -> 작은 구역 -> 큰 구역 순으로 훑고,
                # 작은 구역을 다 보면 바로 화면에 보여 준다 (나머지는 뒤에서 계속)
                for r in sorted(regs, key=lambda r: (r[0] not in last_hot_bases, r[1])):
                    if not shown and r[1] > (2 << 20) and found:
                        publish(found); state['status'], shown = '연결됨 (나머지 확인 중)', True
                    for a, n in pieces_of([r]).items():
                        if g.scan(a, n, found):
                            hot.add(a); bases.add(r[0])
                    done += r[1]; state['scan'] = round(done * 100 / total)
                last_hot_bases.clear(); last_hot_bases.update(bases)   # 다음 판 첫 스캔에서 먼저 볼 구역
                return pieces_of(regs), hot, found

            seen, hot, found = full_scan()
            if not found and g.objs > 100 and fix_offsets(g.samples):   # 워크3 패치로 필드 위치가 바뀐 경우
                seen, hot, found = full_scan()
            publish(found)
            state['status'] = '연결됨'
            small_q, big_q, fresh, empty_since = [], [], {}, None
            while g.alive():
                t0 = time.time()
                # 로비 / 로딩 / 판 끝: 유닛이 6초 넘게 없으면 판이 새로 시작될 때 전체를 다시 훑는다
                empty_since = (empty_since or time.time()) if not state['players'] else None
                if empty_since and time.time() - empty_since > 6:
                    end_game()
                    state['status'] = '게임 시작 기다리는 중'
                    break
                regs = g.regions()
                P = pieces_of(regs)
                # 1) 새로 생기거나 커진 메모리: 게임이 유닛 저장 공간을 늘린 자리일 수 있다 -> 한동안 매 틱 본다.
                #    (게임 후반에 조합한 유닛이 늦게 잡히던 원인: 늘어난 자리가 '큰 구역' 안이라 1~2분에 한 번만 봤다)
                grown = [a for a, n in P.items() if seen.get(a) != n]
                if len(grown) <= 256:
                    for a in grown:
                        fresh[a] = 30
                else:                       # 한꺼번에 수백 MB 가 생기면 천천히 도는 쪽에 넣는다
                    big_q.extend(grown)
                fresh = {a: n - 1 for a, n in fresh.items() if n > 0 and a in P}
                seen = P
                todo = set(hot) | set(fresh)
                # 2) 유닛이 있는 조각의 바로 앞·뒤 조각: 저장 공간은 이어서 늘어난다
                ends = {a + n: a for a, n in P.items()}
                for a in hot:
                    if a not in P:
                        continue
                    nxt = a + P[a]
                    for _ in range(2):
                        if nxt in P:
                            todo.add(nxt); nxt += P[nxt]
                    if a in ends:
                        todo.add(ends[a])
                # 3) 나머지: 작은 구역은 빨리 한 바퀴, 큰 구역은 천천히
                if not small_q:
                    small_q = [a for a in pieces_of(sorted(r for r in regs if r[1] <= SMALL)) if a not in todo]
                if not big_q:
                    big_q = [a for a in pieces_of(sorted(r for r in regs if r[1] > SMALL)) if a not in todo]
                for q, mb in ((small_q, SMALL_MB), (big_q, BIG_MB)):
                    for _ in range(min(mb, len(q))):
                        todo.add(q.pop())
                found, newhot = [], set()
                for a in todo:
                    if a in P and g.scan(a, P[a], found):
                        newhot.add(a)
                hot = newhot
                publish(found)
                state['tick_ms'] = round((time.time() - t0) * 1000)
                state['hot_mb'] = round(sum(P.get(a, 0) for a in hot) / 1048576, 1)
                state['read_mb'] = round(sum(P.get(a, 0) for a in todo) / 1048576)
                if state.get('diag_req'):
                    write_diag(g, regs)
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
    oc = {'x': 24, 'y': 140, 'font': 9, 'alpha': 0.9, 'full': True, 'bar': False,
          'show': {'stats': True, 'wish': True, 'plan': True, 'recs': True, 'pins': True, 'grades': True}}
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
    def menu(e=None):   # ☰: 보일 항목 고르기 + 가로 띠 모드
        m = tk.Menu(root, tearoff=0)
        for key, label in (('stats', '스탯·부족 경고'), ('wish', '뽑히면 좋은 흔함'), ('plan', '조합 계획'), ('recs', '추천'), ('pins', '찜'), ('grades', '등급별 표')):
            m.add_command(label=('✔ ' if oc['show'].get(key, True) else '   ') + label,
                          command=lambda k=key: (oc['show'].update({k: not oc['show'].get(k, True)}), save(), draw(last['s'])))
        m.add_separator()
        m.add_command(label=('✔ ' if oc['bar'] else '   ') + '가로 띠 모드 (화면 위쪽 한 줄)', command=lambda: (oc.update(bar=not oc['bar']), save(), restyle()))
        m.tk_popup(root.winfo_pointerx(), root.winfo_pointery())
    button('☰', menu)
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
        # 글자 수 기준이라 글자가 커지면 창도 같이 커진다. 띠 모드는 화면 폭 전체, 줄바꿈 없음
        txt.config(font=('Malgun Gothic', f), width=int(root.winfo_screenwidth() / (f * 0.95)) if oc['bar'] else 60, wrap='none' if oc['bar'] else 'word')
        if oc['bar']: root.geometry('+0+0')
        else: root.geometry(f"+{oc['x']}+{oc['y']}")
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
        show = lambda k: oc['show'].get(k, True)
        if oc['bar']:   # 한 줄 띠: 스탯 · 부족 · 계획 · 추천 2개 · 지금 가능
            for k, v, t in s.get('stats', []) if show('stats') else []:
                r = v / t if t else 1
                put(f'{k} ', 'b'); put(f'{v}/{t}  ', 'ok' if r >= 1 else 'warn' if r >= 0.6 else 'bad')
            if show('wish') and s.get('wish'): put(' | 뽑히면 좋음: ', 'dim'); put(s['wish'])
            if show('plan') and s.get('plan'): put(' | 계획 ', 'dim'); put(s['plan']['steps'], 'gold')
            for r in s.get('recs', [])[:2] if show('recs') else []:
                put(' | ★ ', 'gold'); put(r['n'], 'b'); put(f" {r['gains']} "); put(r['st'], 'ok' if r['st'] == '바로 가능' else 'warn')
            if s.get('ready'): put(' | 가능: ', 'dim'); put(', '.join(s['ready'][:8]), 'ok')
            txt.config(height=1, state='disabled')
            return
        sel = flags.get('sel')
        if sel and sel in info:
            put(f"▣ {sel.replace('|', ' · ')}\n", 'gold', 'b')
            for line in info[sel].split('\n'):
                put('   ' + line + '\n')
            put('\n')
        if show('stats'):
            for k, v, t in s.get('stats', []):
                r = v / t if t else 1
                put(f'{k} ', 'b'); put(f'{v}/{t}   ', 'ok' if r >= 1 else 'warn' if r >= 0.6 else 'bad')
            put('\n')
            warns = s.get('warns') or []
            put(('  ·  '.join(warns) if warns else '목표치 다 채움') + '\n', 'bad' if warns else 'ok')
        if show('wish') and s.get('wish'):
            put('뽑히면 좋은 흔함: ', 'dim'); put(s['wish'] + '\n')
        if show('plan') and s.get('plan'):
            put('▶ 계획 ', 'gold', 'b'); put(s['plan']['steps'], 'gold'); put(f"   ({s['plan']['result']})\n", 'dim')
        for r in s.get('recs', [])[: (6 if oc['full'] else 3)] if show('recs') else []:
            put('★ ', 'gold'); unit(r['n'], f"{r['n']}|{r['g']}", 'b'); put(f" {r['g']} ", color(r['gc'])); put(f"{r['gains']}  "); put(r['st'], 'ok' if r['st'] == '바로 가능' else 'warn')
            put((f"  {r['why']}" if r.get('why') and oc['full'] else ''), 'gold')
            put((f"  부족: {r['lack']}" if r['lack'] and oc['full'] else '') + '\n', 'dim')
        for p in s.get('pins', []) if show('pins') else []:
            put('찜 ', 'warn'); put(f"{p['n']} · {p['st']}\n")
        if not oc['full']:
            if s.get('ready'):
                put('지금 가능: ', 'ok'); put(', '.join(s['ready']) + '\n', 'ok')
        elif show('grades'):
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


def newer_release():
    """깃허브 최신 릴리스가 지금 버전보다 높으면 (버전, 페이지 주소), 아니면 None"""
    req = urllib.request.Request(f'https://api.github.com/repos/{REPO}/releases/latest', headers={'User-Agent': 'ordr-helper'})
    rel = json.loads(urllib.request.urlopen(req, timeout=10).read())
    tag = rel.get('tag_name', '').lstrip('v')
    if tag and tuple(map(int, tag.split('.'))) > tuple(map(int, VERSION.split('.'))):
        return tag, rel.get('html_url')
    return None


def check_update():
    """켤 때 한 번 + 30분마다 확인. 켤 때 새 버전이 있으면 묻지 않고 바로 설치한다(exe 만).
    게임 중에 프로그램이 저절로 재시작되면 안 되니, 30분 확인에서는 버튼만 띄운다."""
    first = True
    while True:
        try:
            new = newer_release()
            if new:
                state['update'] = {'ver': new[0], 'url': new[1]}
                # 같은 버전 설치는 한 번만 시도: 받은 파일이 잘못돼 버전이 안 오르면 재시작이 끝없이 반복된다
                if first and FROZEN and cfg.get('update_tried') != new[0] and '--no-auto-update' not in sys.argv:
                    cfg['update_tried'] = new[0]; save_cfg()
                    state['notice'] = f'새 버전 {new[0]} 설치 중… 곧 다시 켜져요'
                    if TRAY: TRAY.balloon('원랜디 도우미 업데이트', f'새 버전 {new[0]} 을 설치하고 다시 켜요')
                    self_update()
        except Exception as e:
            print('업데이트 확인 실패:', e)
        first = False
        time.sleep(30 * 60)


def tray_update():
    """트레이 메뉴 '업데이트 확인': 새 버전이 있으면 바로 설치, 없으면 알려 준다."""
    try:
        new = newer_release()
        if not new:
            if TRAY: TRAY.balloon('원랜디 도우미', f'이미 최신 버전이에요 ({VERSION})')
            return
        state['update'] = {'ver': new[0], 'url': new[1]}
        if FROZEN:
            if TRAY: TRAY.balloon('원랜디 도우미 업데이트', f'새 버전 {new[0]} 을 설치하고 다시 켜요')
            cfg['update_tried'] = new[0]; save_cfg()
            self_update()
        else:
            import webbrowser; webbrowser.open(new[1])
    except Exception as e:
        if TRAY: TRAY.balloon('원랜디 도우미', f'업데이트 확인 실패: {e}')


def self_update():
    """최신 릴리스 zip 을 받아 풀고, 프로그램이 꺼진 뒤 파일을 덮어쓰고 다시 켜는 배치 파일을 돌린다.
    config·기록·overlay 설정은 zip 에 없어서 그대로 남는다."""
    import zipfile
    if not FROZEN:
        raise RuntimeError('exe 로 켰을 때만 자동 교체돼요')
    req = urllib.request.Request(f'https://api.github.com/repos/{REPO}/releases/latest', headers={'User-Agent': 'ordr-helper'})
    rel = json.loads(urllib.request.urlopen(req, timeout=15).read())
    url = next(a['browser_download_url'] for a in rel['assets'] if a['name'].endswith('.zip'))
    zpath, new = os.path.join(HERE, 'update.zip'), os.path.join(HERE, '_new')
    urllib.request.urlretrieve(url, zpath)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(new)
    inner = [d for d in os.listdir(new) if os.path.isdir(os.path.join(new, d))]
    src = os.path.join(new, inner[0]) if len(inner) == 1 else new   # zip 안에 폴더 하나로 들어 있다
    if not os.path.exists(os.path.join(src, os.path.basename(sys.executable))):
        raise RuntimeError('받은 파일에 실행 파일이 없어요')
    bat = os.path.join(HERE, '_update.bat')
    args = ' '.join(dict.fromkeys(sys.argv[1:] + ['--no-browser']))   # 같은 옵션으로 다시 켜되, 페이지는 이미 열려 있으니 새 탭은 안 연다
    open(bat, 'w', encoding='mbcs').write(
        '@echo off\r\n'
        'timeout /t 3 /nobreak >nul\r\n'
        f'taskkill /f /im "{os.path.basename(sys.executable)}" >nul 2>&1\r\n'
        f'robocopy "{src}" "{HERE}" /E /NFL /NDL /NJH /NJS /R:5 /W:1 >nul\r\n'
        f'rmdir /s /q "{new}"\r\n'
        f'del "{zpath}"\r\n'
        f'start "" "{sys.executable}" {args}\r\n'
        'del "%~f0"\r\n')
    subprocess.Popen(['cmd', '/c', bat], creationflags=0x08000000 | 0x00000200, close_fds=True)   # 창 없이, 우리와 따로
    threading.Timer(1.0, quit_app).start()


# ───────── 게임 채팅에 조합 명령어 입력 ─────────
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', W.WORD), ('wScan', W.WORD), ('dwFlags', W.DWORD), ('time', W.DWORD), ('dwExtraInfo', ctypes.c_void_p)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [('ki', KEYBDINPUT), ('pad', ctypes.c_byte * 32)]
    _anonymous_ = ('u',)
    _fields_ = [('type', W.DWORD), ('u', _U)]


u32 = ctypes.WinDLL('user32', use_last_error=True)
u32.GetForegroundWindow.restype = W.HWND
u32.GetWindow.restype = W.HWND
u32.GetWindow.argtypes = [W.HWND, W.UINT]
u32.IsWindowVisible.argtypes = u32.IsIconic.argtypes = u32.SetForegroundWindow.argtypes = [W.HWND]
u32.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
u32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
chat_lock = threading.Lock()


def tap(vk=0, scan=0, flags=0):
    for up in (0, 2):
        i = INPUT(type=1, ki=KEYBDINPUT(vk, scan, flags | up, 0, None))
        u32.SendInput(1, ctypes.byref(i), ctypes.sizeof(i))


def game_window(pid):
    found = []

    @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def each(h, _):
        p = W.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(h) and not u32.GetWindow(h, 4):   # 주인 없는 보이는 창 = 게임 본창
            found.append(h)
        return True
    u32.EnumWindows(each, 0)
    return found[0] if found else None


def valid_cmd(text):
    """조합 명령어만 허락한다: sions 설명에 실제로 적혀 있는 글자여야 한다 (아무 글이나 게임 채팅에 치게 두지 않는다)."""
    return 2 <= len(text) <= 40 and '\n' not in text and any(
        text.lower() in (it.get('descr') or '').lower() for g in json.loads(SIONS).get('groups', []) for it in g['items'])


def game_chat(text):
    """게임 창을 앞으로 가져와 Enter - 글자 - Enter. 게임 창이 앞에 있을 때만 친다 (다른 창에 치면 안 된다)."""
    with chat_lock:
        pid = find_pid()
        h = pid and game_window(pid)
        if not h:
            return '게임 창을 못 찾았어요'
        if u32.IsIconic(h):
            u32.ShowWindow(h, 9)
        if u32.GetForegroundWindow() != h:
            u32.keybd_event(0x12, 0, 0, 0)   # Alt 를 눌렀다 떼야 윈도우가 다른 프로그램 창을 앞으로 보내 준다
            u32.SetForegroundWindow(h)
            u32.keybd_event(0x12, 0, 2, 0)
        for _ in range(30):
            if u32.GetForegroundWindow() == h:
                break
            time.sleep(0.05)
        else:
            return '게임 창을 앞으로 못 가져왔어요. 게임 화면을 한 번 누른 뒤 다시 해 주세요'
        time.sleep(0.25)
        tap(0x0D, 0x1C)                      # Enter: 채팅 열기
        time.sleep(0.15)
        for ch in text:
            tap(0, ord(ch), 4)               # 글자를 직접 넣는다: 한/영 상태와 상관없다
        time.sleep(0.1)
        if u32.GetForegroundWindow() != h:
            return '입력 도중 창이 바뀌어서 멈췄어요'
        tap(0x0D, 0x1C)
        return 'ok'


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
                nm = [x for o, x in game['names'] if o == state['owner']]
                if len(nm) == 1 and not cfg.get('my_name'):   # 여럿이 하는 판에서 처음 손으로 고름 = 내 이름을 배운다
                    cfg['my_name'] = nm[0]
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
        elif path == '/update':
            try:
                self_update(); self._send(200, b'ok', 'text/plain')
            except Exception as e:
                state['notice'] = f'업데이트 실패: {e}'; traceback.print_exc()
                self._send(500, str(e).encode(), 'text/plain; charset=utf-8')
        elif path == '/quit':
            self._send(200, b'bye', 'text/plain'); threading.Timer(0.3, quit_app).start()
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
        elif self.path == '/chat':   # 이 컴퓨터에서 연 우리 페이지만: 다른 사이트나 같은 공유기의 다른 기기가 게임에 글을 치게 두지 않는다
            text = body.decode('utf-8', 'replace').strip()
            mine = self.client_address[0] == '127.0.0.1' and self.headers.get('Origin') in (f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}')
            msg = '이 컴퓨터에서 연 페이지에서만 돼요' if not mine else game_chat(text) if valid_cmd(text) else '조합 명령어가 아니에요'
            self._send(200, msg.encode(), 'text/plain; charset=utf-8')
        elif self.path == '/overlay':
            spawn_overlay()
            self._send(200, b'ok', 'text/plain')
        else:
            self._send(404, b'', 'text/plain')

    def log_message(self, *a):
        pass


def open_page():
    import webbrowser
    webbrowser.open(f'http://127.0.0.1:{PORT}')


def quit_app():
    if TRAY:
        TRAY.remove()   # 아이콘을 지우고 나가야 트레이에 유령 아이콘이 안 남는다
    os._exit(0)


TRAY = None

if __name__ == '__main__':
    if '--overlay-only' in sys.argv:
        overlay(); sys.exit()
    lan = '--lan' in sys.argv   # 폰/다른 PC 에서 보기: 같은 와이파이 안에서만
    try:
        class Server(ThreadingHTTPServer):
            allow_reuse_address = False   # 윈도우는 기본값(True)이면 같은 포트를 두 프로그램이 동시에 잡는다 -> 중복 실행을 못 막음
        srv = Server(('0.0.0.0' if lan else '127.0.0.1', PORT), Handler)
    except OSError:
        # 이미 켜져 있다: 또 켜지 말고 페이지만 열어 준다 (두 번째 실행이 조용히 죽으면 켜졌는지 헷갈린다)
        print('이미 실행 중이에요. 페이지를 엽니다.')
        if '--no-browser' not in sys.argv:
            open_page()
        sys.exit()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if '--no-tray' not in sys.argv:
        import tray
        units = lambda: sum(n for k, n in state['counts'].items() if k >= 0)
        TRAY = tray.Tray(os.path.join(RES, 'icon.ico'),
                         lambda: f"원랜디 도우미 {VERSION} · {state['status']}" + (f" · 내 유닛 {units()}" if state['players'] else ''),
                         [('도우미 페이지 열기', open_page),
                          ('게임 위에 띄우기', spawn_overlay),
                          ('업데이트 확인', tray_update),
                          ('-', None),
                          (lambda: f"{state['status']}" + (f" · {state['owner'] + 1}번 슬롯 · 유닛 {units()}" if state['players'] else ''), None),
                          ('-', None),
                          ('프로그램 종료', quit_app)],
                         open_page)
        TRAY.start()
    SIONS = json.dumps(load_sions(), ensure_ascii=False).encode()
    threading.Thread(target=check_map_update, daemon=True).start()
    threading.Thread(target=check_update, daemon=True).start()
    state['startup'] = os.path.exists(STARTUP)
    threading.Thread(target=tracker, daemon=True).start()
    print(f'원랜디 자동 카운터 실행 중 - 브라우저에서 http://127.0.0.1:{PORT} 열기  (끄려면 트레이 아이콘 → 프로그램 종료)')
    if lan:
        import socket
        ip = socket.gethostbyname(socket.gethostname())
        print(f'폰에서 보기: 같은 와이파이에서 http://{ip}:{PORT}/?mini  (윈도우 방화벽이 물으면 "개인 네트워크" 허용)')
    if '--no-browser' not in sys.argv:
        open_page()
    if TRAY:
        time.sleep(0.5)
        state['tray'] = TRAY.ok
        TRAY.balloon('원랜디 도우미가 켜졌어요', '이 아이콘을 누르면 페이지가 열려요. 끄려면 오른쪽 클릭 → 프로그램 종료')
    last = None
    try:
        while True:
            s = f"[{state['status']}{' ' + str(state['scan']) + '%' if state['status'] == '첫 스캔 중' else ''}] 내 슬롯={state['owner'] + 1}번{'(자동)' if state['auto'] else ''} 유닛={sum(n for k, n in state['counts'].items() if k >= 0)} 플레이어별={state['players']}"
            if s != last:
                print(time.strftime('%H:%M:%S'), s, flush=True); last = s
            time.sleep(1)
    except KeyboardInterrupt:
        quit_app()
