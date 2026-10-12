"""PC1・PC2からPC3につながらないときの原因調べ（PC3で実行する）。

使い方（pos フォルダで）:

  1) サーバーを起動したまま、別のターミナルで状態を調べる
       uv run python tools/diagnose_network.py

  2) サーバーを止めて、試験用の受け口を開く。PC1・PC2から表示されたURLを開き、
     PC3まで届くかを確かめる（届けばターミナルに記録が出る）
       uv run python tools/diagnose_network.py --listen

オプション: --port（既定 8443）
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import socket
import ssl
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gakusai_pos.tls import local_ips

IS_WINDOWS = sys.platform == "win32"
VIRTUAL_WORDS = (
    "vethernet", "wsl", "hyper-v", "virtualbox", "vmware", "docker",
    "tailscale", "zerotier", "vpn", "bluetooth", "loopback",
)  # fmt: skip

problems: list[str] = []


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def warn(msg: str) -> None:
    print(f"  [注意] {msg}")
    problems.append(msg)


def ng(msg: str) -> None:
    print(f"  [NG]   {msg}")
    problems.append(msg)


def info(msg: str) -> None:
    print(f"         {msg}")


def section(title: str) -> None:
    print()
    print(f"■ {title}")


def ps_json(script: str) -> list[dict] | None:
    """PowerShellを実行し、結果（ConvertTo-Json）をリストで返す。失敗したら None。"""
    cmd = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        "$ErrorActionPreference = 'Stop';"
        f"@({script}) | ConvertTo-Json -Depth 3 -Compress"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        info(f"PowerShellの実行に失敗: {e}")
        return None
    out = r.stdout.decode("utf-8", errors="replace").strip()
    if r.returncode != 0:
        info("PowerShellの実行に失敗: " + r.stderr.decode("utf-8", errors="replace").strip()[:300])
        return None
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def is_virtual(alias: str) -> bool:
    a = alias.lower()
    return any(w in a for w in VIRTUAL_WORDS)


# ---------------------------------------------------------------- 1. IPアドレス


def check_addresses() -> dict[str, str]:
    """IP -> アダプタ名 を返す（Windows以外は空）。"""
    section("1. IPアドレスとネットワーク")
    ips = local_ips()
    if not ips:
        ng("ネットワークのIPアドレスが見つかりません。Wi-Fiにつながっているか確認してください")
        return {}
    info(f"サーバーが表示するURL・QRコードのIP: {ips[0]}（候補: {', '.join(ips)}）")

    alias_of: dict[str, str] = {}
    if not IS_WINDOWS:
        return alias_of

    addrs = ps_json(
        "Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress, InterfaceAlias"
    )
    routes = ps_json(
        "Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue"
        " | Select-Object InterfaceAlias, NextHop, RouteMetric"
    )
    profiles = ps_json(
        "Get-NetConnectionProfile | Select-Object InterfaceAlias, Name,"
        " @{n='Category';e={$_.NetworkCategory.ToString()}}"
    )
    for a in addrs or []:
        alias_of[a["IPAddress"]] = a["InterfaceAlias"]
    for ip in ips:
        alias = alias_of.get(ip, "?")
        mark = "（仮想アダプタ）" if is_virtual(alias) else ""
        info(f"  {ip:<16} {alias}{mark}")

    first_alias = alias_of.get(ips[0], "")
    if is_virtual(first_alias):
        ng(
            f"QRコードのIP {ips[0]} は仮想アダプタ「{first_alias}」のものです。"
            "PC1・PC2からは届きません。Wi-FiのIPを使ってください"
        )
    gw_aliases = {r["InterfaceAlias"] for r in routes or []}
    if routes:
        for r in routes:
            info(f"  既定の経路: {r['InterfaceAlias']} → ゲートウェイ {r['NextHop']}")
    wifi_ips = [ip for ip in ips if alias_of.get(ip) in gw_aliases and not is_virtual(alias_of[ip])]
    if wifi_ips and ips[0] not in wifi_ips:
        warn(f"PC1・PC2では https://{wifi_ips[0]}:PORT/ を開いてください（QRのIPとは別）")
    elif wifi_ips:
        ok(f"QRコードのIP {ips[0]} はWi-Fi（{first_alias}）のものです")

    for p in profiles or []:
        if p["Category"] == "Public":
            warn(
                f"「{p['InterfaceAlias']}」（{p['Name']}）は『パブリック ネットワーク』です。"
                "ファイアウォールでパブリックの受信を許可している必要があります（下の 3. を参照）"
            )
        else:
            ok(f"「{p['InterfaceAlias']}」（{p['Name']}）は {p['Category']} ネットワークです")
    return alias_of


# ---------------------------------------------------------------- 2. サーバー


def check_server(port: int) -> str | None:
    """待ち受け中ならその実行ファイルのパスを返す。"""
    section(f"2. サーバー（ポート {port}）")
    exe = None
    if IS_WINDOWS:
        rows = ps_json(
            f"Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue"
            " | ForEach-Object { [pscustomobject]@{ Addr = $_.LocalAddress;"
            " Path = (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).Path } }"
        )
        if rows is not None:
            if not rows:
                ng(
                    f"ポート {port} で待ち受けているプログラムがありません。"
                    "`uv run gakusai-pos serve --port 8443` を起動してから実行してください"
                )
                return None
            for r in rows:
                info(f"待ち受け: {r['Addr']}:{port}  {r['Path']}")
                exe = exe or r["Path"]
            if not any(r["Addr"] in ("0.0.0.0", "::") for r in rows):
                ng("全アドレス（0.0.0.0）で待ち受けていません。--host 0.0.0.0 で起動してください")
            else:
                ok("全アドレス（0.0.0.0）で待ち受けています")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for ip in ["127.0.0.1", *local_ips()]:
        try:
            with (
                socket.create_connection((ip, port), timeout=3) as raw,
                ctx.wrap_socket(raw) as s,
            ):
                s.sendall(f"GET / HTTP/1.1\r\nHost: {ip}\r\nConnection: close\r\n\r\n".encode())
                status = s.recv(64).split(b"\r\n")[0].decode(errors="replace")
            ok(f"PC3自身から https://{ip}:{port}/ に接続できます（{status}）")
        except Exception as e:
            ng(f"PC3自身から https://{ip}:{port}/ に接続できません: {e}")
    info(
        "※ PC3自身からの接続はファイアウォールを通らないため、ここがOKでも外から届くとは限りません"
    )
    return exe


# ---------------------------------------------------------------- 3. ファイアウォール


def check_firewall(port: int, exe: str | None) -> None:
    section("3. Windowsファイアウォール")
    if not IS_WINDOWS:
        info("Windows以外は対象外です（macOSは システム設定 → ネットワーク → ファイアウォール）")
        return
    profiles = ps_json(
        "Get-NetFirewallProfile | Select-Object Name, @{n='Enabled';e={[bool]$_.Enabled}}"
    )
    for p in profiles or []:
        info(f"{p['Name']}: {'有効' if p['Enabled'] else '無効'}")
    active = ps_json("Get-NetConnectionProfile | ForEach-Object { $_.NetworkCategory.ToString() }")
    active_cats = {c if isinstance(c, str) else str(c) for c in (active or [])}
    active_cats = {"Domain" if c == "DomainAuthenticated" else c for c in active_cats}
    info(f"いま使われているネットワークの種類: {', '.join(sorted(active_cats)) or '不明'}")

    rules = firewall_rules(port)
    if rules is None:
        warn("ファイアウォールの規則を読めませんでした")
        return

    def covers(rule: dict) -> bool:
        prof = rule["Profile"]
        if prof == "Any":
            return True
        return any(c in prof for c in active_cats)

    def applies(rule: dict) -> bool:
        if rule["Protocol"] not in ("TCP", "Any"):
            return False
        if rule["LocalPort"] not in ("Any", "") and str(port) not in rule["LocalPort"].split(","):
            return False
        prog = rule["Program"]
        if prog and prog != "Any":
            if not exe:
                return True  # 実行ファイルが分からないときは関係ありとみなす
            return os.path.normcase(os.path.expandvars(prog)) == os.path.normcase(exe)
        return True

    relevant = [r for r in rules if applies(r)]
    for r in rules:
        prog = r["Program"] if r["Program"] not in (None, "Any") else "-"
        info(
            f"{'許可' if r['Action'] == 'Allow' else 'ブロック'} | {r['Profile']:<16} | "
            f"{r['Protocol']}:{r['LocalPort']} | {r['Name']} | {prog}"
        )
    if not rules:
        info("このポート・Pythonに関する規則はありません")

    blocks = [r for r in relevant if r["Action"] == "Block" and covers(r)]
    allows = [r for r in relevant if r["Action"] == "Allow" and covers(r)]
    if blocks:
        for r in blocks:
            ng(
                f"ブロック規則「{r['Name']}」（{r['Profile']}）があります。"
                "ブロックは許可より優先されるので、外から届きません"
            )
        info("→ 管理者のPowerShellで次を実行してブロック規則を消してください:")
        for prog in sorted({r["Program"] for r in blocks}):
            if prog == "Any":
                continue
            info(
                f"   Get-NetFirewallApplicationFilter -Program '{prog}' | Get-NetFirewallRule"
                " | Where-Object Action -eq Block | Remove-NetFirewallRule"
            )
        for name in sorted({r["Name"] for r in blocks if r["Program"] == "Any"}):
            info(
                f"   Get-NetFirewallRule -DisplayName '{name}' | Where-Object Action -eq Block"
                " | Remove-NetFirewallRule"
            )
    if allows:
        ok(
            "いまのネットワークで受信を許可する規則があります: "
            + ", ".join(sorted({r["Name"] for r in allows}))
        )
    else:
        ng(
            f"いまのネットワーク（{', '.join(sorted(active_cats))}）でポート {port} を許可する規則がありません"
        )
        info("→ 管理者のPowerShellで:")
        info(
            f'   New-NetFirewallRule -DisplayName "gakusai-pos {port}" -Direction Inbound'
            f" -Protocol TCP -LocalPort {port} -Action Allow -Profile Any"
        )
    if exe:
        info(f"サーバーの実行ファイル: {exe}")


# netsh の出力の見出し（英語版・日本語版）→ 内部の名前
_NETSH_KEYS = {
    "rule name": "Name", "規則名": "Name",
    "enabled": "Enabled", "有効": "Enabled",
    "direction": "Direction", "方向": "Direction",
    "profiles": "Profile", "プロファイル": "Profile",
    "protocol": "Protocol", "プロトコル": "Protocol",
    "localport": "LocalPort", "ローカル ポート": "LocalPort", "ローカルポート": "LocalPort",
    "program": "Program", "プログラム": "Program",
    "action": "Action", "操作": "Action", "アクション": "Action",
}  # fmt: skip
_NETSH_VALUES = {
    "yes": "Yes", "はい": "Yes", "no": "No", "いいえ": "No",
    "in": "In", "入力": "In", "受信": "In",
    "allow": "Allow", "許可": "Allow", "block": "Block", "ブロック": "Block",
    "any": "Any", "任意": "Any", "すべて": "Any",
    "public": "Public", "パブリック": "Public", "private": "Private", "プライベート": "Private",
    "domain": "Domain", "ドメイン": "Domain",
}  # fmt: skip


def _norm(value: str) -> str:
    parts = [v.strip() for v in value.split(",")]
    return ",".join(_NETSH_VALUES.get(v.lower(), v) for v in parts)


def firewall_rules(port: int) -> list[dict] | None:
    """有効な受信規則のうち、このポートかPythonに関係するもの。管理者権限は不要（netsh）。"""
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in", "verbose"],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        info(f"netsh の実行に失敗: {e}")
        return None
    text = r.stdout.decode("oem" if IS_WINDOWS else "utf-8", errors="replace")
    rules: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        name = _NETSH_KEYS.get(key.strip().lower())
        if name is None:
            continue
        if name == "Name":
            cur = {"Name": value.strip()}
            rules.append(cur)
        elif cur is not None:
            cur[name] = value.strip() if name in ("Program", "LocalPort") else _norm(value)
    out = []
    for rule in rules:
        if rule.get("Enabled") != "Yes" or rule.get("Direction", "In") != "In":
            continue
        rule.setdefault("Protocol", "Any")
        rule.setdefault("Program", "Any")
        rule["LocalPort"] = _norm(rule.get("LocalPort", "Any"))
        rule["Profile"] = rule.get("Profile", "Any").replace(",", ", ")
        by_port = str(port) in rule["LocalPort"].split(",")
        prog = rule["Program"]
        by_prog = prog != "Any" and any(w in prog.lower() for w in ("python", "gakusai", "uv"))
        if by_port or by_prog:
            out.append(rule)
    if r.returncode != 0 or not rules:
        info("netsh の結果を読めませんでした: " + text.strip()[:200])
        return None
    return out


# ---------------------------------------------------------------- 試験用の受け口


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        print(
            f"  [{datetime.now():%H:%M:%S}] 接続あり: {self.client_address[0]} が {self.path} を開きました",
            flush=True,
        )
        body = (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<h1>PC3に届きました</h1><p>ネットワークとファイアウォールは問題ありません。</p>"
            f"<p>あなたのIP: {self.client_address[0]}</p>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def listen(port: int) -> None:
    try:
        server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError as e:
        sys.exit(f"ポート {port} を開けません（POSサーバーを止めてから実行してください）: {e}")
    print()
    print(
        "試験用の受け口を開きました（https ではなく http です）。PC1・PC2のChromeで開いてください:"
    )
    for ip in local_ips():
        print(f"  http://{ip}:{port}/")
    print()
    print(
        "  「PC3に届きました」と出る → ネットワークとファイアウォールは正常（POSサーバー側を確認）"
    )
    print("  ここにも「応答時間が長すぎます」→ PC3まで届いていない。ファイアウォールか、")
    print("     学内Wi-Fiが端末どうしの通信を禁止している（APアイソレーション）可能性が高い")
    print()
    print("  接続を待っています…（終了は Ctrl+C）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- まとめ


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="PC1・PC2からPC3につながらない原因を調べる")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--listen", action="store_true", help="試験用の受け口を開いて接続を待つ")
    args = parser.parse_args()

    if args.listen:
        listen(args.port)
        return

    print("学祭飲食POS 接続診断（PC3で実行）")
    check_addresses()
    exe = check_server(args.port)
    check_firewall(args.port, exe)

    section("まとめ")
    if problems:
        for p in problems:
            print(f"  - {p}")
    else:
        print("  PC3側の設定に問題は見つかりませんでした。")
    print()
    print(
        "  次に、POSサーバーを止めて `uv run python tools/diagnose_network.py --listen` を実行し、"
    )
    print("  PC1・PC2から表示されたURLを開いてください。それでも届かない場合は、学内Wi-Fiが")
    print(
        "  端末どうしの通信を禁止している可能性が高いです（スマホのテザリングや持参のルーターで回避）。"
    )


if __name__ == "__main__":
    main()
