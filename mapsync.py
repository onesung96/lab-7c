"""새 원랜디 맵 버전이 나오면 mapping.json(맵 유닛 코드 -> sions 도우미 id)을 자동으로 다시 만든다.

1. Maps/Download 에서 가장 최신 ORDR 맵을 찾는다
2. 맵(MPQ, 보호맵 포함)에서 war3map.w3u + war3map.wts 를 읽어 유닛 코드 -> "이름 - 등급"
3. sions mk_helper 게시판에서 같은 버전 글을 찾아 조합 데이터를 받는다
4. 같은 등급 안에서 이름이 가장 많이 겹치는 항목끼리 짝짓는다
"""
import bz2, html, json, os, re, struct, urllib.parse, urllib.request, zlib

M32 = 0xFFFFFFFF


# ───────── MPQ (보호맵: 헤더 값 뒤섞기, 가짜 single-unit 플래그, 암호화 파일 대응) ─────────
def _crypt_table():
    seed, t = 0x00100001, [0] * 0x500
    for i in range(0x100):
        idx = i
        for _ in range(5):
            seed = (seed * 125 + 3) % 0x2AAAAB; a = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB; b = seed & 0xFFFF
            t[idx] = a | b; idx += 0x100
    return t


CT = _crypt_table()


def mhash(s, typ):
    s1, s2 = 0x7FED7FED, 0xEEEEEEEE
    for ch in s.upper().encode('latin1'):
        s1 = (CT[(typ << 8) + ch] ^ (s1 + s2)) & M32
        s2 = (ch + s1 + s2 + (s2 << 5) + 3) & M32
    return s1


def decrypt(data, key):
    out, s2 = bytearray(), 0xEEEEEEEE
    n = len(data) // 4
    for i in range(n):
        s2 = (s2 + CT[0x400 + (key & 0xFF)]) & M32
        v = (struct.unpack_from('<I', data, i * 4)[0] ^ (key + s2)) & M32
        key = ((~key << 0x15) + 0x11111111 | key >> 0x0B) & M32
        s2 = (v + s2 + (s2 << 5) + 3) & M32
        out += struct.pack('<I', v)
    return bytes(out) + bytes(data[n * 4:])   # 끝의 len%4 바이트는 암호화 안 됨


class MPQ:
    def __init__(self, path):
        d = open(path, 'rb').read()
        self.m = d[d.find(b'MPQ\x1a'):]
        ht_off, bt_off, ht_n, bt_n = struct.unpack_from('<IIII', self.m, 16)
        self.shift = struct.unpack_from('<H', self.m, 14)[0]
        ht = decrypt(self.m[ht_off:ht_off + ht_n * 16], mhash('(hash table)', 3))
        bt = decrypt(self.m[bt_off:bt_off + bt_n * 16], mhash('(block table)', 3))
        self.hash = [struct.unpack_from('<IIHHI', ht, i * 16) for i in range(ht_n)]
        self.block = [struct.unpack_from('<IIII', bt, i * 16) for i in range(bt_n)]

    def _lookup(self, name):
        a, b, n = mhash(name, 1), mhash(name, 2), len(self.hash)
        i = mhash(name, 0) & (n - 1)
        for _ in range(n):
            ha, hb, _, _, bi = self.hash[i]
            if bi == 0xFFFFFFFF:
                return None
            if ha == a and hb == b and bi < len(self.block):
                return self.block[bi]
            i = (i + 1) & (n - 1)

    @staticmethod
    def _decomp(s):
        if s[0] == 2: return zlib.decompress(s[1:])
        if s[0] == 0x10: return bz2.decompress(s[1:])
        raise ValueError('압축 방식 %x' % s[0])

    def read(self, name):
        blk = self._lookup(name)
        if not blk:
            return None
        off, csize, size, flags = blk
        raw = self.m[off:off + csize]
        key = None
        if flags & 0x10000:
            key = mhash(name.split('\\')[-1], 3)
            if flags & 0x20000: key = ((key + off) ^ size) & M32
        dec = (lambda x, k: decrypt(x, k)) if key is not None else (lambda x, k: bytes(x))
        err = None
        for shift in (self.shift, 3, None):   # 보호맵은 single-unit 플래그를 가짜로 달고 섹터 크기를 부풀린다
            try:
                if shift is None:
                    r = dec(raw, key or 0)
                    return self._decomp(r) if flags & 0x200 and size > csize else r
                ss = 512 << shift
                ns = (size + ss - 1) // ss
                cnt = ns + 1 + (1 if flags & 0x04000000 else 0)
                pos = struct.unpack('<%dI' % cnt, dec(raw[:4 * cnt], ((key or 0) - 1) & M32))
                out, left = bytearray(), size
                for i in range(ns):
                    s = dec(raw[pos[i]:pos[i + 1]], ((key or 0) + i) & M32)
                    if flags & 0x200 and len(s) < min(ss, left): s = self._decomp(s)
                    out += s; left -= len(s)
                if len(out) == size:
                    return bytes(out)
            except Exception as e:
                err = e
        raise ValueError(f'{name} 읽기 실패: {err}')


