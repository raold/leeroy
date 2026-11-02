"""The Win32 declarations push-to-talk needs, through ctypes: a hidden window and its message
loop, Raw Input, the async key state, SendInput (the live tests only) and the token elevation
checks. hotkey imports this only inside RawInputKeys, key_pressed, send_key and the elevation
helpers, so the rest of the service imports on Linux (CI); the voice suite lets only tests
marked `windows` import it.

The DLLs are loaded as private WinDLL objects, so the argtypes set here never change the
functions ctypes.windll hands comtypes and pycaw."""
import ctypes
import sys
from ctypes import wintypes

if sys.platform != "win32":
    raise ImportError("leeroy_voice.winapi is Windows only")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

LRESULT = wintypes.LPARAM  # pointer-sized and signed
ULONG_PTR = wintypes.WPARAM  # pointer-sized and unsigned
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                             wintypes.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_INPUT = 0x00FF
WS_EX_TOOLWINDOW = 0x00000080  # kept off the taskbar and Alt+Tab
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100  # input while another window has focus
RID_INPUT = 0x10000003
RIM_TYPEKEYBOARD = 1
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_KEYBOARD = 0x06
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEY_DOWN_BIT = 0x8000  # GetAsyncKeyState: the key is down now
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TokenElevation, of TOKEN_INFORMATION_CLASS
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87  # OpenProcess on a process that has exited
GETRAWINPUTDATA_FAILED = 0xFFFFFFFF  # (UINT)-1


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT), ("lPrivate", wintypes.DWORD)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT),
                ("Reserved", wintypes.USHORT), ("VKey", wintypes.USHORT),
                ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]


class _RAWMOUSEBUTTONS(ctypes.Structure):
    _fields_ = [("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT)]


class _RAWMOUSEUNION(ctypes.Union):
    _fields_ = [("ulButtons", wintypes.ULONG), ("buttons", _RAWMOUSEBUTTONS)]


class RAWMOUSE(ctypes.Structure):  # only for the size of RAWINPUT's union
    _fields_ = [("usFlags", wintypes.USHORT), ("u", _RAWMOUSEUNION),
                ("ulRawButtons", wintypes.ULONG), ("lLastX", wintypes.LONG),
                ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]


class RAWHID(ctypes.Structure):
    _fields_ = [("dwSizeHid", wintypes.DWORD), ("dwCount", wintypes.DWORD),
                ("bRawData", wintypes.BYTE * 1)]


class _RAWINPUTDATA(ctypes.Union):
    _fields_ = [("mouse", RAWMOUSE), ("keyboard", RAWKEYBOARD), ("hid", RAWHID)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("data", _RAWINPUTDATA)]


class MOUSEINPUT(ctypes.Structure):  # only for the size of INPUT's union
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


def _declare(dll, name: str, restype, *argtypes) -> None:
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = argtypes


_declare(kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
_declare(kernel32, "GetCurrentThreadId", wintypes.DWORD)
_declare(kernel32, "GetCurrentProcess", wintypes.HANDLE)
_declare(kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL,
         wintypes.DWORD)
_declare(kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_declare(advapi32, "OpenProcessToken", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
         ctypes.POINTER(wintypes.HANDLE))
_declare(advapi32, "GetTokenInformation", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int,
         wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
_declare(user32, "RegisterClassExW", wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
_declare(user32, "UnregisterClassW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
_declare(user32, "CreateWindowExW", wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR,
         wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
         ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
_declare(user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
_declare(user32, "IsWindow", wintypes.BOOL, wintypes.HWND)
_declare(user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
_declare(user32, "DefWindowProcW", LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
         wintypes.LPARAM)
_declare(user32, "GetMessageW", wintypes.BOOL, ctypes.POINTER(MSG), wintypes.HWND,
         wintypes.UINT, wintypes.UINT)
_declare(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(MSG))
_declare(user32, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
         wintypes.LPARAM)
_declare(user32, "PostThreadMessageW", wintypes.BOOL, wintypes.DWORD, wintypes.UINT,
         wintypes.WPARAM, wintypes.LPARAM)
_declare(user32, "RegisterRawInputDevices", wintypes.BOOL, ctypes.POINTER(RAWINPUTDEVICE),
         wintypes.UINT, wintypes.UINT)
_declare(user32, "GetRawInputData", wintypes.UINT, wintypes.HANDLE, wintypes.UINT,
         wintypes.LPVOID, ctypes.POINTER(wintypes.UINT), wintypes.UINT)
_declare(user32, "GetAsyncKeyState", wintypes.SHORT, ctypes.c_int)
_declare(user32, "SendInput", wintypes.UINT, wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_declare(user32, "EnumWindows", wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
_declare(user32, "GetForegroundWindow", wintypes.HWND)
_declare(user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND,
         ctypes.POINTER(wintypes.DWORD))
