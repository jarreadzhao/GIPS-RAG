"""生成本地 mTLS 测试证书（CA / server / client），用于 AC-12 验证。

只用于本机验收：私钥不加密、放在 var/certs 下，不得用于生产。
服务端证书带 SAN（localhost / 127.0.0.1）与 SERVER_AUTH，
客户端证书带 CLIENT_AUTH；uvicorn 以 CERT_REQUIRED 启动后，
不带客户端证书的握手必须失败。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

OUT = Path(__file__).resolve().parents[1] / "var" / "certs"


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _cert(cn: str, key, ca_cert, ca_key, *, ca: bool = False,
          eku: ExtendedKeyUsageOID | None = None,
          san: bool = False) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(ca_cert.subject if ca_cert is not None else _name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if eku is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
    if san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
    return builder.sign(ca_key, hashes.SHA256())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ca_key = _key()
    ca_cert = _cert("GIPS RAG Test CA", ca_key, None, ca_key, ca=True)
    server_key = _key()
    server_cert = _cert("localhost", server_key, ca_cert, ca_key,
                        eku=ExtendedKeyUsageOID.SERVER_AUTH, san=True)
    client_key = _key()
    client_cert = _cert("gips-client", client_key, ca_cert, ca_key,
                        eku=ExtendedKeyUsageOID.CLIENT_AUTH)

    _write(OUT / "ca.key", ca_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    _write(OUT / "ca.crt", ca_cert.public_bytes(serialization.Encoding.PEM))
    _write(OUT / "server.key", server_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    _write(OUT / "server.crt", server_cert.public_bytes(serialization.Encoding.PEM))
    _write(OUT / "client.key", client_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    _write(OUT / "client.crt", client_cert.public_bytes(serialization.Encoding.PEM))
    print(f"证书已写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
