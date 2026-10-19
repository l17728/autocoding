"""Check DNS/TCP/TLS to Zhipu without reading credentials or calling a model."""
import json
import socket
import ssl


def main():
    report = {"host": "open.bigmodel.cn", "port": 443, "credentials_used": False}
    stage = "dns"
    try:
        socket.getaddrinfo(report["host"], 443, type=socket.SOCK_STREAM)
        report["dns"] = "ok"
        stage = "tcp"
        with socket.create_connection((report["host"], 443), timeout=8) as connection:
            report["tcp"] = "ok"
            stage = "tls"
            with ssl.create_default_context().wrap_socket(connection, server_hostname=report["host"]) as secured:
                report["tls"] = secured.version()
        report["status"] = "reachable"
    except OSError as exc:
        report.update(status="blocked", stage=stage, error_type=type(exc).__name__,
                      errno=exc.errno, winerror=getattr(exc, "winerror", None))
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "reachable" else 2


if __name__ == "__main__":
    raise SystemExit(main())
