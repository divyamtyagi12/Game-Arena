"""Dependency-free runner (pytest also works): python -m tests.run_all"""
import importlib, pkgutil, sys, traceback, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.support import stubs
stubs.install()
class _SuiteResult(unittest.TestResult):
    def __init__(self, mod_name):
        super().__init__()
        self.mod_name = mod_name
    def addSuccess(self, test):
        super().addSuccess(test)
        print("PASS", self.mod_name, test.id().split(".")[-1])
    def addFailure(self, test, err):
        super().addFailure(test, err)
        print("FAIL", self.mod_name, test.id().split(".")[-1])
        print(self._exc_info_to_string(err, test))
    def addError(self, test, err):
        super().addError(test, err)
        print("FAIL", self.mod_name, test.id().split(".")[-1])
        print(self._exc_info_to_string(err, test))
    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        print("SKIP", self.mod_name, test.id().split(".")[-1], f"({reason})")

fails = total = skipped = 0
for mod in sorted(m.name for m in pkgutil.iter_modules([str(Path(__file__).parent)]) if m.name.startswith("test_")):
    module = importlib.import_module(f"tests.{mod}")
    for name in dir(module):
        if name.startswith("test_") and callable(getattr(module, name)) and not isinstance(getattr(module, name), type):
            total += 1
            try:
                getattr(module, name)()
                print("PASS", mod, name)
            except unittest.SkipTest as why:
                skipped += 1
                print("SKIP", mod, name, f"({why})")
            except Exception:
                fails += 1
                print("FAIL", mod, name); traceback.print_exc()
    suite = unittest.TestLoader().loadTestsFromModule(module)
    if suite.countTestCases() > 0:
        res = _SuiteResult(mod)
        suite.run(res)
        total += res.testsRun
        fails += len(res.failures) + len(res.errors)
        skipped += len(res.skipped)
print(f"{total - fails - skipped}/{total} passed" + (f", {skipped} skipped" if skipped else ""))
sys.exit(1 if fails else 0)
