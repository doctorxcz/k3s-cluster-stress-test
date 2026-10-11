"""--keep-probes: probe pods that outlive the program, are found again by label and reused (no real cluster: a stub kubectl)."""
import json
import time

from stress_test import dashboard as d, manifests as m


def _stamp(age_s):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_s))


class Info:
    def __init__(self, master=False, gpu=0, ready=True):
        self.is_control_plane, self.gpu_count, self.ready = master, gpu, ready


class StubKube:
    def __init__(self, items=None, infos=None):
        self.items, self.infos = items or [], infos or {}
        self.deleted, self.applied, self.ran = [], [], []

    def get_json(self, *a):
        return {"items": self.items}

    def list_node_names(self):
        return list(self.infos)

    def get_node(self, n):
        return self.infos[n]

    def delete_pods(self, *names):
        self.deleted += names

    def run(self, *a, **k):
        self.ran.append(a)
        return ""

    def apply_image_pod(self, build, name, kind, say=None, node=None):
        self.applied.append(build())

    def wait_ready(self, pod, t):
        return True

    def exec(self, *a, **k):
        return ""


def _kept_pod(node, name="dash-probe-old-1", age=3600, life=86400, phase="Running", role="dashboard-probe", keep=True, gpu=False):
    pod = (m.dashboard_gpu_pod if role == "dashboard-gpu" else m.dashboard_pod)(node, name, "old", life, keep)
    pod["metadata"]["creationTimestamp"] = _stamp(age)
    pod["status"] = {"phase": phase}
    return pod


def test_pod_marked_and_hashed_regardless_of_name_and_deadline():
    a, b = m.dashboard_pod("n1", "a", "r1", 100, True), m.dashboard_pod("n1", "b", "r2", 999, False)
    assert a["metadata"]["labels"]["persist"] == "1" and "persist" not in b["metadata"]["labels"]
    assert a["metadata"]["annotations"][m.SPEC_ANNOTATION] == b["metadata"]["annotations"][m.SPEC_ANNOTATION] == m.expected_spec_hash("n1", False)
    assert m.expected_spec_hash("n1", True) != m.expected_spec_hash("n1", False)


def test_keep_mode_makes_a_long_lived_labeled_pod_but_never_on_the_master():
    kube = StubKube(infos={"w1": Info(), "master": Info(master=True)})
    c = d.Collector(kube, keep_hours=24)
    c.start(wait=True)
    by_node = {p["spec"]["nodeName"]: p for p in kube.applied}
    assert by_node["w1"]["spec"]["activeDeadlineSeconds"] == 86400 and by_node["w1"]["metadata"]["labels"]["persist"] == "1"
    assert by_node["master"]["spec"]["activeDeadlineSeconds"] == d.POD_DEADLINE and "persist" not in by_node["master"]["metadata"]["labels"]
    assert c.nodes["w1"].probe_kept and not c.nodes["master"].probe_kept
    c.stop()
    master_pod = next(p["metadata"]["name"] for p in kube.applied if p["spec"]["nodeName"] == "master")
    assert kube.deleted == [master_pod]                                           # only the master's pod is deleted


def test_temporary_mode_is_as_before():
    kube = StubKube(infos={"w1": Info()})
    c = d.Collector(kube)
    c.start(wait=True)
    assert kube.applied[0]["spec"]["activeDeadlineSeconds"] == d.POD_DEADLINE and "persist" not in kube.applied[0]["metadata"]["labels"]
    c.stop()
    assert len(kube.deleted) == 1


def test_a_kept_pod_is_reused_and_not_made_again_nor_deleted():
    kube = StubKube(items=[_kept_pod("w1")], infos={"w1": Info()})
    c = d.Collector(kube, keep_hours=24)
    c.start(wait=True)
    assert kube.applied == [] and c.pods["w1"] == ("dash-probe-old-1", "")
    v = c.nodes["w1"]
    assert v.probe_kept and v.probe_reused and 3500 < time.time() - v.probe_born < 3700 and v.probe_life == 86400
    c.stop()
    assert "dash-probe-old-1" not in kube.deleted


