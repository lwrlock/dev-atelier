from pathlib import Path
import pytest


def test_validate_upstream_url_accepts_loopback_and_rejects_ssrf():
    from cortexscope.config import validate_upstream_url

    assert validate_upstream_url("http://127.0.0.1:11434/") == "http://127.0.0.1:11434"
    assert validate_upstream_url("http://localhost:11434") == "http://localhost:11434"
    with pytest.raises(ValueError):
        validate_upstream_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(ValueError):
        validate_upstream_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        validate_upstream_url("http://example.com:11434")


def test_rapl_delta_handles_wraparound_and_bad_bounds():
    from cortexscope.sensors import rapl_delta_uj

    assert rapl_delta_uj(1000, 2500, 10000) == 1500
    # Counter wraps past max_range (10000)
    assert rapl_delta_uj(9200, 400, 10000) == 1200
    assert rapl_delta_uj(500, 500, 10000) == 0
    assert rapl_delta_uj(100, 200, 0) == 0


def test_hardware_sampler_reads_synthetic_sysfs_tree(tmp_path: Path):
    from cortexscope.sensors import HardwareSampler

    rapl_pkg = tmp_path / "powercap" / "intel-rapl:0"
    rapl_pkg.mkdir(parents=True)
    (rapl_pkg / "name").write_text("package-0\n")
    (rapl_pkg / "energy_uj").write_text("10000000\n")
    (rapl_pkg / "max_energy_range_uj").write_text("262143328850\n")

    gt0 = tmp_path / "drm" / "card0" / "device" / "tile0" / "gt0" / "freq0"
    gt0.mkdir(parents=True)
    (gt0 / "act_freq").write_text("1450\n")
    (gt0 / "throttle").write_text("0\n")

    tz = tmp_path / "thermal" / "thermal_zone7"
    tz.mkdir(parents=True)
    (tz / "type").write_text("x86_pkg_temp\n")
    (tz / "temp").write_text("61500\n")

    sampler = HardwareSampler(sysfs_root=tmp_path)
    s1 = sampler.sample(now_mono=10.0)
    assert s1.temp_c == pytest.approx(61.5, rel=1e-3)
    assert s1.gt0_act_mhz == 1450
    assert s1.gt0_throttle == 0
    assert s1.pkg_watts is None  # Needs two samples for delta watts

    (rapl_pkg / "energy_uj").write_text("35000000\n")  # +25 J in 2.0s => 12.5 W
    s2 = sampler.sample(now_mono=12.0)
    assert s2.pkg_watts == pytest.approx(12.5, rel=1e-3)


def test_hardware_sampler_graceful_when_sysfs_missing(tmp_path: Path):
    from cortexscope.sensors import HardwareSampler

    sampler = HardwareSampler(sysfs_root=tmp_path)
    snap = sampler.sample(now_mono=1.0)
    assert snap.pkg_watts is None
    assert snap.gt0_act_mhz is None
    assert snap.cpu_pct >= 0.0
    assert snap.ram_total_gb > 0.0


def test_stream_profiler_splits_thinking_and_computes_quantiles_and_energy():
    from cortexscope.profiler import StreamRunTracker, compute_quantiles

    q = compute_quantiles([10.0, 20.0, 30.0, 40.0, 100.0])
    assert q["p50"] == pytest.approx(30.0)
    assert q["p95"] >= 40.0
    assert q["max"] == 100.0

    tracker = StreamRunTracker(
        run_id="r1",
        model="qwen3.5-hizli:latest",
        source="proxy",
        endpoint="/api/chat",
        prompt_preview="hello",
        start_ns=1_000_000_000,
        start_energy={"package-0": 1_000_000, "psys": 2_000_000},
        max_energy={"package-0": 100_000_000, "psys": 100_000_000},
    )
    ev1 = tracker.on_chunk(
        {"message": {"role": "assistant", "thinking": "step1 "}, "done": False},
        now_ns=1_120_000_000,
    )
    ev2 = tracker.on_chunk(
        {"message": {"role": "assistant", "content": "Answer"}, "done": False},
        now_ns=1_160_000_000,
    )
    ev3 = tracker.on_chunk(
        {"message": {"role": "assistant", "content": "!"}, "done": False},
        now_ns=1_200_000_000,
    )
    assert ev1.phase == "think"
    assert ev2.phase == "answer"
    assert ev3.delta_ms == pytest.approx(40.0)

    summary = tracker.finalize(
        final_chunk={
            "done": True,
            "prompt_eval_count": 12,
            "eval_count": 3,
            "load_duration": 20_000_000,
            "prompt_eval_duration": 50_000_000,
            "eval_duration": 100_000_000,
            "total_duration": 200_000_000,
        },
        now_ns=1_220_000_000,
        end_energy={"package-0": 4_000_000, "psys": 8_000_000},  # +3 J pkg, +6 J psys
        peak_temp_c=68.5,
        peak_gt_mhz=1800,
    )
    assert summary["ttft_ms"] == pytest.approx(120.0)
    assert summary["think_tokens"] == 1
    assert summary["answer_tokens"] == 2
    assert summary["tps"] == pytest.approx(30.0)
    assert summary["pkg_joules"] == pytest.approx(3.0)
    assert summary["psys_joules"] == pytest.approx(6.0)
    assert summary["joules_per_100tok"] == pytest.approx(100.0)


def test_context_budget_estimator():
    from cortexscope.profiler import estimate_context_budget

    b = estimate_context_budget(
        prompt="Explain Intel RAPL wraparound math in detail.",
        system_prompt="You are concise.",
        messages=[{"role": "user", "content": "prior turn"}],
        context_length=4096,
    )
    assert b["estimated_tokens"] > 5
    assert 0.0 < b["utilization_pct"] < 100.0
    assert b["pressure_level"] == "low"