# ───────── 맵 -> 유닛 이름 ─────────
CLR = re.compile(r'\|[cC][0-9a-fA-F]{8}|\|[rRnN]')


def map_units(path):
    mpq = MPQ(path)
    wts = mpq.read('war3map.wts').decode('utf-8', 'replace')
    strings = {int(m.group(1)): m.group(2).strip() for m in re.finditer(r'STRING (\d+)[^{]*\{\r?\n(.*?)\r?\n\}', wts, re.S)}
    d, pos = mpq.read('war3map.w3u'), [0]

    def i32():
        v = struct.unpack_from('<i', d, pos[0])[0]; pos[0] += 4; return v

    def raw4():
        v = d[pos[0]:pos[0] + 4].decode('latin1'); pos[0] += 4; return v

    def cstr():
        e = d.index(b'\0', pos[0]); v = d[pos[0]:e].decode('utf-8', 'replace'); pos[0] = e + 1; return v

    ver, fields = i32(), {}
    for _table in range(2):
        for _ in range(i32()):
            orig, new = raw4(), raw4()
            code = new if new != '\0\0\0\0' else orig
            for _ in range(i32() if ver >= 3 else 1):
                if ver >= 3: i32()
                for _ in range(i32()):
                    mid, typ = raw4(), i32()
                    if typ == 0: v = i32()
                    elif typ in (1, 2): v = None; pos[0] += 4
                    else: v = cstr()
                    i32()
                    if mid in ('unam', 'upro') and isinstance(v, str):
                        m = re.fullmatch(r'TRIGSTR_0*(\d+)', v)
                        fields.setdefault(code, {})[mid] = CLR.sub('', strings.get(int(m.group(1)), v) if m else v)
    out = {}
    for code, f in fields.items():
        name = f.get('unam', '')
        pro = f.get('upro', '').split(',')[0]
        if code[0] == 'H' and ' - ' in pro:   # 영웅: unam 은 칭호, upro 에 "이름 - 등급"
            name = pro
        out[code] = name
    return out


# ───────── 짝짓기 ─────────
GRADE = {'흔함': '흔함', '안흔함': '안흔함', '특별': '특별함', '희귀': '희귀함', '전설': '전설', '히든': '히든',
         '왜곡': '왜곡', '변화': '변화', '세라핌': '세라핌', '초월': '초월함', '제한': '제한됨', '불멸': '불멸의',
         '영원': '영원함', '신비': '신비함', '랜덤전용': '랜덤유닛'}
WISP = {'e0IX': 0}   # 랜덤위습 -> 위습
# 이름이 달라 자동으로 못 짝짓는 것 (sions 쪽 라벨이 다름). 코드가 버전 간에 유지된다고 본다.
OVERRIDE_NAMES = {'h02N': '모리아', 'h05X': '레일리(히든)', 'h05Y': '고대의 배', 'h060': '해적선', 'h04S': '초월쿠마',
                  'H0AA': '베가펑크', 'H0BK': '니카 [루피]'}
