"""원랜디 자동 카운터.

워크3 메모리를 '읽기만' 해서 내 유닛 수를 세고, http://127.0.0.1:8765 전용 페이지에 보여준다.
조합식·유닛 수치는 sions 조합 도우미 글에서 받아온다(sions_cache.json 에 캐시).
새 맵 버전이 보이면 mapping.json 을 스스로 다시 만든다(mapsync.py).

게임이 멈추지 않도록: 1MB씩 나눠 읽고 중간중간 쉰다. 첫 스캔만 20초~1분, 이후엔 2초마다 조금씩.

옵션: --lan (폰에서 보기)  --no-browser  --port=NNNN  --overlay-only (게임 위 작은 창만 띄움)
"""
import collections, ctypes, ctypes.wintypes as W, json, os, struct, subprocess, sys, threading, time, traceback, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.7.16'
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
# typeId, 주인 플레이어 번호, 1 = 제거됨(조합 재료로 사라짐).
# 주인은 +0x1c0. +0x58 은 주인이 아니라 '색 번호'다: 혼자 하는 판에선 둘이 같지만, 여럿이 하는 판에선 맵이 색을 바꿔서
# (2번 플레이어의 유닛이 색 3) 내 유닛이 남의 슬롯으로 잡혔다.
OFF_TYPE, OFF_OWNER, OFF_GONE = 0x70, 0x1c0, 0x274
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


MAPPING, CODES, REV = {}, {}, {}   # REV: sions id -> [메모리에 적힌 코드]


def apply_mapping(mp):
    global MAPPING, CODES
    MAPPING = mp
    CODES = {struct.pack('<I', int.from_bytes(c.encode(), 'big')): sid for c, sid in {**mp, **WISP_CODES}.items()}
    state['managed'] = sorted(set(mp.values()))
    REV.clear()
    for code, sid in CODES.items():
        REV.setdefault(sid, []).append(code)


state = {'status': '워크3 기다리는 중', 'counts': {}, 'players': {}, 'ts': 0, 'scan': 0,
         'owner': cfg['owner'], 'auto': cfg['auto'], 'managed': [], 'notice': '', 'data_ver': 1,
         'map_version': cfg['map_version'], 'big_key': cfg.get('big_key', '마우스 옆버튼'), 'big_alpha': cfg.get('big_alpha', 100), 'version': VERSION, 'frozen': FROZEN, 'update': None, 'startup': False, 'soon': None}
apply_mapping(json.load(open(res_or_local('mapping.json'), encoding='utf-8')))
COMBOS = {}   # 버튼으로 조합하는 유닛: {결과 sions id: [[버튼을 가진 유닛 코드, 단축키], ...]}


def apply_combos(c):
    global COMBOS
    COMBOS = {int(k): v for k, v in c.items()}
    state['combos'] = sorted(COMBOS)
    state['costs'] = {k: v[0][2:4] for k, v in COMBOS.items() if v and len(v[0]) >= 4 and any(v[0][2:4])}   # {id: [목재, 골드]}


try:
    apply_combos(json.load(open(res_or_local('combos.json'), encoding='utf-8')))
except Exception:
    apply_combos({})
