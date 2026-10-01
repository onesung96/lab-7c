"""윈도우 트레이 아이콘 (ctypes 만 사용, 추가 라이브러리 없음).

Tray(icon_path, tooltip_fn, items, on_click).start()
  items: [(라벨 또는 라벨을 돌려주는 함수, 눌렀을 때 함수 또는 None=회색 글자), ...]  라벨이 '-' 면 구분선
"""
import ctypes, ctypes.wintypes as W, threading

user32, shell32, kernel32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
WM_APP_TRAY, WM_APP_STOP = 0x8001, 0x8002


class WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', W.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', W.HINSTANCE), ('hIcon', W.HICON), ('hCursor', W.HANDLE), ('hbrBackground', W.HBRUSH),
                ('lpszMenuName', W.LPCWSTR), ('lpszClassName', W.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [('cbSize', W.DWORD), ('hWnd', W.HWND), ('uID', W.UINT), ('uFlags', W.UINT), ('uCallbackMessage', W.UINT),
                ('hIcon', W.HICON), ('szTip', W.WCHAR * 128), ('dwState', W.DWORD), ('dwStateMask', W.DWORD),
                ('szInfo', W.WCHAR * 256), ('uVersion', W.UINT), ('szInfoTitle', W.WCHAR * 64), ('dwInfoFlags', W.DWORD),
                ('guidItem', ctypes.c_byte * 16), ('hBalloonIcon', W.HICON)]


user32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = W.HWND
user32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
user32.LoadImageW.restype = W.HANDLE
user32.LoadImageW.argtypes = [W.HINSTANCE, W.LPCWSTR, W.UINT, ctypes.c_int, ctypes.c_int, W.UINT]
user32.LoadIconW.restype = W.HICON
user32.LoadIconW.argtypes = [W.HINSTANCE, W.LPVOID]
user32.CreatePopupMenu.restype = W.HMENU
user32.AppendMenuW.argtypes = [W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR]
user32.TrackPopupMenu.argtypes = [W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.LPVOID]
user32.DestroyMenu.argtypes = [W.HMENU]
user32.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
user32.SetForegroundWindow.argtypes = [W.HWND]
shell32.Shell_NotifyIconW.argtypes = [W.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
kernel32.GetModuleHandleW.restype = W.HINSTANCE


class Tray:
    def __init__(self, icon_path, tooltip_fn, items, on_click):
        self.icon_path, self.tooltip_fn, self.items, self.on_click = icon_path, tooltip_fn, items, on_click
        self.hwnd, self.nid, self.ok = None, None, False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _nid(self, flags):
        n = self.nid
        n.uFlags = flags
        return ctypes.byref(n)

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)          # 참조를 잡아 둬야 한다 (가비지 수집되면 죽음)
        wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName='OrdrHelperTray')
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, 'ordr-helper', 0, 0, 0, 0, 0, None, None, hinst, None)
        icon = user32.LoadImageW(None, self.icon_path, 1, 0, 0, 0x10 | 0x40) or user32.LoadIconW(None, ctypes.c_void_p(32512))
        self.nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                                   uCallbackMessage=WM_APP_TRAY, hIcon=icon)
        self.nid.szTip = self.tooltip_fn()[:127]
        self.ok = bool(shell32.Shell_NotifyIconW(0, self._nid(0x1 | 0x2 | 0x4)))   # NIM_ADD: MESSAGE|ICON|TIP
        user32.SetTimer(self.hwnd, 1, 2000, None)    # 툴팁(상태) 2초마다 갱신
        msg = W.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def balloon(self, title, text):
        if not self.ok: return
        self.nid.szInfoTitle, self.nid.szInfo, self.nid.dwInfoFlags = title[:63], text[:255], 1
        shell32.Shell_NotifyIconW(1, self._nid(0x10))   # NIM_MODIFY, NIF_INFO
        self.nid.szInfo = ''

    def remove(self):
        if self.ok:
            shell32.Shell_NotifyIconW(2, self._nid(0)); self.ok = False   # NIM_DELETE

    def _menu(self):
        menu = user32.CreatePopupMenu()
        for i, (label, fn) in enumerate(self.items):
            text = label() if callable(label) else label
            if text == '-': user32.AppendMenuW(menu, 0x800, 0, None)               # MF_SEPARATOR
            else: user32.AppendMenuW(menu, 0 if fn else 0x1, i + 1, text)          # MF_GRAYED when no action
        pt = W.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)        # 이게 없으면 메뉴 밖을 눌러도 안 닫힌다
        cmd = user32.TrackPopupMenu(menu, 0x100 | 0x80, pt.x, pt.y, 0, self.hwnd, None)   # RETURNCMD | NONOTIFY
        user32.DestroyMenu(menu)
        if cmd and self.items[cmd - 1][1]:
            threading.Thread(target=self.items[cmd - 1][1], daemon=True).start()

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_APP_TRAY:
                ev = lp & 0xFFFF
                if ev in (0x202, 0x203): threading.Thread(target=self.on_click, daemon=True).start()   # 왼쪽 클릭/더블클릭
                elif ev == 0x205: self._menu()                                                          # 오른쪽 클릭
                return 0
            if msg == 0x113 and self.ok:              # WM_TIMER
                tip = self.tooltip_fn()[:127]
                if tip != self.nid.szTip:
                    self.nid.szTip = tip
                    shell32.Shell_NotifyIconW(1, self._nid(0x4))
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wp, lp)
