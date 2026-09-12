import A101.rectangle_solver_job as mod


class FakeParent:
    def poll(self, timeout=0):
        return True
    def recv(self):
        raise EOFError
    def close(self):
        pass


class FakeChild:
    def close(self):
        pass


class FakeProcess:
    def __init__(self):
        self.pid = 123
        self.exitcode = None
        self._alive = False
    def start(self):
        pass
    def join(self, timeout=None):
        self.exitcode = -9
    def is_alive(self):
        return self._alive
    def kill(self):
        self._alive = False
        self.exitcode = -9


class FakeContext:
    def __init__(self, process):
        self.process = process
    def Pipe(self, duplex):
        return FakeParent(), FakeChild()
    def Process(self, target, args):
        return self.process


def test_run_worker_joins_before_reporting_exitcode_on_eof(monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr(mod, "get_context", lambda name: FakeContext(process))
    ticks = iter([0.0, 0.01, 0.02, 0.03])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(ticks, 0.04))

    status, payload, _elapsed = mod._run_worker({"x": 1}, timeout=1.0)

    assert status == "error"
    assert payload == "worker exited with code -9"
