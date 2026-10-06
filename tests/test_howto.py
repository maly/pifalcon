"""Kontroly konfigurace z HOWTO.md (laser-bridge, camera-stream, firewall).

Skript, systemd unity i pravidla nftables existují jen jako bloky kódu v HOWTO.md,
proto je testy vytahují přímo odtud. Integrační testy (skutečný ser2net,
systemd-analyze, nft) se přeskočí, pokud příslušný nástroj není k dispozici.
"""

import ipaddress
import os
import pty
import re
import select
import shutil
import socket
import stat
import subprocess
import time
import tty
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOWTO = ROOT / "HOWTO.md"
CAMERA_CONTROLS = ROOT / "camera" / "camera_controls.py"
CAMERA_DEFAULTS = ROOT / "camera" / "camera-controls.default"

FENCE_RE = re.compile(r"^```(\w*)\n(.*?)^```\s*$", re.S | re.M)
REQUIRED_PORTS = {22, 23, 8080, 8081}
NFT = shutil.which("nft") or shutil.which("nft", path="/usr/sbin:/sbin")


def code_blocks():
    return [(lang, body) for lang, body in FENCE_RE.findall(HOWTO.read_text(encoding="utf-8"))]


def find_block(lang, marker):
    matches = [body for block_lang, body in code_blocks() if block_lang == lang and marker in body]
    assert len(matches) == 1, f"čekán právě jeden blok ```{lang} obsahující {marker!r}, nalezeno {len(matches)}"
    return matches[0]


def laser_script():
    return find_block("sh", "DEFAULTS=/etc/default/laser-bridge")


def unit_directives(unit_text):
    directives = {}
    section = None
    for line in unit_text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        key, _, value = line.partition("=")
        directives.setdefault((section, key.strip()), []).append(value.strip())
    return directives


def laser_unit():
    return find_block("ini", "ExecStart=/usr/local/sbin/laser-bridge")


def camera_stream_unit():
    return find_block("ini", "ExecStart=/usr/local/sbin/camera-stream")


def nft_rules():
    return find_block("nft", "table inet pifalcon")


def default_allowed_networks():
    source = CAMERA_CONTROLS.read_text(encoding="utf-8")
    match = re.search(r'"ALLOWED_NETWORKS",\s*"([^"]+)"', source)
    assert match, "výchozí ALLOWED_NETWORKS v camera_controls.py nenalezeno"
    return {ipaddress.ip_network(n.strip()) for n in match.group(1).split(",") if n.strip()}


# --- kickolduser a generování konfigurace -------------------------------------------------


def test_laser_bridge_does_not_kick_existing_client():
    script = laser_script()
    assert "kickolduser: false" in script
    assert "kickolduser: true" not in HOWTO.read_text(encoding="utf-8").replace(
        "Původní `kickolduser: true`", ""
    )


def test_howto_explains_kickolduser_and_restart():
    text = HOWTO.read_text(encoding="utf-8")
    assert "`kickolduser: false`" in text
    assert "sudo systemctl restart laser-bridge.service" in text


def test_laser_bridge_config_is_generated_into_run_not_etc():
    script = laser_script()
    assert "/etc/ser2net.yaml" not in script
    assert "install -o root" not in script
    assert 'RUNTIME_DIR="${RUNTIME_DIRECTORY:-/run/laser-bridge}"' in script
    assert 'CONFIG="$RUNTIME_DIR/ser2net.yaml"' in script
    assert '-P "$PIDFILE"' in script and 'PIDFILE="$RUNTIME_DIR/ser2net.pid"' in script


# --- systemd unity bez roota -----------------------------------------------------------------

HARDENING = {
    "DynamicUser": "yes",
    "NoNewPrivileges": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "PrivateTmp": "yes",
}


