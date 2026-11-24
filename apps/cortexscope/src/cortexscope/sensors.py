"""Linux hardware telemetry sampler (Intel RAPL, thermal zones, xe GT, CPU/RAM)."""

from __future__ import annotations

import glob
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil


def rapl_delta_uj(prev_uj: int, curr_uj: int, max_range_uj: int) -> int:
    """Compute microjoule delta handling 32/64-bit RAPL counter wraparound."""
    if max_range_uj <= 0 or prev_uj < 0 or curr_uj < 0:
        return 0
    if curr_uj >= prev_uj:
        return curr_uj - prev_uj
    if prev_uj > max_range_uj:
        return 0
    return (max_range_uj - prev_uj) + curr_uj


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _read_str(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


@dataclass(slots=True)
class HardwareSnapshot:
    ts: float
    pkg_watts: float | None
    core_watts: float | None
    uncore_watts: float | None
    psys_watts: float | None
    temp_c: float | None
    cpu_pct: float
    cpu_freq_mhz: float | None
    ram_used_gb: float
    ram_total_gb: float
    swap_used_gb: float
    gt0_act_mhz: int | None
    gt1_act_mhz: int | None
    gt0_throttle: int | None
    capabilities: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HardwareSampler:
    """Samples Linux sysfs RAPL, thermal zones, Intel xe GT, and psutil metrics."""

    def __init__(self, sysfs_root: Path | str = "/sys/class") -> None:
        self.sysfs_root = Path(sysfs_root)
        self._rapl_nodes = self._discover_rapl()
        self._thermal_pkg_path = self._discover_pkg_thermal()
        self._gt_paths = self._discover_xe_gt()
        self._prev_energy: dict[str, int] = {}
        self._prev_mono: float | None = None
        # Prime psutil cpu_percent non-blocking baseline.
        psutil.cpu_percent(interval=None)

    def _discover_rapl(self) -> dict[str, tuple[Path, int]]:
        nodes: dict[str, tuple[Path, int]] = {}
        powercap_dir = self.sysfs_root / "powercap"
        if not powercap_dir.exists():
            return nodes
        for entry in sorted(powercap_dir.glob("intel-rapl:*")):
            name = _read_str(entry / "name")
            energy_file = entry / "energy_uj"
            max_range = _read_int(entry / "max_energy_range_uj")
            if name and energy_file.exists() and max_range and max_range > 0:
                nodes[name] = (energy_file, max_range)
        return nodes

    def _discover_pkg_thermal(self) -> Path | None:
        thermal_dir = self.sysfs_root / "thermal"
        if not thermal_dir.exists():
            return None
        preferred = ("x86_pkg_temp", "TCPU", "INT3400 Thermal")
        candidates: dict[str, Path] = {}
        for zone in sorted(thermal_dir.glob("thermal_zone*")):
            ztype = _read_str(zone / "type")
            temp_file = zone / "temp"
            if ztype and temp_file.exists():
                candidates[ztype] = temp_file
        for name in preferred:
            if name in candidates:
                return candidates[name]
        return next(iter(candidates.values()), None)

    def _discover_xe_gt(self) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        drm_dir = self.sysfs_root / "drm"
        if not drm_dir.exists():
            return paths
        for gt_idx in ("gt0", "gt1"):
            matches = sorted(drm_dir.glob(f"card*/device/tile*/{gt_idx}/freq0"))
            if matches:
                paths[gt_idx] = matches[0]
        return paths

    def read_energy_counters(self) -> tuple[dict[str, int], dict[str, int]]:
        """Return (current energy_uj per domain, max_energy_range_uj per domain)."""
        counters: dict[str, int] = {}
        max_ranges: dict[str, int] = {}
        for domain, (path, max_range) in self._rapl_nodes.items():
            val = _read_int(path)
            if val is not None:
                counters[domain] = val
                max_ranges[domain] = max_range
        return counters, max_ranges

    def _read_temp_c(self) -> float | None:
        if self._thermal_pkg_path is not None:
            raw = _read_int(self._thermal_pkg_path)
            if raw is not None and -40_000 < raw < 150_000:
                return round(raw / 1000.0, 2)
        try:
            temps = psutil.sensors_temperatures() or {}
            for key in ("coretemp", "k10temp", "acpitz"):
                entries = temps.get(key)
                if entries:
                    return round(float(entries[0].current), 2)
        except Exception:
            pass
        return None

    def sample(self, now_mono: float | None = None, now_wall: float | None = None) -> HardwareSnapshot:
        """Read current hardware telemetry and compute RAPL watts since last call."""
        mono = time.monotonic() if now_mono is None else now_mono
        wall = time.time() if now_wall is None else now_wall

        curr_energy, max_ranges = self.read_energy_counters()
        watts: dict[str, float | None] = {
            "package-0": None,
            "core": None,
            "uncore": None,
            "psys": None,
        }
        if self._prev_mono is not None and mono > self._prev_mono:
            dt = mono - self._prev_mono
            for domain in watts:
                if domain in curr_energy and domain in self._prev_energy:
                    duj = rapl_delta_uj(self._prev_energy[domain], curr_energy[domain], max_ranges[domain])
                    watts[domain] = round((duj / 1_000_000.0) / dt, 2)

        self._prev_energy = curr_energy
        self._prev_mono = mono

        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        freq = psutil.cpu_freq()

        gt0_dir = self._gt_paths.get("gt0")
        gt1_dir = self._gt_paths.get("gt1")
        gt0_act = _read_int(gt0_dir / "act_freq") if gt0_dir else None
        gt1_act = _read_int(gt1_dir / "act_freq") if gt1_dir else None
        gt0_throttle = _read_int(gt0_dir / "throttle") if gt0_dir else None

        caps = {
            "rapl": bool(self._rapl_nodes),
            "xe_gt": bool(self._gt_paths),
            "thermal": self._thermal_pkg_path is not None,
        }

        return HardwareSnapshot(
            ts=wall,
            pkg_watts=watts["package-0"],
            core_watts=watts["core"],
            uncore_watts=watts["uncore"],
            psys_watts=watts["psys"],
            temp_c=self._read_temp_c(),
            cpu_pct=round(float(psutil.cpu_percent(interval=None)), 1),
            cpu_freq_mhz=round(float(freq.current), 0) if freq else None,
            ram_used_gb=round(vm.used / (1024**3), 2),
            ram_total_gb=round(vm.total / (1024**3), 2),
            swap_used_gb=round(sm.used / (1024**3), 2),
            gt0_act_mhz=gt0_act,
            gt1_act_mhz=gt1_act,
            gt0_throttle=gt0_throttle,
            capabilities=caps,
        )
