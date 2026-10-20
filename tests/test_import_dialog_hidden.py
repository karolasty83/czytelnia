"""Hidden wxMSW controls only: never Show, focus, speech or installed NVDA."""
import importlib.util
from pathlib import Path
import sys
import unittest

try:
    import wx
except ImportError:
    wx = None


@unittest.skipUnless(wx is not None and sys.platform == 'win32', 'Needs wxMSW')
class HiddenImportDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.GetApp() or wx.App(False)
        spec = importlib.util.spec_from_file_location('hidden_import_dialogs',
            Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/dialogs.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_import_description_is_exactly_the_short_message(self):
        dialog = self.module.ImportProgressDialog(None)
        try:
            labels = [child.GetLabel() for child in dialog.GetChildren()
                      if isinstance(child, wx.StaticText)]
            self.assertEqual(labels, ['Przygotowanie importu — 0%', 'Trwa import książki'])
        finally:
            dialog.Destroy()

    def test_hidden_msaa_reports_progressbar_role_and_numeric_percentage(self):
        import ctypes as C
        import uuid
        # IAccessible ABI from oleacc.h; only query our hidden native control.
        class VariantValue(C.Union):
            _fields_ = [('number', C.c_long), ('storage', C.c_byte * (16 if C.sizeof(C.c_void_p) == 8 else 8))]
        class Variant(C.Structure):
            _fields_ = [('vt', C.c_ushort), ('reserved', C.c_ushort * 3), ('value', VariantValue)]
        class GUID(C.Structure):
            _fields_ = [('data', C.c_byte * 16)]
        iid = GUID.from_buffer_copy(uuid.UUID('618736e0-3c3d-11cf-810c-00aa00389b71').bytes_le)
        oleacc, oleaut = C.WinDLL('oleacc'), C.WinDLL('oleaut32')
        oleacc.AccessibleObjectFromWindow.argtypes = [C.c_void_p, C.c_ulong, C.POINTER(GUID), C.POINTER(C.c_void_p)]
        oleacc.AccessibleObjectFromWindow.restype = C.c_long
        oleaut.SysFreeString.argtypes = [C.c_void_p]
        dialog = self.module.ImportProgressDialog(None)
        pointer = C.c_void_p()
        try:
            result = oleacc.AccessibleObjectFromWindow(dialog.gauge.GetHandle(), 0xFFFFFFFC,
                                                       C.byref(iid), C.byref(pointer))
            self.assertEqual(result, 0)
            vtable = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
            role = C.WINFUNCTYPE(C.c_long, C.c_void_p, Variant, C.POINTER(Variant))(vtable[13])
            value = C.WINFUNCTYPE(C.c_long, C.c_void_p, Variant, C.POINTER(C.c_void_p))(vtable[11])
            child = Variant()
            child.vt, child.value.number = 3, 0  # VT_I4, CHILDID_SELF
            result_role = Variant()
            self.assertEqual(role(pointer, child, C.byref(result_role)), 0)
            self.assertEqual(result_role.vt, 3)
            self.assertEqual(result_role.value.number, 48)  # ROLE_SYSTEM_PROGRESSBAR
            for done in (1, 3, 4):
                dialog.update_progress('copy', done, 5)
                text = C.c_void_p()
                self.assertEqual(value(pointer, child, C.byref(text)), 0)
                try:
                    self.assertRegex(C.wstring_at(text), rf'^\s*{done * 20}\s*%\s*$')
                finally:
                    oleaut.SysFreeString(text)
            self.assertFalse(dialog.IsShown())
        finally:
            if pointer.value:
                release = C.WINFUNCTYPE(C.c_ulong, C.c_void_p)(vtable[2])
                release(pointer)
            dialog.Destroy()

    def test_hidden_native_gauge_value_title_and_close_continue(self):
        self.assertTrue(hasattr(self.module, 'ImportProgressDialog'))
        dialog = self.module.ImportProgressDialog(None)
        try:
            self.assertFalse(dialog.IsShown())
            self.assertEqual(dialog.GetTitle(), 'Import książki do biblioteki')
            self.assertIsInstance(dialog.gauge, wx.Gauge)
            self.assertEqual(dialog.gauge.GetRange(), 100)
            self.assertEqual(dialog.gauge.GetName(), 'Postęp importu')
            import ctypes
            classname = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(ctypes.c_void_p(dialog.gauge.GetHandle()), classname, 256)
            self.assertEqual(classname.value.lower(), 'msctls_progress32')
            dialog.update_progress('copy', 1, 5)
            self.assertEqual(dialog.gauge.GetValue(), 20)
            self.assertIn('Kopiowanie', dialog.phase.GetLabel())
            dialog.update_progress('probe', 3, 5)
            self.assertEqual(dialog.gauge.GetValue(), 60)
            dialog.update_progress('save', 4, 5)
            self.assertEqual(dialog.gauge.GetValue(), 80)
            dialog.update_progress('complete', 5, 5)
            self.assertEqual(dialog.gauge.GetValue(), 100)
            event = wx.CloseEvent(wx.wxEVT_CLOSE_WINDOW, dialog.GetId())
            event.SetCanVeto(True)
            dialog.on_close(event)
            self.assertTrue(event.GetVeto())
            self.assertFalse(dialog.IsShown())
            self.assertFalse(dialog.IsBeingDeleted())
            escape = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
            escape.m_keyCode = wx.WXK_ESCAPE
            dialog.on_key(escape)
            self.assertFalse(dialog.IsBeingDeleted())
        finally:
            dialog.Destroy()


if __name__ == '__main__':
    unittest.main()
