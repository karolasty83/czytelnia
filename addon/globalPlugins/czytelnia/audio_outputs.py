"""Standalone Windows Core Audio output enumeration (stdlib only)."""
import ctypes
import sys


def _check_hresult(result, operation):
    """Use Windows LONG width even on LP64 hosts."""
    if ctypes.c_int32(result).value < 0:
        raise OSError(f'{operation} failed (HRESULT 0x{result & 0xffffffff:08X})')


class _GUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16),
                ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [('fmtid', _GUID), ('pid', ctypes.c_uint32)]


class _COUNTED_POINTER(ctypes.Structure):
    # A PROPVARIANT's largest value includes a count followed by a pointer:
    # 16 bytes on Win64 (including padding), 8 bytes on Win32.
    _fields_ = [('count', ctypes.c_uint32), ('pointer', ctypes.c_void_p)]


class _PROPVARIANT_VALUE(ctypes.Union):
    _fields_ = [('pwszVal', ctypes.c_wchar_p),
                ('counted', _COUNTED_POINTER), ('integer', ctypes.c_int64)]


class _PROPVARIANT(ctypes.Structure):
    # The DECIMAL overlay does not enlarge this layout. Zero initialization
    # is PropVariantInit; never use a string buffer or a pointer-sized union.
    _fields_ = [('vt', ctypes.c_uint16), ('reserved1', ctypes.c_uint16),
                ('reserved2', ctypes.c_uint16), ('reserved3', ctypes.c_uint16),
                ('value', _PROPVARIANT_VALUE)]


_CLSID_MMDEVICE_ENUMERATOR = _GUID(
    0xbcde0395, 0xe52f, 0x467c,
    (ctypes.c_ubyte * 8)(0x8e, 0x3d, 0xc4, 0x57, 0x92, 0x91, 0x69, 0x2e))
_IID_IMMDEVICE_ENUMERATOR = _GUID(
    0xa95664d2, 0x9614, 0x4f35,
    (ctypes.c_ubyte * 8)(0xa7, 0x46, 0xde, 0x8d, 0xb6, 0x36, 0x17, 0xe6))
# Same key used by SDL2's SDL_immdevice.c / GetMMDeviceInfo; not the
# interface friendly name and not waveOut's truncated device description.
_PKEY_DEVICE_FRIENDLY_NAME = _PROPERTYKEY(
    _GUID(0xa45c254e, 0xdf1c, 0x4efd,
          (ctypes.c_ubyte * 8)(0x80, 0x20, 0x67, 0xd1, 0x46, 0xa8, 0x50, 0xe0)), 14)
_HRESULT = ctypes.c_int32
_DWORD = ctypes.c_uint32
_POINTER = ctypes.c_void_p
_POINTER_OUT = ctypes.POINTER(_POINTER)


def _com_method(pointer, index, result_type, *argtypes):
    """Bind a COM vtable slot and its this pointer (stdcall on Win32)."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_POINTER))).contents
    prototype = ctypes.WINFUNCTYPE(result_type, _POINTER, *argtypes)
    function = prototype(vtable[index])
    return lambda *args: function(pointer, *args)


def _release(pointer):
    if pointer.value:
        _com_method(pointer, 2, _DWORD)()


def _list_windows(ole32):
    """Enumerate on this thread; never uninitialize an existing STA on mismatch."""
    initialized = ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    if initialized & 0xffffffff != 0x80010106:  # RPC_E_CHANGED_MODE
        _check_hresult(initialized, 'CoInitializeEx')
    uninitialize = ctypes.c_int32(initialized).value >= 0
    enumerator = _POINTER()
    collection = _POINTER()
    try:
        _check_hresult(ole32.CoCreateInstance(
            ctypes.byref(_CLSID_MMDEVICE_ENUMERATOR), None, 1,  # CLSCTX_INPROC_SERVER
            ctypes.byref(_IID_IMMDEVICE_ENUMERATOR), ctypes.byref(enumerator)),
            'CoCreateInstance(MMDeviceEnumerator)')
        _check_hresult(_com_method(enumerator, 3, _HRESULT, ctypes.c_int32,
                                  _DWORD, _POINTER_OUT)(0, 1, ctypes.byref(collection)),
                       'IMMDeviceEnumerator.EnumAudioEndpoints')  # eRender, ACTIVE
        count = _DWORD()
        _check_hresult(_com_method(collection, 3, _HRESULT,
                                  ctypes.POINTER(_DWORD))(ctypes.byref(count)),
                       'IMMDeviceCollection.GetCount')
        names = set()
        for index in range(count.value):
            device = _POINTER()
            store = _POINTER()
            try:
                _check_hresult(_com_method(collection, 4, _HRESULT, _DWORD,
                                          _POINTER_OUT)(index, ctypes.byref(device)),
                               'IMMDeviceCollection.Item')
                _check_hresult(_com_method(device, 4, _HRESULT, _DWORD,
                                          _POINTER_OUT)(0, ctypes.byref(store)),
                               'IMMDevice.OpenPropertyStore')  # STGM_READ
                value = _PROPVARIANT()
                try:
                    _check_hresult(_com_method(
                        store, 5, _HRESULT, ctypes.POINTER(_PROPERTYKEY),
                        ctypes.POINTER(_PROPVARIANT))(
                            ctypes.byref(_PKEY_DEVICE_FRIENDLY_NAME), ctypes.byref(value)),
                        'IPropertyStore.GetValue(PKEY_Device_FriendlyName)')
                    if value.vt != 31:  # VT_LPWSTR
                        raise OSError('PKEY_Device_FriendlyName is not VT_LPWSTR')
                    name = value.value.pwszVal  # copies the complete Unicode string
                    if name is None:
                        raise OSError('PKEY_Device_FriendlyName is a null string')
                    names.add(name)
                finally:
                    ole32.PropVariantClear(ctypes.byref(value))
            finally:
                _release(store)
                _release(device)
        return sorted(names)
    finally:
        _release(collection)
        _release(enumerator)
        if uninitialize:
            ole32.CoUninitialize()


def list_output_devices() -> list[str]:
    """Return sorted unique active Windows render endpoint friendly names.

    Names are unmodified Unicode strings matching SDL2 WASAPI's device names.
    Identical names cannot be distinguished by SDL's name-based selection.
    An empty list means no active endpoints. Unsupported OSes and Core Audio
    failures raise OSError rather than returning fabricated devices.
    Importing this module does not load any DLL or initialize COM.
    """
    if sys.platform != 'win32':
        raise OSError('Audio output enumeration requires Windows')
    ole32 = ctypes.WinDLL('ole32.dll')
    ole32.CoInitializeEx.argtypes = [_POINTER, _DWORD]
    ole32.CoInitializeEx.restype = _HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), _POINTER, _DWORD,
                                     ctypes.POINTER(_GUID), _POINTER_OUT]
    ole32.CoCreateInstance.restype = _HRESULT
    ole32.PropVariantClear.argtypes = [ctypes.POINTER(_PROPVARIANT)]
    ole32.PropVariantClear.restype = _HRESULT
    return _list_windows(ole32)
