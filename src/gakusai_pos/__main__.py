"""コマンドライン：serve / init-db / backup / restore / reset-passcode。"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import sqlite3
import sys
from pathlib import Path

from .db import DB_FILENAME, Tx, backup_to, connect, init_db, log_event, run_backup, set_setting

LOOPBACK_HOST = "127.0.0.1"


def _data_dir(args) -> Path:
    return Path(args.data_dir or os.environ.get("GAKUSAI_POS_DATA") or "data").resolve()


def _print_qr(url: str) -> None:
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    try:
        qr.print_ascii(invert=True)
    except Exception:  # 端末が罫線文字を出せない場合は省略
        pass


def _print_passcode(passcode: str | None) -> None:
    if passcode:
        print("=" * 60)
        print(f"  初期パスコード: {passcode}")
        print("  （PC1・PC2の接続に使います。管理画面の「設定」で変更してください）")
        print("=" * 60)


def cmd_serve(args) -> None:
    import uvicorn

    from .app import create_app
    from .tls import ensure_cert, local_ips

    data_dir = _data_dir(args)
    scheme = "http" if args.http else "https"
    app = create_app(data_dir, port=args.port, scheme=scheme)
    ssl = {}
    if scheme == "https":
        cert, key = ensure_cert(data_dir, local_ips())
        ssl = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}

    ips = app.state.local_ip_list
    print()
    print("学祭飲食POS サーバーを起動します")
    print(f"  データ:   {data_dir}")
    admin_port = args.admin_port if scheme == "https" else 0
    if admin_port and not _port_free(LOOPBACK_HOST, admin_port):
        print(f"  ※ ポート {admin_port} が使用中のため、管理画面の http 版は起動しません")
        admin_port = 0
    if admin_port:
        # http://localhost はChromeが安全な接続として扱うので、証明書の警告が出ない
        print(f"  管理画面: http://localhost:{admin_port}/admin  （このPCのChromeで開く）")
    else:
        print(f"  管理画面: {scheme}://localhost:{args.port}/admin  （このPCのChromeで開く）")
    if not ips:
        print("  ※ ネットワークのIPアドレスが見つかりません。Wi-Fiの接続を確認してください")
    for ip in ips:
        url = f"{scheme}://{ip}:{args.port}/"
        print(f"  受付・厨房: {url}")
    if ips:
        _print_qr(f"{scheme}://{ips[0]}:{args.port}/")
    _print_passcode(app.state.initial_passcode)
    print("  終了するには Ctrl+C")
    print(flush=True)
    configs = [uvicorn.Config(app, host=args.host, port=args.port, log_level="warning", **ssl)]
    if admin_port:
        # 同じアプリをPC3自身からだけ http で開けるようにする（起動・終了処理は本体側で1回だけ）
        configs.append(
            uvicorn.Config(
                app, host=LOOPBACK_HOST, port=admin_port, log_level="warning", lifespan="off"
            )
        )
    _run_servers([uvicorn.Server(c) for c in configs])


def _port_free(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _run_servers(servers) -> None:
    import asyncio

    async def run_all() -> None:
        tasks = [asyncio.create_task(s.serve()) for s in servers]
        try:
            # どれかが止まったら（Ctrl+Cを含む）全部止める
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for s in servers:
                s.should_exit = True
            await asyncio.gather(*tasks, return_exceptions=True)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_all())


def cmd_init_db(args) -> None:
    data_dir = _data_dir(args)
    conn = connect(data_dir / DB_FILENAME)
    passcode = init_db(conn)
    conn.close()
    print(f"DBを準備しました: {data_dir / DB_FILENAME}")
    _print_passcode(passcode)
    if not passcode:
        print("（既存のDBです。データはそのままです）")


def cmd_backup(args) -> None:
    data_dir = _data_dir(args)
    db_path = data_dir / DB_FILENAME
    if not db_path.exists():
        sys.exit(f"DBがありません: {db_path}")
    conn = connect(db_path)
    if args.out:
        print("保存しました:", backup_to(conn, Path(args.out).resolve()))
    else:
        result = run_backup(conn, data_dir)
        print("保存しました:", result["path"])
        if result["usb_path"]:
            print("USBにも保存しました:", result["usb_path"])
        if result["usb_error"]:
            print("USBへの保存に失敗:", result["usb_error"])
    conn.close()


def cmd_restore(args) -> None:
    data_dir = _data_dir(args)
    src = Path(args.file).resolve()
    if not src.exists():
        sys.exit(f"ファイルがありません: {src}")
    check = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            sys.exit("バックアップが壊れています")
        check.execute("SELECT COUNT(*) FROM orders").fetchone()
    except sqlite3.DatabaseError as e:
        sys.exit(f"POSのバックアップとして読めません: {e}")
    db_path = data_dir / DB_FILENAME
    if not args.yes:
        ans = input(
            f"{db_path} を {src.name} の内容で置き換えます。サーバーは止めましたか？ [y/N] "
        )
        if ans.strip().lower() != "y":
            sys.exit("中止しました")
    if db_path.exists():
        cur = connect(db_path)
        saved = backup_to(cur, data_dir / "backups" / "before-restore.db")
        cur.close()
        print("置き換え前のDBを保存しました:", saved)
    dest = connect(db_path)
    check.backup(dest)
    check.close()
    init_db(dest)
    dest.close()
    print("復元しました:", db_path)


def cmd_reset_passcode(args) -> None:
    from .auth import hash_passcode

    data_dir = _data_dir(args)
    conn = connect(data_dir / DB_FILENAME)
    init_db(conn)
    passcode = args.passcode or getpass.getpass("新しいパスコード（4文字以上）: ")
    if len(passcode) < 4:
        sys.exit("4文字以上にしてください")
    with Tx(conn):
        set_setting(conn, "passcode_hash", hash_passcode(passcode))
        set_setting(conn, "passcode_changed", "1")
        log_event(conn, "PC3管理", "settings_updated", None, {"passcode": "（変更）"})
    conn.close()
    print("パスコードを変更しました。PC1・PC2は再登録が必要です")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gakusai-pos", description="学祭飲食POSシステム")
    parser.add_argument("--data-dir", help="DB・証明書・バックアップの置き場所（既定: ./data）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="サーバーを起動する")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8443)
    p.add_argument(
        "--admin-port",
        type=int,
        default=8080,
        help="PC3の管理画面を http://localhost で開くポート（既定: 8080。0で無効）",
    )
    p.add_argument(
        "--http", action="store_true", help="HTTPSを使わない（開発用。カメラは使えない）"
    )
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("init-db", help="DBを作る（既存のデータは消さない）")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("backup", help="DBをバックアップする")
    p.add_argument("--out", help="保存先のファイル名（省略時は data/backups/）")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="バックアップからDBを戻す（サーバー停止中に行う）")
    p.add_argument("file")
    p.add_argument("-y", "--yes", action="store_true", help="確認しない")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("reset-passcode", help="端末の接続用パスコードを変更する")
    p.add_argument("passcode", nargs="?")
    p.set_defaults(func=cmd_reset_passcode)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