def test_temporary_mode_also_reuses_but_leaves_a_kept_pod_alone():
    kube = StubKube(items=[_kept_pod("w1")], infos={"w1": Info()})
    c = d.Collector(kube)
    c.start(wait=True)
    assert kube.applied == []
    c.stop()
    assert "dash-probe-old-1" not in kube.deleted


def test_unusable_pods_are_deleted_and_replaced():
    bad = [("not running", _kept_pod("w1", phase="Pending")),
           ("almost expired", _kept_pod("w1", age=86400 - 100))]
    stale = _kept_pod("w1")
    stale["metadata"]["annotations"][m.SPEC_ANNOTATION] = "000000000000"
    bad.append(("outdated spec", stale))
    for why, pod in bad:
        kube = StubKube(items=[pod], infos={"w1": Info()})
        c = d.Collector(kube, keep_hours=24)
        c.start(wait=True)
        assert pod["metadata"]["name"] in kube.deleted and len(kube.applied) == 1, why


def test_a_temporary_pod_is_not_reused_but_left_alone():
    pod = _kept_pod("w1", keep=False)
    kube = StubKube(items=[pod], infos={"w1": Info()})
    d.Collector(kube, keep_hours=24).start(wait=True)
    assert pod["metadata"]["name"] not in kube.deleted and len(kube.applied) == 1


def test_a_gpu_node_needs_both_pods_to_reuse():
    probe = _kept_pod("g1")
    kube = StubKube(items=[probe], infos={"g1": Info(gpu=1)})
    d.Collector(kube, keep_hours=24).start(wait=True)
    assert "dash-probe-old-1" in kube.deleted and len(kube.applied) == 2
    gpu = _kept_pod("g1", name="dash-gpu-old-1", role="dashboard-gpu")
    kube = StubKube(items=[probe, gpu], infos={"g1": Info(gpu=1)})
    c = d.Collector(kube, keep_hours=24)
    c.start(wait=True)
    assert kube.applied == [] and c.pods["g1"] == ("dash-probe-old-1", "dash-gpu-old-1")


def test_a_kept_pod_is_replaced_before_its_own_deadline():
    now = [1000.0]
    kube = StubKube(items=[_kept_pod("w1", age=2 * 3600, life=3 * 3600)], infos={"w1": Info()})
    c = d.Collector(kube, clock=lambda: now[0], keep_hours=3)
    c.start(wait=True)
    c.refresh()
    assert kube.applied == []
    now[0] += 3600 - d.RENEW_MARGIN + 1
    c.refresh()
    deadline = time.time() + 5
    while time.time() < deadline and not kube.applied:
        time.sleep(0.05)
    assert kube.applied and kube.applied[0]["metadata"]["labels"]["persist"] == "1"


def test_cleanup_probes_lists_and_deletes():
    kube = StubKube(items=[_kept_pod("w1"), _kept_pod("w2", name="dash-probe-old-2")])
    assert d.cleanup_probes(kube) == 0
    assert set(kube.deleted) == {"dash-probe-old-1", "dash-probe-old-2"}
    assert any("app=stress-test-dashboard" in " ".join(r) for r in kube.ran)
    assert d.cleanup_probes(StubKube()) == 0


def test_menu_adds_the_hours_to_the_dashboard_commands():
    from stress_test import menu
    cfg = {"keep_hours": 12}
    assert menu.apply_settings(["--dashboard"], cfg) == ["--dashboard", "--keep-probes", "12"]
    assert menu.apply_settings(["--nodes-view"], cfg) == ["--nodes-view", "--keep-probes", "12"]
    assert menu.apply_settings(["--nodes-view"], {"keep_hours": 0}) == ["--nodes-view"]
    assert menu.apply_settings(["--status"], cfg) == ["--status"]


def test_the_pods_block_shows_how_the_probe_lives():
    v = d.NodeView("w1")
    v.probe_born, v.probe_life, v.probe_kept, v.probe_reused = time.time() - 7200, 86400, True, True
    text = "\n".join(d.block_pods(v, False, 10))
    assert "kept" in text and "reused" in text and "22 h" in text
    v.probe_kept = False
    assert "temporary" in "\n".join(d.block_pods(v, False, 10))