_strip = lambda s: re.sub(r'[^가-힣A-Za-z]', '', s).lower()


def _tokens(sname):
    out = []
    for w in re.split(r'[\s,(]+', sname):
        w = re.split(r'[0-9●:%]', w)[0]
        if _strip(w): out.append(_strip(w))
        if len(out) >= 4: break
    return out


def _score(uname, item):
    un, toks = _strip(uname), _tokens(item['name'])
    if not toks: return (0, 0)
    if un == toks[0]: return (100, 0)
    hit = sum(len(t) for t in toks if t in un)
    return (hit, -sum(len(t) for t in toks if t not in un)) if hit >= 2 else (0, 0)


def build_mapping(units, sions):
    groups, allitems = {}, []
    for g in sions['groups']:
        allitems += g['items']
        for key, gname in GRADE.items():
            if g['name'].startswith(gname):
                groups.setdefault(key, []).extend(g['items'])
    mapping = dict(WISP)
    for code, n in units.items():
        if ' - ' not in n or n.startswith('!'): continue
        uname, grade = [x.strip() for x in n.rsplit(' - ', 1)]
        if grade not in groups: continue
        ranked = sorted(((_score(uname, it), it) for it in groups[grade]), key=lambda x: x[0], reverse=True)
        best, it = ranked[0]
        if best[0] > 0 and not (len(ranked) > 1 and ranked[1][0] == best):
            mapping[code] = it['id']
    for code, label in OVERRIDE_NAMES.items():
        hit = next((it['id'] for it in allitems if it['name'].startswith(label)), None)
        if code in units and hit is not None:
            mapping[code] = hit
    return mapping


# ───────── sions ─────────
UA = {'User-Agent': 'Mozilla/5.0'}


def fetch(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read().decode('utf-8', 'replace')


def sions_data(url):
    raw = re.search(r'id="wr_content_json"[^>]*>(.*?)</', fetch(url), re.S).group(1)
    return json.loads(html.unescape(raw))


def find_sions_post(version):
    """mk_helper 에서 '원랜디 … {version}' 제목의 가장 최근 글 URL"""
    q = urllib.parse.quote(version)
    page = fetch(f'https://sions.kr/bbs/board.php?bo_table=mk_helper&sfl=wr_subject&stx={q}')
    best = None
    for m in re.finditer(r'href="[^"]*wr_id=(\d+)[^"]*"[^>]*>(.*?)</a>', page, re.S):
        title = re.sub(r'<[^>]+>|\s+', ' ', html.unescape(m.group(2)))
        if version in title and ('원랜디' in title or 'ORDR' in title.upper()):
            wid = int(m.group(1))
            best = max(best or 0, wid)
    return best and f'https://sions.kr/bbs/board.php?bo_table=mk_helper&wr_id={best}'


# ───────── 맵 찾기 ─────────
def map_dirs():
    home = os.path.expanduser('~')
    for docs in ('Documents', os.path.join('OneDrive', '문서'), os.path.join('OneDrive', 'Documents'), '문서'):
        p = os.path.join(home, docs, 'Warcraft III', 'Maps', 'Download')
        if os.path.isdir(p):
            yield p


def latest_map():
    found = []
    for d in map_dirs():
        for f in os.listdir(d):
            m = re.search(r'(\d+\.\d{3})', f)
            if f.lower().endswith('.w3x') and 'ORDR' in f.upper() and m:
                p = os.path.join(d, f)
                found.append((tuple(int(x) for x in m.group(1).split('.')), os.path.getmtime(p), m.group(1), p))
    return max(found)[2:] if found else (None, None)


if __name__ == '__main__':   # 수동 점검: python mapsync.py
    ver, path = latest_map()
    print('최신 맵', ver, path)
    units = map_units(path)
    print('유닛', len(units))
    url = find_sions_post(ver)
    print('sions 글', url)
    mp = build_mapping(units, sions_data(url))
    print('짝지음', len(mp))
