"""Thin wrapper over `kubectl` (subprocess). Nothing else lives here."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from typing import Optional

from .models import TOOL_LABEL, NodeInfo, NodeWorkload, ToolPod
from .parsing import clean_ip, node_from_json, tool_pods_from_json, workload_from_json


log = logging.getLogger(__name__)
_MAX_LOGGED = 2000


# values of keys that look like credentials are hidden in the debug log (also inside JSON that is escaped in a JSON string)
_SECRET_RE = re.compile(r'(\\?"[\w-]*(?:TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|PRIVATE_KEY|CLIENT[-_]KEY[-_]DATA)[\w-]*\\?"\s*:\s*\\?")[^"\\]*',
                        re.IGNORECASE)


def _cut(text: str) -> str:
    text = _SECRET_RE.sub(r"\1***", text.strip())
    if len(text) <= _MAX_LOGGED:
        return text
    return text[:_MAX_LOGGED] + f"…(+{len(text) - _MAX_LOGGED} chars)"


class KubectlError(RuntimeError):
    """kubectl finished with an error or timed out."""


class Kubectl:
    def __init__(self, binary: Optional[str] = None) -> None:
        # can be overridden with the KUBECTL variable (handy for tests)
        self.binary = binary or os.environ.get("KUBECTL", "kubectl")

    # --- low level ------------------------------------------------------
    def run(self, *args: str, input_text: Optional[str] = None,
            check: bool = True, timeout: Optional[float] = 60) -> str:
        cmd = [self.binary, *args]
        note = f"   [stdin {len(input_text)} B]" if input_text else ""
        log.debug("kubectl: %s%s", " ".join(cmd), note)
        started = time.monotonic()
        try:
            proc = subprocess.run(cmd, input=input_text, capture_output=True,
                                  text=True, errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            log.error("kubectl timed out after %ss: %s", timeout, " ".join(cmd))
            raise KubectlError(f"Timed out: {' '.join(cmd)}") from exc
        except FileNotFoundError as exc:
            log.error("kubectl not found: %s", self.binary)
            raise KubectlError(f"Not found: {self.binary}") from exc
        log.debug("  -> rc=%s in %.2f s | stdout=%r | stderr=%r",
                  proc.returncode, time.monotonic() - started,
                  _cut(proc.stdout), _cut(proc.stderr))
        if check and proc.returncode != 0:
            log.error("kubectl failed (rc=%s): %s | stderr=%s",
                      proc.returncode, " ".join(cmd), _cut(proc.stderr))
            raise KubectlError(
                f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout

    def get_json(self, *args: str) -> dict:
        return json.loads(self.run("get", *args, "-o", "json"))

    # --- nodes --------------------------------------------------------------
    def list_node_names(self) -> list[str]:
        items = self.get_json("nodes").get("items", [])
        return [i["metadata"]["name"] for i in items]

    def get_node(self, name: str) -> NodeInfo:
        return node_from_json(self.get_json("node", name))

    def top_node(self, name: str) -> str:
        return self.run("top", "node", name, "--no-headers").strip()

    # --- pods --------------------------------------------------------------
    def list_tool_pods(self) -> list[ToolPod]:
        """All pods of this tool (also from other concurrent runs)."""
        return tool_pods_from_json(self.get_json("pods", "-l", f"app={TOOL_LABEL}"))

    def list_node_workload(self, node: str) -> NodeWorkload:
        """Pods currently running on the node (excluding this tool and kube-system)."""
        return workload_from_json(
            self.get_json("pods", "-A", "--field-selector", f"spec.nodeName={node}"))

    def gpu_in_use(self, node: str) -> int:
        """How many nvidia.com/gpu are requested by live pods on the node (0 if it cannot be found out)."""
        try:
            items = self.get_json("pods", "-A", "--field-selector", f"spec.nodeName={node}").get("items", [])
        except (KubectlError, ValueError):
            return 0
        total = 0
        for pod in items:
            if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            for c in pod.get("spec", {}).get("containers", []):
                try:
                    total += int(c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 0))
                except (TypeError, ValueError):
                    pass
        return total

    def delete_finished_tool_pods(self) -> None:
        """Deletes only finished/failed pods of the tool (leftovers), leaves running ones."""
        for phase in ("Succeeded", "Failed"):
            self.run("delete", "pod", "-l", f"app={TOOL_LABEL}",
                     f"--field-selector=status.phase={phase}",
                     "--ignore-not-found", "--now", check=False, timeout=120)

    def apply(self, manifest: dict) -> None:
        log.info("apply pod %s (node %s, deadline %ss)",
                 manifest.get("metadata", {}).get("name"),
                 manifest.get("spec", {}).get("nodeName"),
                 manifest.get("spec", {}).get("activeDeadlineSeconds"))
        self.run("apply", "-f", "-", input_text=json.dumps(manifest))

    def delete_pods(self, *names: str) -> None:
        """Deletes pods; ignores errors (a pod may not exist)."""
        self.run("delete", "pod", *names, "--ignore-not-found", "--now",
                 check=False, timeout=120)

    def wait_ready(self, pod: str, timeout_s: int) -> bool:
        """True if the pod reaches the Ready state within timeout_s seconds."""
        try:
            self.run("wait", "--for=condition=Ready", f"pod/{pod}",
                     f"--timeout={timeout_s}s", timeout=timeout_s + 30)
        except KubectlError:
            return False
        return True

    def pod_phase(self, pod: str) -> str:
        return self.run("get", "pod", pod, "-o", "jsonpath={.status.phase}",
                        check=False).strip()

    def pod_ip(self, pod: str) -> str:
        return clean_ip(self.run("get", "pod", pod, "-o", "jsonpath={.status.podIP}", check=False))

    def used_node_ports(self) -> set:
        """NodePorts taken by Services of the whole cluster (an empty set if they cannot be listed)."""
        try:
            items = self.get_json("services", "-A").get("items", [])
        except (KubectlError, ValueError):
            return set()
        return {p["nodePort"] for s in items for p in s.get("spec", {}).get("ports", []) if p.get("nodePort")}

    def service_ip(self, name: str) -> str:
        return clean_ip(self.run("get", "service", name, "-o", "jsonpath={.spec.clusterIP}", check=False))

    def delete_service(self, name: str) -> None:
        self.run("delete", "service", name, "--ignore-not-found", check=False, timeout=60)

    def logs(self, pod: str) -> str:
        return self.run("logs", pod, check=False)

    def stream_logs(self, pod: str) -> subprocess.Popen:
        log.debug("kubectl logs -f %s (stream)", pod)
        return subprocess.Popen([self.binary, "logs", "-f", pod],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace")

    def exec(self, pod: str, script: str, timeout: int = 30) -> str:
        return self.run("exec", pod, "--", "sh", "-c", script, timeout=timeout)