@pytest.mark.parametrize(
    "unit_getter, extra",
    [
        (
            laser_unit,
            {
                "SupplementaryGroups": "dialout",
                "RuntimeDirectory": "laser-bridge",
                "AmbientCapabilities": "CAP_NET_BIND_SERVICE",
                "CapabilityBoundingSet": "CAP_NET_BIND_SERVICE",
                "ReadWritePaths": "/run/lock",
            },
        ),
        (camera_stream_unit, {"SupplementaryGroups": "video", "CapabilityBoundingSet": ""}),
    ],
    ids=["laser-bridge", "camera-stream"],
)
def test_service_units_run_unprivileged(unit_getter, extra):
    directives = unit_directives(unit_getter())
    for key, value in {**HARDENING, **extra}.items():
        assert directives.get(("Service", key)) == [value], key
    assert ("Service", "User") not in directives
    assert ("Service", "PrivateDevices") not in directives


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze není k dispozici")
@pytest.mark.parametrize("name, unit_getter", [("laser-bridge", laser_unit), ("camera-stream", camera_stream_unit)])
def test_service_units_pass_systemd_analyze_verify(tmp_path, name, unit_getter):
    exec_path = tmp_path / name
    exec_path.write_text("#!/bin/sh\nexit 0\n")
    exec_path.chmod(0o755)
    unit = unit_getter().replace(f"/usr/local/sbin/{name}", str(exec_path))
    unit_path = tmp_path / f"{name}.service"
    unit_path.write_text(unit)
    result = subprocess.run(
        ["systemd-analyze", "verify", "--man=no", str(unit_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    relevant = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if unit_path.name in line or "Unknown" in line or "Failed to parse" in line
    ]
    assert result.returncode == 0 and not relevant, result.stdout + result.stderr


# --- firewall ----------------------------------------------------------------------------------


def nft_rule_lines():
    return [line.strip() for line in nft_rules().splitlines() if "dport" in line]


def parse_set(text):
    return {item.strip() for item in text.split(",") if item.strip()}


def test_firewall_restricts_all_service_ports():
    rules = nft_rule_lines()
    assert len(rules) == 3
    for line in rules:
        ports = parse_set(re.search(r"tcp dport \{([^}]*)\}", line).group(1))
        assert {int(p) for p in ports} == REQUIRED_PORTS, line
    assert rules[0].endswith("accept") and " ip saddr " in rules[0]
    assert rules[1].endswith("accept") and " ip6 saddr " in rules[1]
    assert rules[2].endswith("drop") and "saddr" not in rules[2]
    assert "iif lo accept" in nft_rules()


def test_firewall_ipv4_ranges_match_camera_allowed_networks():
    ipv4 = {ipaddress.ip_network(n) for n in parse_set(re.search(r"ip saddr \{([^}]*)\}", nft_rules()).group(1))}
    loopback = ipaddress.ip_network("127.0.0.0/8")
    expected = default_allowed_networks() - {loopback}
    assert ipv4 == expected
    assert loopback in default_allowed_networks()  # loopback pokrývá "iif lo accept"

    for text in (CAMERA_DEFAULTS.read_text(encoding="utf-8"), HOWTO.read_text(encoding="utf-8")):
        configured = re.search(r"^ALLOWED_NETWORKS=(.+)$", text, re.M).group(1)
        assert {ipaddress.ip_network(n) for n in configured.split(",")} == default_allowed_networks()


def test_firewall_ipv6_only_link_local_and_ula():
    ipv6 = {ipaddress.ip_network(n) for n in parse_set(re.search(r"ip6 saddr \{([^}]*)\}", nft_rules()).group(1))}
    assert ipv6 == {ipaddress.ip_network("fe80::/10"), ipaddress.ip_network("fc00::/7")}


def test_howto_firewall_step_is_mandatory_and_persistent():
    text = HOWTO.read_text(encoding="utf-8")
    assert "### 7. Omezení přístupu k portům (povinné)" in text
    assert "/etc/nftables.d/pifalcon.nft" in text
    assert 'include "/etc/nftables.d/*.nft"' in text
    assert "sudo systemctl enable nftables.service" in text
    assert "Pokud na Pi používáte firewall" not in text


def test_howto_verification_covers_firewall_user_and_second_connection():
    verification = HOWTO.read_text(encoding="utf-8").split("### 8. Ověření výsledku", 1)[1]
    assert "sudo nft list ruleset | grep -E 'dport'" in verification
    assert "systemctl show -p User,DynamicUser laser-bridge.service" in verification
    assert "nc 192.168.0.99 23" in verification


@pytest.mark.skipif(not NFT, reason="nft není k dispozici")
def test_firewall_rules_pass_nft_check(tmp_path):
    rules = tmp_path / "pifalcon.nft"
    rules.write_text(nft_rules())
    result = subprocess.run([NFT, "-c", "-f", str(rules)], capture_output=True, text=True, timeout=30)
    if result.returncode != 0 and "Operation not permitted" in result.stderr:
        pytest.skip("nft -c vyžaduje CAP_NET_ADMIN (spusťte jako root)")
    assert result.returncode == 0, result.stderr


# --- integrační test: skutečný ser2net jako neprivilegovaný proces ---------------------------


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_fd(fd, expected, timeout=5.0):
    data = b""
    deadline = time.monotonic() + timeout
    while expected not in data and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            data += os.read(fd, 1024)
    return data


def recv_until(sock, expected, timeout=5.0):
    sock.settimeout(0.1)
    data = b""
    deadline = time.monotonic() + timeout
    while expected not in data and time.monotonic() < deadline:
        try:
            chunk = sock.recv(1024)
        except socket.timeout:
            continue
        if not chunk:
            break
        data += chunk
    return data


def recv_until_closed(sock, timeout=5.0):
    sock.settimeout(timeout)
    data = b""
    while True:
        chunk = sock.recv(1024)  # socket.timeout = spojení nebylo ukončeno -> test selže
        if not chunk:
            return data
        data += chunk


def connect(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=1)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


@pytest.mark.skipif(not os.path.exists("/usr/sbin/ser2net"), reason="ser2net není nainstalovaný")
def test_laser_bridge_script_rejects_second_client_and_keeps_first(tmp_path):
    master, slave = pty.openpty()
    tty.setraw(slave)
    device = os.ttyname(slave)
    port = free_port()

    defaults = tmp_path / "laser-bridge.default"
    defaults.write_text(
        f"LASER_DEVICE={device}\nLASER_BAUD=115200\nLASER_PORT={port}\nLISTEN_ADDRESS=127.0.0.1\nWAIT_SECONDS=1\n"
    )
    script = laser_script()
    assert "DEFAULTS=/etc/default/laser-bridge\n" in script
    script_path = tmp_path / "laser-bridge"
    script_path.write_text(script.replace("DEFAULTS=/etc/default/laser-bridge\n", f"DEFAULTS={defaults}\n"))
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir(mode=0o750)

    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "RUNTIME_DIRECTORY": str(runtime_dir)}
    proc = subprocess.Popen(
        ["sh", str(script_path)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    first = second = None
    try:
        first = connect(port)

        config = runtime_dir / "ser2net.yaml"
        assert config.exists()
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert "kickolduser: false" in config.read_text()
        assert f"accepter: tcp,127.0.0.1,{port}" in config.read_text()
        assert [p.name for p in runtime_dir.iterdir() if p.name.startswith("ser2net.yaml.")] == []

        # Spojení klient -> sériový port -> klient funguje.
        time.sleep(0.3)
        first.sendall(b"?\n")
        assert b"?\n" in read_fd(master, b"?\n")
        os.write(master, b"<Idle>\r\n")
        assert b"<Idle>" in recv_until(first, b"<Idle>")

        # Druhý klient je odmítnut (spojení se zavře) ...
        second = connect(port)
        refusal = recv_until_closed(second)
        assert b"in use" in refusal.lower() or refusal == b""

        # ... a první klient zůstává připojen a dál komunikuje.
        first.sendall(b"G0 X1\n")
        assert b"G0 X1\n" in read_fd(master, b"G0 X1\n")
        os.write(master, b"ok\r\n")
        assert b"ok" in recv_until(first, b"ok")
        assert proc.poll() is None
    finally:
        for sock in (first, second):
            if sock is not None:
                sock.close()
        proc.terminate()
        try:
            output = proc.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            output = proc.communicate()[0]
        os.close(master)
        os.close(slave)
    assert b"rror" not in output, output.decode(errors="replace")