SIONS = b'{}'
summary = {}           # 페이지가 계산해서 보내 주는 요약 (작은 창·판 기록용)
game = {'start': None, 'timeline': [], 'tl_ts': 0, 'sum_ts': 0}
LIVE = {'g': None}     # 지금 붙어 있는 게임 (페이지에서 조합을 누르면 쓴다)
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
        combos = mapsync.map_combos(path, mp)
        json.dump(combos, open(os.path.join(HERE, 'combos.json'), 'w', encoding='utf-8'), ensure_ascii=False)
        apply_combos(combos)
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
        tt, sel = self.vtable_of(img, base, b'CTextTagManager'), self.vtable_of(img, base, b'CSelectionWar3')
        self.tt_vt, self.sel_vt = tt and struct.pack('<Q', tt), sel and struct.pack('<Q', sel)
        self.pl_off, self.tt = None, []   # 내 플레이어를 가리키는 칸의 위치, CTextTagManager 주소들
        self.res = None                    # 내 자원 기록(골드·목재)이 줄지어 있는 곳
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

    def unit_ptrs(self):
        """게임의 유닛 목록에 든 주소들. 목록이 사라졌으면 None."""
        obj, off = self.world
        d = self.read(obj, off + 24)
        if not d or len(d) < off + 24 or d[:8] != self.wf_vt:
            return None
        cnt, ptr, cap = struct.unpack_from('<I4xQI', d, off)
        if cnt > cap or cap > 200000:
            return None
        if not cnt:
            return ()
        a = self.read(ptr, cnt * 8)
        return struct.unpack(f'<{cnt}Q', a) if a and len(a) == cnt * 8 else None

    def local_player(self):
        """내 플레이어(CPlayerWar3) 주소. CWorldFrameWar3 안에 플레이어를 가리키는 칸이 딱 하나 있고 그게 나다
        (3.0.0.24268 에선 +0x2c8). 위치는 박아 두지 않고 내용으로 찾는다."""
        d = self.pl_vt and self.read(self.world[0], 0x800) or b''
        for off in ([self.pl_off] if self.pl_off else range(8, len(d) - 7, 8)):
            p = struct.unpack_from('<Q', d, off)[0] if off + 8 <= len(d) else 0
            if 0x10000 < p < 0x7FFFFFFFFFFF and p % 8 == 0 and self.read(p, 8) == self.pl_vt:
                self.pl_off = off
                return p
        return None

    def local_slot(self):
        p = self.local_player()
        s = p and self.read(p + OFF_SLOT, 1)
        return s[0] if s and s[0] < 24 else None

    def view_matrix(self):
        """월드 좌표 -> 화면 변환 행렬(4x4, [x y z 1]·M). 떠다니는 글자를 그리는 CTextTagManager 가 들고 있다.
        카메라가 보는 점을 넣으면 깊이가 카메라 거리로 나와야 진짜다 (아니면 None: 엉뚱한 곳을 누르지 않는다)."""
        cam = self.read(self.world[0] + OFF_CAM, 8)
        c = cam and self.read(struct.unpack('<Q', cam)[0], OFF_CAM_DIST + 4)
        if not c or len(c) < OFF_CAM_DIST + 4:
            return None
        tx, ty, tz = struct.unpack_from('<3f', c, OFF_CAM_TARGET)
        dist = struct.unpack_from('<f', c, OFF_CAM_DIST)[0]
        for a in self.tt:
            m = self.read(a + OFF_VP, 64)
            M = m and len(m) == 64 and struct.unpack('<16f', m)
            if M and all(v == v and abs(v) < 1e7 for v in M) and dist > 100 and abs(tx * M[3] + ty * M[7] + tz * M[11] + M[15] - dist) < dist * 0.02:
                return M
        return None

    def world_units(self):
        """게임이 가진 유닛 목록을 그대로 읽는다 -> [(주인, sions id)], 목록이 사라졌으면 None.
        메모리를 훑지 않아서 빠짐이 없고, 한 번에 수백 KB 만 읽는다."""
        ptrs = self.unit_ptrs()
        if ptrs is None:
            return None
        found, self.samples, self.neutral, self.anchor = [], [], [], {}
        for q in ptrs:
            o = self.read(q, SZ)
            if not o or len(o) < SZ or o[:8] != self.vt:
                continue
            if len(self.samples) < 400: self.samples.append(o)
            code, owner = o[OFF_TYPE:OFF_TYPE + 4], struct.unpack_from('<I', o, OFF_OWNER)[0]
            sid = CODES.get(code)
            if code == b'U50h':                  # h05U '연구소 효과': 플레이어마다 자기 구역 한가운데에 하나 서 있다
                self.anchor[owner] = q
            if sid is not None and struct.unpack_from('<I', o, OFF_GONE)[0] == 0:
                found.append((owner, sid))
                if owner >= 24:                  # 중립
                    self.neutral.append((q, sid))
        return found

    def pos(self, p):
        """유닛의 월드 좌표 (x, y, z)."""
        o = p and self.read(p + OFF_SPRITE, 8)
        d = o and self.read(struct.unpack('<Q', o)[0] + OFF_POS, 12)
        return struct.unpack('<3f', d) if d and len(d) == 12 else None

    def reward(self, me):
        """내 구역 한가운데에 서 있는 중립 유닛의 sions id: 몹을 150마리쯤 잡으면 받는 보상 유닛. 없으면 None."""
        a = self.pos(self.anchor.get(me))
        for q, sid in self.neutral if a else ():
            b = self.pos(q)
            if b and abs(a[0] - b[0]) < 64 and abs(a[1] - b[1]) < 64:
                return sid
        return None

    def find_names(self):
        """플레이어 이름 [(슬롯, 이름)]. CPlayerWar3: 슬롯 번호 +0x6a, 이름 주소 +0xa0, 길이 +0xa8 (3.0.0.24268).
        사람 플레이어만 이름이 있다. 1MB 이하 구역만 훑어서 6초쯤 걸린다 -> 판마다 한 번만.
        같은 길에 CTextTagManager(화면 변환 행렬을 가진 물체)도 찾아 둔다."""
        out, naps, tt, res = [], 0, [], []
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
            i = d.find(self.tt_vt) if d and self.tt_vt else -1
            while i >= 0:
                if i % 8 == 0:
                    tt.append(base + i)
                i = d.find(self.tt_vt, i + 1)
            i = d.find(RES_SIG[0]) if d else -1   # 자원 기록: 번호 (1,1) (2,2) (3,3) (4,4) 가 0xe0 간격으로 줄지어 있다
            while i >= 0:
                if i % 8 == 0 and all(d[i + k * RES_STEP:i + k * RES_STEP + 16] == RES_SIG[k] for k in range(1, 8)):
                    res.append(base + i)
                i = d.find(RES_SIG[0], i + 1)
        self.tt = tt
        # ponytail: 이 기록은 혼자 하는 판에서만 내 것으로 확인됐다. 여럿이 하는 판에서도 하나만 나오는데 남의 값이었다
        # (목재 5개가 있는데 1개로 읽어 조합을 막음) -> 사람이 나 혼자일 때만 쓴다. 여럿일 때 내 자원이 어디 있는지는 아직 못 찾았다.
        self.res = res[0] if len(res) == 1 and len({n for _, n in out}) == 1 else None
        return out

    def resources(self):
        """(골드, 목재). 게임은 10배 한 값으로 들고 있다. 못 읽으면 None."""
        d = self.res and self.read(self.res + RES_STEP, RES_STEP * 2)
        if not d or len(d) < RES_STEP * 2 or d[:16] != RES_SIG[1] or d[RES_STEP:RES_STEP + 16] != RES_SIG[2]:
            return None
        return struct.unpack_from('<i', d, 0xb0)[0] // 10, struct.unpack_from('<i', d, RES_STEP + 0xb0)[0] // 10

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
    주인·제거 표시 위치는 typeId 에서 같은 거리만큼 옮겨졌다고 가정한다(주인 +0x150, 제거 표시 +0x204)."""
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


def read_names(g):
    """슬롯 고르는 칸에 보여 줄 이름 + 화면 변환 행렬 위치. 6초쯤 걸려서 추적과 따로 돈다."""
    for _ in range(6):   # 아직 안 채워졌으면 조금 뒤 다시
        try:
            names = g.find_names()
        except Exception:
            traceback.print_exc(); return
        if names or not g.alive():
            state['names'] = {o: n for o, n in names}
            return
        time.sleep(20)


RECENT = collections.deque(maxlen=3)   # 최근 세 번 읽은 (슬롯, 유닛 수)


def publish(found, me=None):
    """me: 게임이 알려 준 내 슬롯. 못 읽었으면(None) 예전처럼 사람 후보가 하나일 때만 그 슬롯."""
    counts, players = {}, {}
    for owner, sid in found:
        if sid >= 0:
            players[owner] = players.get(owner, 0) + 1
    # ponytail: slot 7 = this map's showcase computer (200+ units), 24+ = neutral.
    cand = [o for o, n in humans(players).items() if n < 150]
    pick = me if me is not None else cand[0] if len(cand) == 1 else None
    if state['auto'] and pick is not None and pick != state['owner']:
        state['owner'] = pick; save_cfg()
    for owner, sid in found:
        if owner == state['owner']:
            counts[sid] = counts.get(sid, 0) + 1
    if humans(players) and not game['start']:
        game['start'] = time.time()
    # 위습이 유닛으로 바뀌는 순간 게임에 그 유닛이 0.4초쯤 둘 있다가 하나가 사라진다. 그대로 보이면 '+우솝 ×2' 로 뜨고
    # 재료가 다 모인 것처럼 보였다가 만다 -> 늘어난 수는 0.5초(세 번 읽는 동안) 버텨야 인정한다. 줄어든 수는 바로.
    if RECENT and RECENT[-1][0] != state['owner']:
        RECENT.clear()
    RECENT.append((state['owner'], counts))
    steady = {sid: n for sid in counts for n in [min(c.get(sid, 0) for _, c in RECENT)] if n > 0}
    state.update(counts=steady, players=players, ts=time.time())


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
    empty_since, bad, me = None, 0, None
    LIVE['g'] = g
    threading.Thread(target=read_names, args=(g,), daemon=True).start()
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
        if me is None:
            me = g.local_slot()
        publish(found, me)
        state['soon'] = g.reward(state['owner'])
        r = g.resources()
        state.update(gold=r and r[0], wood=r and r[1])
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
    chat_lock.acquire(timeout=120)   # 게임에서 조합하는 중이면 끝날 때까지 기다린다: 마우스·키를 쥔 채로 꺼지면 안 된다
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


def front(h):
    u32.keybd_event(0x12, 0, 0, 0)   # Alt 를 눌렀다 떼야 윈도우가 다른 프로그램 창을 앞으로 보내 준다
    u32.SetForegroundWindow(h)
    u32.keybd_event(0x12, 0, 2, 0)


def focus_game():
    """게임 창을 앞으로 가져온다 -> 창 핸들, 안 되면 까닭(글)."""
    g = LIVE['g']
    pid = g.pid if g and g.alive() else find_pid()   # find_pid 는 tasklist 를 돌려서 느리다(0.2~0.4초): 붙어 있는 게임이 있으면 그걸 쓴다
    h = pid and game_window(pid)
    if not h:
        return '게임 창을 못 찾았어요'
    if u32.GetForegroundWindow() == h:
        return h
    if u32.IsIconic(h):
        u32.ShowWindow(h, 9)
    # 마우스를 먼저 게임 화면 가운데로: 다른 모니터에 둔 채 넘어가면 게임이 '화면 끝에 마우스'로 알고 화면을 쭉 밀어 버린다
    rc, mid = W.RECT(), W.POINT()
    u32.GetClientRect(h, ctypes.byref(rc))
    mid.x, mid.y = rc.right // 2, rc.bottom * 2 // 5
    u32.ClientToScreen(h, ctypes.byref(mid))
    u32.SetCursorPos(mid.x, mid.y)
    front(h)
    for _ in range(30):
        if u32.GetForegroundWindow() == h:
            time.sleep(0.25)
            return h
        time.sleep(0.05)
    return '게임 창을 앞으로 못 가져왔어요. 게임 화면을 한 번 누른 뒤 다시 해 주세요'


# 3.0.0.24268 기준 위치. 하나라도 어긋나면 확인 단계에서 걸려 아무것도 누르지 않는다.
OFF_SLOT = 0x6a                             # CPlayerWar3: 슬롯 번호
RES_STEP = 0xe0                             # 자원 기록 한 칸의 크기. 값(10배)은 칸 +0xb0. 2번 칸 = 골드, 3번 칸 = 목재
RES_SIG = [struct.pack('<IIQ', k, k, 0) for k in range(1, 9)]   # 칸 머리: 번호 두 번 + 0. 1~8번이 줄지어 있어야 진짜다
OFF_SEL, OFF_SEL_NOW, OFF_SEL_SYNC = 0x168, 0x3b0, 0x348   # 플레이어 -> 선택 물체 -> 방금 고른 유닛 / 게임이 확정한 유닛
OFF_SPRITE, OFF_POS = 0x60, 0x170           # 유닛 -> 그리기 물체 -> 위치 (x, y, z)
OFF_CAM, OFF_CAM_TARGET, OFF_CAM_DIST = 0x228, 0xc0, 0x108   # CWorldFrameWar3 -> CCamera -> 보는 점 / 거리
OFF_VP = 0x58                               # CTextTagManager: 화면 변환 행렬
OFF_HOVER = 0x468                           # CWorldFrameWar3: 지금 마우스 아래에 있는 유닛
OFF_MINIMAP, OFF_MM_RECT, OFF_MM_SCREEN, OFF_MM_WORLD = 0x2f0, 0x1e0, 0x234, 0x290   # 월드프레임 -> CMinimap: 자리(아래,왼,위,오른) / 화면 범위 / 맵 범위
MINI_CAL = {}                               # (게임 pid, 창 크기) -> 미니맵 눈금 (px, py, X, Y, kx, ky, 맵 범위)
# 유닛 종류(코드) -> 그 유닛 몸에서 클릭이 먹는 자리. 맵 위 위치가 아니라 '유닛 발끝에서 얼마나 떨어진 곳을 눌러야 하나'라서
# 유닛이 어디 서 있든 같다(모델마다 다를 뿐). 카메라 거리 4000 일 때의 픽셀로 적어 두고, 다른 거리에선 비례로 바꿔 쓴다.
HITS_PATH = os.path.join(HERE, 'hits.json')
try:
    HIT_AT = {k: tuple(v) for k, v in json.load(open(HITS_PATH, encoding='utf-8')).items()}
except Exception:
    HIT_AT = {}
u32.WindowFromPoint.argtypes, u32.WindowFromPoint.restype = [W.POINT], W.HWND
u32.GetAncestor.argtypes, u32.GetAncestor.restype = [W.HWND, W.UINT], W.HWND
u32.GetClientRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
u32.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
u32.keybd_event.argtypes = [W.BYTE, W.BYTE, W.DWORD, ctypes.c_void_p]
u32.ClientToScreen.argtypes = [W.HWND, ctypes.POINTER(W.POINT)]


def game_steps(steps):
    """페이지에서 누른 조합을 게임에서 차례로 한다. steps = [[만들 유닛의 sions id, 채팅 명령어 또는 '']].
    한 단계가 끝나면 그 유닛이 실제로 생겼는지 확인하고 다음으로 간다 (안 생겼으면 거기서 멈춘다)."""
    if not chat_lock.acquire(blocking=False):   # 버튼을 두 번 눌렀을 때 줄 서서 또 돌면, 재료가 남아 있는 한 한 번 더 조합해 버린다
        return '이미 조합하는 중이에요'
    try:
        if time.time() - LIVE.get('done', 0) < 0.8:   # 방금 끝났다: 페이지가 아직 예전 유닛 수로 순서를 짰을 수 있다
            return '방금 조합이 끝났어요. 잠깐 뒤에 다시 눌러 주세요'
        g = LIVE['g']
        if not g or not g.world or not g.alive():
            return '게임 중이 아니에요'
        try:
            return _steps(g, steps)
        finally:
            LIVE['done'] = time.time()
    finally:
        chat_lock.release()


def _steps(g, steps):
    prev, old, ui, cam0 = u32.GetForegroundWindow(), W.POINT(), None, None
    big = big_window()
    big = big if big_shown(big) else None   # 큰 창이 게임 화면을 가리니 조합하는 동안 감춘다
    if big:
        big_show(big, False)
    u32.GetCursorPos(ctypes.byref(old))
    try:
        me = g.local_slot()
        if me is None:
            return '내 플레이어를 못 찾았어요 (게임이 업데이트된 것 같아요)'
        # Esc 를 '누르고 있을 때'만 보면 단계 사이의 짧은 순간에 맞춰 눌러야 멈춘다: 지난 확인 뒤로 한 번이라도 눌렸으면 멈춘다
        esc = lambda: u32.GetAsyncKeyState(0x1B) & 0x8001
        esc()                                    # 시작 전에 눌렸던 기록은 지운다
        for i, (sid, cmd) in enumerate(steps):
            t0 = time.time()
            if esc():
                return f'Esc 로 멈췄어요 ({i}/{len(steps)}단계까지 함)'
            before = mine_of(g, me, sid)
            wisp = cmd == '@wisp'                # 흔함선택위습을 그 흔함의 자리로 보내는 단계
            chat = bool(cmd) and not wisp
            if not chat:
                ui = Clicker(g, me)
                cam0 = cam0 or (not ui.err and ui.cam()) or None   # 처음 보던 곳: 끝나면 돌아온다
            msg = _chat(cmd) if chat else ui.err or (ui.wisp(sid) if wisp else ui.combine(COMBOS[sid]))
            t1 = time.time()
            slow = not cmd and any(k == 'B' for _, k, *_ in COMBOS[sid])   # 변화는 시전에 10초가 걸린다
            done = msg != 'ok'
            # 결과 유닛이 생기거나 누른 재료가 사라질 때까지 (최대 3초, 변화는 13초, 위습은 걸어가야 해서 12초)
            for k in range(0 if done else 430 if slow else 400 if wisp else 100):
                time.sleep(0.03)
                if esc():                        # 기다리는 중에도 멈출 수 있게 (위습·변화는 10초 넘게 기다린다)
                    return f'Esc 로 멈췄어요 ({i + 1}/{len(steps)}단계까지 함)'
                if (not chat and not slow and not ui.unit(ui.last)) or (k % 3 == 2 and mine_of(g, me, sid) - before):
                    done = True
                    break
            if wisp and done and msg == 'ok':
                time.sleep(0.5)                  # 위습이 바뀐 직후 0.4초쯤은 유닛이 둘로 보인다: 가라앉은 뒤 다음으로
            print(f'조합 {i + 1}/{len(steps)} id={sid} {cmd or "버튼"}: {msg}  (고르고 누르기 {t1 - t0:.2f}초, 결과 기다림 {time.time() - t1:.2f}초)')
            if msg != 'ok':
                return msg if len(steps) == 1 else f'{i + 1}번째 조합에서 멈췄어요: {msg}'
            if not done:
                print(f'조합 {i + 1}/{len(steps)} id={sid}: 결과 유닛이 안 생김')
                if chat and i + 1 == len(steps):
                    return f'명령어({cmd})는 입력했는데 새 유닛이 안 보여요. 게임에서 조합됐는지 확인해 주세요'
                return (f'{i + 1}번째 조합이 게임에서 안 됐어요. ' if len(steps) > 1 else '게임에서 조합이 안 됐어요. ') + '목재·골드·재료를 확인해 주세요'
            time.sleep(0.05)
        return 'ok'
    except Exception as e:
        traceback.print_exc()
        return f'조합하지 못했어요: {e}'
    finally:   # 화면을 처음 보던 곳으로 -> 원래 창(페이지)으로 -> 마우스 제자리. 창보다 마우스를 먼저 돌리면 게임이 화면을 밀어 버린다
        if cam0 and ui and not ui.err:
            ui.back(cam0)
        if big:
            big_show(big, True)
        gh, r = game_window(g.pid), W.RECT()
        if prev and prev != gh and prev != big and prev != u32.GetForegroundWindow():   # 큰 창에서 눌렀으면 초점은 게임에 둔다
            front(prev)
            for _ in range(10):
                if u32.GetForegroundWindow() != gh:
                    break
                time.sleep(0.05)
        u32.GetWindowRect(gh, ctypes.byref(r))
        inside = r.left <= old.x < r.right and r.top <= old.y < r.bottom
        stuck = u32.GetForegroundWindow() == gh and not inside
        print(f'끝: 게임이 앞={u32.GetForegroundWindow() == gh} 마우스 원래 자리가 게임 안={inside}')
        if not stuck:   # 게임이 앞에 남았는데 마우스만 다른 모니터로 보내면 게임이 화면을 그쪽으로 계속 민다
            u32.SetCursorPos(old.x, old.y)


def name_of(p, g):
    return (g.read(p + OFF_TYPE, 4) or b'')[::-1]


def mine_of(g, me, sid):
    """내 유닛 중 이 sions id 인 것들의 주소."""
    out = set()
    for p in g.unit_ptrs() or ():
        o = g.read(p, SZ)
        if (o and len(o) == SZ and o[:8] == g.vt and CODES.get(o[OFF_TYPE:OFF_TYPE + 4]) == sid
                and struct.unpack_from('<I', o, OFF_OWNER)[0] == me and struct.unpack_from('<I', o, OFF_GONE)[0] == 0):
            out.add(p)
    return out


class Clicker:
    """게임 화면에서 내 유닛을 눌러 고르고 단축키를 누른다. 누른 뒤에는 늘 게임에서 '지금 골라진 유닛'을 읽어 확인한다."""

    def __init__(self, g, me):
        self.g, self.me, self.err, self.last = g, me, None, 0
        pl = g.local_player()
        self.sel = pl and self.rq(pl + OFF_SEL)
        if not self.sel or not g.sel_vt or g.read(self.sel, 8) != g.sel_vt:
            self.err = '선택 정보를 못 찾았어요 (게임이 업데이트된 것 같아요)'
        elif not g.view_matrix():
            self.err = '화면 정보를 아직 못 찾았어요. 몇 초 뒤 다시 눌러 주세요'
        else:
            h = focus_game()
            if isinstance(h, str):
                self.err = h
            else:
                self.h, self.rc = h, W.RECT()
                u32.GetClientRect(h, ctypes.byref(self.rc))

    def rq(self, a):
        return struct.unpack('<Q', self.g.read(a, 8) or bytes(8))[0]

    def unit(self, p):
        """내 살아 있는 유닛이면 그 내용, 아니면 None."""
        o = self.g.read(p, SZ) if p else None
        ok = (o and len(o) == SZ and o[:8] == self.g.vt and struct.unpack_from('<I', o, OFF_OWNER)[0] == self.me
              and struct.unpack_from('<I', o, OFF_GONE)[0] == 0)
        return o if ok else None

    def where(self, p):
        return self.g.pos(p)

    def screen(self, xyz):
        """월드 좌표 -> 게임 창 안쪽 화면 좌표."""
        M = self.g.view_matrix()
        if not M or not xyz:
            return None
        x, y, z = xyz
        w = x * M[3] + y * M[7] + z * M[11] + M[15]
        if w <= 1:
            return None
        return (((x * M[0] + y * M[4] + z * M[8] + M[12]) / w * 0.5 + 0.5) * self.rc.right,
                (0.5 - (x * M[1] + y * M[5] + z * M[9] + M[13]) / w * 0.5) * self.rc.bottom)

    def body(self, p):
        xyz = self.where(p)
        return xyz and self.screen((xyz[0], xyz[1], xyz[2] + 25))   # 발끝보다 조금 위

    def safe(self, s):
        # 화면 가운데-왼쪽 상자만 누른다. 오른쪽 위엔 점수판, 아래엔 조작판·미니맵, 맨 위엔 자원 줄이 있어서
        # 거기 선 유닛은 좌표가 맞아도 눌리지 않는다. 밖에 있으면 aim() 이 화면을 옮겨 상자 안으로 데려온다.
        return s and self.rc.right * 0.10 < s[0] < self.rc.right * 0.64 and self.rc.bottom * 0.18 < s[1] < self.rc.bottom * 0.68

    def cam(self):
        """카메라가 보는 점 (x, y)."""
        c = self.g.read(self.rq(self.g.world[0] + OFF_CAM) + OFF_CAM_TARGET, 8)
        return struct.unpack('<2f', c) if c and len(c) == 8 else None

    def steer(self, want, secs):
        """want() 가 알려 주는 방향키를 누르고 있는다. None 을 돌려주면 다 온 것(True). 시간이 다 되거나 게임이 뒤로 가면 False."""
        held, end = None, time.time() + secs
        try:
            while time.time() < end and u32.GetForegroundWindow() == self.h:
                vk = want()
                if held and vk != held:
                    u32.keybd_event(held, u32.MapVirtualKeyW(held, 0), 3, 0); held = None
                if vk is None:
                    time.sleep(0.15 if held else 0.04)   # 방향키로 밀던 화면이 멈출 때까지
                    if want() is None:
                        return True
                    continue
                u32.keybd_event(vk, u32.MapVirtualKeyW(vk, 0), 1, 0); held = vk   # 누르고 있는 동안 계속 보낸다 (키 반복)
                time.sleep(0.03)
        finally:
            if held:
                u32.keybd_event(held, u32.MapVirtualKeyW(held, 0), 3, 0)
        return False

    def mini_click(self, px, py):
        pt = W.POINT(round(px), round(py))
        u32.ClientToScreen(self.h, ctypes.byref(pt))
        if u32.GetForegroundWindow() != self.h or u32.GetAncestor(u32.WindowFromPoint(pt), 2) != self.h:
            return False
        u32.SetCursorPos(pt.x, pt.y)
        u32.mouse_event(1, 1, 0, 0, 0)
        u32.mouse_event(1, -1, 0, 0, 0)
        time.sleep(0.06)
        self.press()
        return True

    def calibrate(self):
        """미니맵 두 군데를 눌러 '미니맵 픽셀 -> 월드 좌표' 눈금을 맞춘다 (게임마다 한 번). 안 되면 None."""
        d = self.g.read(self.rq(self.g.world[0] + OFF_MINIMAP), OFF_MM_WORLD + 16)
        if not d or len(d) < OFF_MM_WORLD + 16:
            return None
        bottom, left, top, right = struct.unpack_from('<4f', d, OFF_MM_RECT)
        sl, st, sr = struct.unpack_from('<3f', d, OFF_MM_SCREEN)
        wb = struct.unpack_from('<4f', d, OFF_MM_WORLD)
        if not (0 <= left < right <= 0.8 and 0 <= bottom < top <= 0.6 and sl <= 0 < sr and 0.5 < st < 0.7 and wb[0] < wb[2] and wb[1] < wb[3]):
            return None
        Wd, Ht = self.rc.right, self.rc.bottom
        # 조작판이 화면 가운데 4:3 에 놓였든 좌우로 늘어났든 둘 다에서 미니맵 안쪽인 구간만 누른다
        lo, hi = max((left - sl) / (sr - sl), left / 0.8) * Wd, min((right - sl) / (sr - sl), right / 0.8) * Wd
        y0, y1 = (1 - top / st) * Ht, (1 - bottom / st) * Ht
        if hi - lo < 20:
            return None
        pts, got = [(lo + (hi - lo) * 0.3, y0 + (y1 - y0) * 0.35), (lo + (hi - lo) * 0.7, y0 + (y1 - y0) * 0.65)], []
        for px, py in pts:
            if not self.mini_click(px, py):
                return None
            time.sleep(0.2)
            got.append(self.cam())
        if not all(got):
            return None
        kx, ky = (got[1][0] - got[0][0]) / (pts[1][0] - pts[0][0]), (got[1][1] - got[0][1]) / (pts[1][1] - pts[0][1])
        if not (5 < kx < 500 and -500 < ky < -5):      # 눌러도 화면이 안 따라왔다
            return None
        return (pts[0][0], pts[0][1], got[0][0], got[0][1], kx, ky, wb)

    def jump(self, x, y):
        """화면을 월드 (x, y) 로 한 번에 옮긴다: 미니맵을 누른다. 안 되면 False (그땐 방향키로 민다)."""
        key = (self.g.pid, self.rc.right, self.rc.bottom)
        cal = MINI_CAL.get(key) or self.calibrate()
        if not cal:
            return False
        MINI_CAL[key] = cal
        px, py, X, Y, kx, ky, wb = cal
        x, y = min(max(x, wb[0]), wb[2]), min(max(y, wb[1]), wb[3])
        c0 = c = self.cam()
        if not self.mini_click(px + (x - X) / kx, py + (y - Y) / ky):
            return False
        for _ in range(20):
            time.sleep(0.02)
            c = self.cam()
            if c and abs(c[0] - x) < 400 and abs(c[1] - y) < 400:
                return True
        if c and c0 and abs(c[0] - c0[0]) + abs(c[1] - c0[1]) < 50:
            MINI_CAL.pop(key, None)                    # 화면이 아예 안 움직였다: 눈금을 다음에 다시 맞춘다
        return False                                   # 움직이긴 했는데 못 닿았다 = 화면이 갈 수 없는 곳(맵 가장자리)

    def aim(self, p):
        """유닛이 누를 수 있는 자리에 오게 화면을 옮긴다: 미니맵을 눌러 한 번에, 모자라면 방향키로. 화면에 들어왔으면 True."""
        rc = self.rc

        def want():
            s = self.body(p)
            if s:
                return (0x28 if s[1] > rc.bottom * 0.58 else 0x26 if s[1] < rc.bottom * 0.28 else
                        0x27 if s[0] > rc.right * 0.58 else 0x25 if s[0] < rc.right * 0.16 else None)
            xyz, c = self.where(p), self.cam()       # 카메라 뒤쪽이라 화면 좌표가 없다: 보는 점보다 남쪽이면 아래로
            return (0x28 if xyz[1] < c[1] else 0x26) if xyz and c else 0x28
        xyz, secs = self.where(p), 4
        if want() is None:
            return bool(self.safe(self.body(p)))       # 이미 누를 수 있는 자리에 있다
        if xyz:
            self.jump(xyz[0], xyz[1])
            if (self.g.pid, rc.right, rc.bottom) in MINI_CAL:
                secs = 1.5                             # 미니맵으로 옮겼으면 방향키는 마무리만: 못 가는 곳에서 오래 끌지 않는다
        return self.steer(want, secs) and bool(self.safe(self.body(p)))

    def back(self, to):
        """화면을 처음 보던 곳으로 되돌린다: 미니맵을 눌러 한 번에, 안 되면 방향키로 (카메라가 북쪽을 위로 본다고 가정)."""
        c = self.cam()
        if c and abs(c[0] - to[0]) < 150 and abs(c[1] - to[1]) < 150 or self.jump(to[0], to[1]):
            return
        def want():
            c = self.cam()
            if not c:
                return None
            dx, dy = to[0] - c[0], to[1] - c[1]
            return 0x26 if dy > 150 else 0x28 if dy < -150 else 0x27 if dx > 150 else 0x25 if dx < -150 else None
        self.steer(want, 3)

    def move(self, s):
        """마우스를 화면 좌표로 옮긴다. 조작판·점수판 쪽, 다른 창에 가려진 곳, 게임이 앞에 없을 때는 안 옮긴다(False)."""
        if not self.safe(s):
            return False
        pt = W.POINT(int(s[0]), int(s[1]))
        u32.ClientToScreen(self.h, ctypes.byref(pt))
        if u32.GetAncestor(u32.WindowFromPoint(pt), 2) != self.h or u32.GetForegroundWindow() != self.h:
            return False
        u32.SetCursorPos(pt.x, pt.y)
        u32.mouse_event(1, 1, 0, 0, 0)    # 1픽셀 흔들기: 게임이 '마우스가 움직였다'는 신호를 받아야 커서 위치를 새로 잡는다
        u32.mouse_event(1, -1, 0, 0, 0)
        return True

    def press(self, right=False):
        u32.mouse_event(8 if right else 2, 0, 0, 0, 0)
        time.sleep(0.04)
        u32.mouse_event(0x10 if right else 4, 0, 0, 0, 0)

    def click(self, s, right=False):
        if not self.move(s):
            return False
        time.sleep(0.15)
        self.press(right)
        return True

    def hover(self):
        """지금 마우스 아래에 있는 유닛 (게임이 적어 둔 것)."""
        return self.rq(self.g.world[0] + OFF_HOVER)

    def point_at(self, p, ok):
        """마우스를 유닛 p 둘레에서 조금씩 옮기며, 게임이 '마우스 아래 유닛'을 ok 인 유닛이라고 할 때까지 찾는다.
        찾으면 True (마우스는 그 자리에 있다). 유닛마다 눌리는 범위가 달라서(나미는 15픽셀 남짓) 좌표만 믿고 누르면 빗나간다."""
        s = self.body(p)
        if not s:
            return False
        # 눌리는 자리는 보통 발끝보다 15픽셀쯤 위. 거기서부터 촘촘히(6픽셀), 바깥은 성기게(9픽셀) 60픽셀까지 넓혀 간다.
        near = lambda d: d[0] ** 2 + d[1] ** 2
        pts = (sorted(((dx, dy) for dx in range(-30, 31, 6) for dy in range(-30, 31, 6) if dx * dx + dy * dy <= 900), key=near)
               + sorted(((dx, dy) for dx in range(-63, 64, 9) for dy in range(-63, 64, 9) if 900 < dx * dx + dy * dy <= 3969), key=near))
        code = (self.g.read(p + OFF_TYPE, 4) or b'')[::-1].decode('latin1')
        d = self.g.read(self.rq(self.g.world[0] + OFF_CAM) + OFF_CAM_DIST, 4)
        zoom = max(struct.unpack('<f', d)[0], 500) / 4000 if d and len(d) == 4 else 1   # 멀리서 볼수록 같은 거리가 적은 픽셀
        if code in HIT_AT:                        # 이 종류가 지난번에 눌렸던 자리부터 본다: 대개 첫 번에 맞는다
            pts.insert(0, (round(HIT_AT[code][0] / zoom), round(HIT_AT[code][1] / zoom)))
        for dx, dy in pts:
            if self.move((s[0] + dx, s[1] + dy - 15)):
                time.sleep(0.025)
                if ok(self.hover()):
                    time.sleep(0.03)              # 한 박자 늦게 읽혔을 수 있으니 같은 자리에서 다시 확인
                    if ok(self.hover()):
                        new = (round(dx * zoom), round(dy * zoom))
                        if HIT_AT.get(code) != new:
                            HIT_AT[code] = new
                            try:
                                json.dump(HIT_AT, open(HITS_PATH, 'w', encoding='utf-8'))
                            except OSError:
                                pass
                        return True
        return False

    def selected(self, before=None):
        """지금 골라진 유닛. before 를 주면 선택이 그것에서 '바뀌고' 게임이 확정할 때까지 기다린다 (최대 0.8초).
        바뀌기 전에 읽으면 방금 전 유닛을 고른 줄 안다."""
        now = self.rq(self.sel + OFF_SEL_NOW)
        for _ in range(40 if before is not None else 0):
            if now and now != before and now == self.rq(self.sel + OFF_SEL_SYNC):
                break
            time.sleep(0.02)
            now = self.rq(self.sel + OFF_SEL_NOW)
        return now if now == self.rq(self.sel + OFF_SEL_SYNC) else 0

    def step_aside(self, blocker, p, n):
        """지금 골라져 있는 유닛(blocker)을 재료 유닛 p 에게서 멀리 보낸다 (오른쪽 클릭 = 이동). 보냈으면 True."""
        a, b = self.where(blocker), self.where(p)
        if not a or not b:
            return False
        ways = (260, 140), (-260, 140), (260, -140), (-260, -140), (0, 300), (0, -300)
        for k in range(6):                           # 화면 안에서 누를 수 있는 쪽을 찾는다
            ox, oy = ways[(n + k) % 6]
            if self.click(self.screen((b[0] + ox, b[1] + oy, b[2])), right=True):
                break
        else:
            return False
        print(f'  겹쳐 선 {name_of(blocker, self.g)} 를 옆으로 보냄')
        for _ in range(16):                          # 비켜날 때까지 (최대 0.8초)
            time.sleep(0.05)
            now = self.where(blocker)
            if now and (now[0] - b[0]) ** 2 + (now[1] - b[1]) ** 2 > 120 ** 2:
                break
        return True

    def scatter(self, p, n, to=None):
        """유닛 p 둘레에 뭉쳐 선 내 유닛들을 한꺼번에 끌어서 고른 뒤 옆으로 보낸다.
        무리로 움직이면 게임이 대형을 지어 서로 떨어뜨려 세우니, 겹쳐서 못 누르던 유닛이 따로 서게 된다.
        끌어서 고르면 남의 유닛은 안 골라지니, 남의 유닛과 겹친 내 유닛만 빼낼 때도 쓴다 (to = 보낼 곳)."""
        s, xyz = self.body(p), self.where(p)
        if not s or not xyz:
            return False
        a, b = (s[0] - 55, s[1] - 75), (s[0] + 55, s[1] + 35)
        if not self.move(a) or not self.safe(b):
            return False
        time.sleep(0.08)
        u32.mouse_event(2, 0, 0, 0, 0)               # 끌어서 고르기
        for k in range(1, 7):
            time.sleep(0.03)
            self.move((a[0] + (b[0] - a[0]) * k / 6, a[1] + (b[1] - a[1]) * k / 6))
        time.sleep(0.05)
        u32.mouse_event(4, 0, 0, 0, 0)
        time.sleep(0.2)
        if not self.click(self.screen(to or (xyz[0] + (320 if n % 2 == 0 else -320), xyz[1] + 160, xyz[2])), right=True):
            return False
        last = None
        for _ in range(50):                          # 다 걸어가서 멈출 때까지 (최대 2.5초)
            time.sleep(0.05)
            now = self.where(p)
            if now and (now[0] - xyz[0]) ** 2 + (now[1] - xyz[1]) ** 2 > 200 ** 2 and now == last:
                break
            last = now
        time.sleep(0.25)                             # 뒤따라오는 유닛들이 자리를 잡을 때까지
        return True

    def wisp(self, sid):
        """흔함선택위습 하나를 골라, 이 흔함의 전시 유닛(7번 플레이어)이 서 있는 자리로 보낸다. 거기 닿으면 그 흔함으로 바뀐다.
        남의 유닛과 겹쳐 서 있으면 눌러도 남의 것이 골라지니, 내 위습들을 끌어서 골라 빈 곳으로 먼저 옮긴다.
        보내기 전에는 한 마리만 골라졌는지 확인한다: 여럿이 골라진 채로 보내면 전부 그 흔함이 돼 버린다."""
        g, want = self.g, set(REV.get(sid, ()))
        wcode = struct.pack('<I', int.from_bytes(b'e018', 'big'))
        wisps, marks, others, show = [], [], [], []
        for p in g.unit_ptrs() or ():
            o = g.read(p, SZ)
            xyz = o and len(o) == SZ and o[:8] == g.vt and not struct.unpack_from('<I', o, OFF_GONE)[0] and self.where(p)
            if not xyz:
                continue
            code, own = o[OFF_TYPE:OFF_TYPE + 4], struct.unpack_from('<I', o, OFF_OWNER)[0]
            if own != self.me:
                others.append((p, xyz))
                if own == 7:                     # 7번 = 이 맵의 전시용 컴퓨터
                    show.append(xyz)
                    if code in want:
                        marks.append(p)
            elif code == wcode:
                wisps.append(p)
        if not wisps:
            return '흔함선택위습이 없어요'
        if not marks:
            return '그 흔함을 뽑는 자리를 못 찾았어요'
        d2 = lambda a, b: (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
        is_wisp = lambda p: (self.unit(p) or b'')[OFF_TYPE:OFF_TYPE + 4] == wcode
        crowd = lambda w: [f for f, xyz in others if d2(self.where(w), xyz) < 150 ** 2]
        one = len(wisps) == 1
        for again in range(2):
            before = self.rq(self.sel + OFF_SEL_NOW)
            # 지금 골라진 위습 말고 다른 위습을 누른다: 선택이 '바뀌어야' 한 마리만 골라진 것을 알 수 있다
            pool = [w for w in wisps if w != before and is_wisp(w)] or [w for w in wisps if is_wisp(w)]
            if not pool:
                return '흔함선택위습이 없어요'
            w = min(pool, key=lambda a: (bool(crowd(a)), min(d2(self.where(a), self.where(b)) for b in marks)))
            m = min(marks, key=lambda b: d2(self.where(w), self.where(b)))
            ok = lambda u: is_wisp(u) and (one or u != before)
            if not self.aim(w):
                return '흔함선택위습을 화면에 못 잡았어요'
            x, y, z = self.where(w)
            spots = [(min((d2((x + ox, y + oy), f) for _, f in others), default=1e12), (x + ox, y + oy, z))
                     for ox, oy in ((0, -350), (350, 0), (-350, 0), (250, -250), (-250, -250), (250, 250), (-250, 250), (0, 350))
                     if self.safe(self.screen((x + ox, y + oy, z))) and all(d2((x + ox, y + oy), k) > 400 ** 2 for k in show)]
            spot = max(spots)[1] if spots and not again and max(spots)[0] > 200 ** 2 else None   # 남의 유닛과 가장 먼 빈 곳 (뽑는 자리 근처는 안 됨)
            near = crowd(w)
            if (not near or not spot) and self.point_at(w, ok):
                break
            if not spot or not self.scatter(w, 0, spot):
                return ('흔함선택위습을 한 마리만 고르지 못했어요. 위습끼리 조금 떨어뜨려 주세요' if not near and not one
                        else '흔함선택위습을 고르지 못했어요. 다른 유닛과 겹쳐 있으면 조금 떨어뜨려 주세요')
            print(f'  위습들을 끌어서 골라 빈 곳으로 옮김 (겹쳐 있던 남의 유닛 {len(near)})')
            # 방금 위습 여럿을 한꺼번에 골랐다: 원래 자리에 남은 남의 유닛을 눌러 선택을 푼다 (안 풀려도 아래 확인이 막는다)
            if near and self.point_at(near[0], lambda u: u and not self.unit(u)):
                self.press()
                self.selected(self.rq(self.sel + OFF_SEL_NOW))
        under = self.hover()
        self.press()
        if under == before:
            time.sleep(0.12)
            now = self.selected()
        else:
            now = self.selected(before)
        if not is_wisp(now) or (now == before and not one):
            return '흔함선택위습을 한 마리만 고르지 못했어요' if is_wisp(now) else '흔함선택위습을 고르지 못했어요'
        self.last = now                          # 보낸 위습: 유닛으로 바뀌면 사라진다
        if not self.aim(m) or not self.click(self.screen(self.where(m)), right=True):   # 전시 유닛의 발밑으로 이동
            return '뽑는 자리를 화면에서 누르지 못했어요'
        print(f'  위습 -> {name_of(m, g)} 자리로 보냄')
        return 'ok'

    def combine(self, opts):
        """버튼 조합 한 번: 조합 버튼을 가진 재료 유닛을 골라 단축키를 누른다.
        마우스가 그 재료 위에 올라간 것을 게임에서 읽어 확인한 뒤에 누르고, 골라진 유닛도 다시 확인한다.
        겹쳐 서 있어서 마우스로 못 찾으면 둘레 유닛들을 옮겨 떨어뜨린 뒤 다시 한다."""
        g = self.g
        keys = {struct.pack('<I', int.from_bytes(c.encode(), 'big')): k for c, k, *_ in opts}
        key_of = lambda p: keys.get((self.unit(p) or b'')[OFF_TYPE:OFF_TYPE + 4])
        name = lambda p: (g.read(p + OFF_TYPE, 4) or b'')[::-1] if p else None
        cx, cy = self.cam() or (0, 0)
        cands = []
        for _wait in range(8):                   # 앞 단계에서 막 만든 유닛이 재료일 수 있다: 아직 안 보이면 잠깐 기다린다
            for p in g.unit_ptrs() or ():
                xyz = key_of(p) and self.where(p)
                if xyz:
                    cands.append(((xyz[0] - cx) ** 2 + (xyz[1] - cy) ** 2, p))
            if cands:
                break
            time.sleep(0.07)
        if not cands:
            return '조합 버튼을 가진 재료 유닛이 없어요'
        tried = scattered = moved = 0
        for _, p in sorted(cands)[:4]:           # 화면 가운데에서 가까운 것부터
            if not self.unit(p):                 # 그새 사라졌다 (판이 끝났거나 다른 조합에 쓰였다)
                continue
            for _again in range(8):
                if not key_of(p) or not self.aim(p):
                    break
                tried += 1
                # 이미 골라져 있어도 늘 한 마리를 눌러서 고른다: 여러 마리가 함께 골라져 있으면 단축키가 전부에 들어가 여러 번 조합된다
                if not self.point_at(p, key_of):
                    print(f'  마우스로 {name(p)} 를 못 찾음 -> 둘레 유닛을 옮겨 떨어뜨림 ({scattered + 1}번째)')
                    if scattered < 2 and self.scatter(p, scattered):
                        scattered += 1
                        continue
                    break
                before, under = self.selected(), self.hover()
                self.press()                     # 마우스 아래가 재료인 걸 두 번 확인했으니 바로 누른다
                if under == before:              # 골라져 있던 그 유닛을 다시 누른 것: 선택이 안 바뀌는 게 맞다
                    time.sleep(0.12)
                    now = self.selected()
                else:
                    now = self.selected(before)
                print(f'  마우스 아래 재료 확인 누름 -> 고른 유닛 {name(now)}{" (선택 안 바뀜)" if now == before and under != before else ""}')
                key = key_of(now)                # 겹친 다른 유닛이 골라졌어도 같은 재료면 된다
                if key and u32.GetForegroundWindow() == self.h:
                    time.sleep(0.02)
                    self.last = now              # 단축키를 누른 재료: 조합되면 사라진다
                    tap(ord(key), u32.MapVirtualKeyW(ord(key), 0))
                    return 'ok'
                # 재료 위에서 눌렀는데 겹쳐 선 다른 내 유닛이 골라졌다: 그 유닛은 지금 골라져 있으니 옆으로 보내고 다시 누른다
                if now and now != p and self.unit(now) and moved < 6 and self.step_aside(now, p, moved):
                    moved += 1
                else:
                    time.sleep(0.15)             # 선택이 안 바뀌었다: 한 박자 쉬고 다시
        if not any(self.unit(p) for p in g.unit_ptrs() or ()):
            return '내 유닛이 하나도 없어요. 판이 끝난 것 같아요'
        return ('재료 유닛을 고르지 못했어요. 유닛들을 옮겨 떨어뜨려 봤는데도 안 눌려요' if scattered
                else '재료 유닛을 고르지 못했어요. 한 번 더 눌러 주세요' if tried
                else '재료 유닛을 화면에 못 잡았어요. 창고에 있으면 꺼내 주세요')


def _chat(text):
    """게임 창을 앞으로 가져와 Enter - 글자 - Enter. 게임 창이 앞에 있을 때만 친다 (다른 창에 치면 안 된다)."""
    h = focus_game()
    if isinstance(h, str):
        return h
    tap(0x0D, 0x1C)                      # Enter: 채팅 열기
    time.sleep(0.15)
    for ch in text:
        tap(0, ord(ch), 4)               # 글자를 직접 넣는다: 한/영 상태와 상관없다
    time.sleep(0.1)
    if u32.GetForegroundWindow() != h:
        return '입력 도중 창이 바뀌어서 멈췄어요'
    tap(0x0D, 0x1C)
    return 'ok'


# ───────── 게임 위 큰 창 (F1): 도우미 페이지를 그대로 게임 위에 띄운다 ─────────
BIG_TITLE = '원랜디 도우미 · 게임 위'   # 페이지가 ?ov 로 열리면 이 제목을 단다 -> 창을 제목으로 찾는다
u32.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT]


def browser_exe():
    """페이지를 앱 창으로 띄울 브라우저. 기본 브라우저가 크롬 계열이면 그걸 쓴다(찜·설정이 평소 페이지와 같다), 아니면 엣지."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice') as k:
            prog = winreg.QueryValueEx(k, 'ProgId')[0]
        cmd = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, prog + r'\shell\open\command')
        exe = cmd.split('"')[1] if cmd.startswith('"') else cmd.split(' ')[0]
        if os.path.basename(exe).lower() in ('chrome.exe', 'msedge.exe', 'brave.exe', 'whale.exe') and os.path.exists(exe):
            return exe
    except OSError:
        pass
    return next((p for p in (r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                             r'C:\Program Files\Microsoft\Edge\Application\msedge.exe') if os.path.exists(p)), None)