def test_the_narrow_and_middle_layouts_show_the_probe_life_too():
    v = d.NodeView("w1")
    v.probe_born, v.probe_life, v.probe_kept = time.time() - 600, 3600, True
    rows = d.pods_rows(v, False, 5)
    assert rows[0].startswith("probe kept") and "50 min" in rows[0] and rows[-1] == "no pods"
    assert d.pods_rows(d.NodeView("w2"), False, 5) == ["no pods"]


# ---------------- nodes that go away, odd pods ----------------------------------------------------------------------

def test_a_pod_of_a_node_that_is_not_ready_or_gone_is_not_reused_and_removed():
    gone = _kept_pod("gone", name="dash-probe-old-gone")
    down = _kept_pod("down", name="dash-probe-old-down")
    kube = StubKube(items=[gone, down], infos={"down": Info(ready=False), "w1": Info()})
    c = d.Collector(kube, keep_hours=24)
    c.start(wait=True)
    assert {"dash-probe-old-gone", "dash-probe-old-down"} <= set(kube.deleted)
    assert [p["spec"]["nodeName"] for p in kube.applied] == ["w1"]                  # nothing is made on a node that is not Ready


def test_failed_terminating_master_and_duplicate_pods_are_never_reused():
    terminating = _kept_pod("w1", name="t")
    terminating["metadata"]["deletionTimestamp"] = _stamp(1)
    failed = _kept_pod("w1", name="f", phase="Failed")                              # the deadline passed
    master = _kept_pod("m", name="m")
    first, second = _kept_pod("w2", name="a"), _kept_pod("w2", name="b")           # two dashboards raced
    kube = StubKube(items=[terminating, failed, master, first, second],
                    infos={"w1": Info(), "m": Info(master=True), "w2": Info()})
    c = d.Collector(kube, keep_hours=24)
    c.start(wait=True)
    assert {"t", "f", "m"} <= set(kube.deleted) and ("a" in kube.deleted) != ("b" in kube.deleted)
    assert c.pods["w2"][0] in ("a", "b") and c.pods["w2"][0] not in kube.deleted


def test_a_node_that_stops_answering_does_not_break_the_screen_and_its_kept_pod_is_not_deleted():
    from stress_test.kube import KubectlError

    class Dying(StubKube):
        down = False

        def exec(self, *a, **k):
            if self.down:
                raise KubectlError("the node is gone")
            return ""

        def apply_image_pod(self, *a, **k):
            if self.down:
                raise KubectlError("the node is gone")
            super().apply_image_pod(*a, **k)

    kube = Dying(items=[_kept_pod("w1")], infos={"w1": Info()})
    now = [0.0]
    c = d.Collector(kube, clock=lambda: now[0], keep_hours=24)
    c.start(wait=True)
    kube.down = True
    for _ in range(d.FAILS_TO_RENEW + 2):
        c.refresh()
        now[0] += 1
    deadline = time.time() + 5
    while time.time() < deadline and c._renewing:
        time.sleep(0.05)
    assert c.nodes["w1"].probe_ok is False and c.pods["w1"][0] == "dash-probe-old-1"      # the old pod stays; the replacement did not start
    assert c._retry_at.get("w1", 0) > now[0] - 1                                             # and it is not retried every round
    c.stop()
    assert "dash-probe-old-1" not in kube.deleted


# ---------------- two dashboards at the same time ------------------------------------------------------------------

def test_a_second_dashboard_never_touches_the_temporary_pods_of_the_first():
    other = _kept_pod("master", name="dash-probe-other-1", keep=False)            # the master's temporary pod of a dashboard open elsewhere
    kube = StubKube(items=[other], infos={"w1": Info(), "master": Info(master=True)})
    c = d.Collector(kube, keep_hours=24)
    c.start(wait=True)
    assert "dash-probe-other-1" not in kube.deleted
    c.stop()
    assert "dash-probe-other-1" not in kube.deleted
    selectors = [" ".join(r) for r in kube.ran if "-l" in r]
    assert selectors and all(f"run-id={c.rid}" in s for s in selectors)           # the label cleanup is limited to this run