def big_window():
    found = []

    @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def each(h, _):
        buf = ctypes.create_unicode_buffer(64)
        u32.GetWindowTextW(h, buf, 64)
        if buf.value == BIG_TITLE:
            found.append(h)
        return True
    u32.EnumWindows(each, 0)
    return found[0] if found else None


u32.GetWindowLongW.argtypes = [W.HWND, ctypes.c_int]
u32.SetWindowLongW.argtypes = [W.HWND, ctypes.c_int, ctypes.c_long]
u32.SetLayeredWindowAttributes.argtypes = [W.HWND, W.DWORD, W.BYTE, W.DWORD]
u32.FindWindowW.restype = W.HWND


def big_shown(h):
    return bool(h and u32.IsWindowVisible(h) and not u32.IsIconic(h) and not u32.GetWindowLongW(h, -20) & 0x20)


def big_show(h, on):
    """큰 창을 보이거나 감춘다. 창을 진짜로 숨기면 브라우저가 화면 그리기를 멈춰서 다시 띄울 때 오래 걸리고 버벅인다
    -> 창은 그대로 두고 투명하게(+마우스가 통과하게)만 바꾼다. 바로 켜지고 바로 꺼진다."""
    ex = u32.GetWindowLongW(h, -20) | 0x80000                    # WS_EX_LAYERED
    if on and u32.IsIconic(h):
        u32.ShowWindow(h, 4)                                     # 최소화돼 있었다: 초점은 뺏지 않고 편다
    u32.SetWindowLongW(h, -20, (ex & ~0x20) if on else (ex | 0x20))   # WS_EX_TRANSPARENT: 감췄을 땐 클릭이 게임으로 간다
    u32.SetLayeredWindowAttributes(h, 0, max(60, min(255, int(cfg.get('big_alpha', 100)) * 255 // 100)) if on else 0, 2)
    if on:
        u32.SetWindowPos(h, W.HWND(-1), 0, 0, 0, 0, 0x13)        # 항상 위, 초점은 게임에 둔다


def toggle_big():
    """큰 창을 열거나(처음) 감추거나 다시 보인다. 감출 때 초점이 큰 창에 있었으면 게임 창으로 돌려준다."""
    h = big_window()
    if big_shown(h):
        big_show(h, False)
        gh = u32.FindWindowW(None, 'Warcraft III')
        if gh and u32.GetForegroundWindow() == h:
            front(gh)
        return
    if not h:
        exe = browser_exe()
        if not exe:
            state['notice'] = '크롬이나 엣지를 못 찾아서 큰 창을 띄울 수 없어요'
            return
        subprocess.Popen([exe, f'--app=http://127.0.0.1:{PORT}/?ov', '--window-size=1400,800', '--window-position=60,60'])
        for _ in range(60):
            time.sleep(0.1)
            h = big_window()
            if h:
                break
    if h:
        if not u32.IsWindowVisible(h):
            u32.ShowWindow(h, 4)
        big_show(h, True)


BIG_KEYS = {'`': 0xC0, 'F1': 0x70, 'F4': 0x73, 'F5': 0x74, 'F7': 0x76, 'Insert': 0x2D, '마우스 옆버튼': 0x05}


def hotkey_loop():
    """큰 창 여닫기 키(기본 마우스 옆버튼). 게임이나 큰 창이 앞에 있을 때만 (키 상태만 본다, 키보드 후킹 아님)."""
    was = False
    while True:
        down = bool(u32.GetAsyncKeyState(BIG_KEYS.get(cfg.get('big_key'), 0x05)) & 0x8000)
        if down and not was:
            buf = ctypes.create_unicode_buffer(64)
            u32.GetWindowTextW(u32.GetForegroundWindow(), buf, 64)
            if buf.value in ('Warcraft III', BIG_TITLE):
                try:
                    toggle_big()
                except Exception:
                    traceback.print_exc()
        was = down
        time.sleep(0.02)


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
        try:
            self.end_headers()
            self.wfile.write(body)
        except OSError:      # 페이지가 새로 고쳐지며 연결을 끊었다: 기록을 어지럽힐 일이 아니다
            pass

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
        elif path == '/bigalpha' and q.startswith('v=') and q[2:].isdigit():   # 큰 창 투명도 (30~100 %)
            cfg['big_alpha'] = state['big_alpha'] = max(30, min(100, int(q[2:])))
            save_cfg()
            h = big_window()
            if big_shown(h):
                big_show(h, True)
            self._send(200, b'ok', 'text/plain')
        elif path == '/bigkey' and q.startswith('k='):   # 큰 창 여닫는 키 바꾸기
            from urllib.parse import unquote
            k = unquote(q[2:])
            if k in BIG_KEYS:
                cfg['big_key'] = state['big_key'] = k
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
        elif self.path == '/combine':   # 몸통 = [[만들 유닛 id, 채팅 명령어 또는 ''], ...]
            # 이 컴퓨터에서 연 우리 페이지만: 다른 사이트나 같은 공유기의 다른 기기가 게임을 조작하게 두지 않는다
            mine = self.client_address[0] == '127.0.0.1' and self.headers.get('Origin') in (f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}')
            try:
                steps = [(int(i), str(c)) for i, c in json.loads(body)]
            except Exception:
                steps = []
            ok = 0 < len(steps) <= 60 and all(i in REV if c == '@wisp' else valid_cmd(c) if c else i in COMBOS for i, c in steps)
            msg = '이 컴퓨터에서 연 페이지에서만 돼요' if not mine else game_steps(steps) if ok else '게임에서 대신 조합할 수 없는 유닛이 섞여 있어요'
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
                          ('게임 위 큰 창 여닫기', toggle_big),
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
    threading.Thread(target=hotkey_loop, daemon=True).start()
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
