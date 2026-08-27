"""TRAE 设备签名单测：密钥对、DeviceProof 签名算法、过期时间解析。"""
import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.auth.trae.device import (
    compute_machine_id,
    device_info,
    gen_device_keypair,
    parse_expire,
    sign_device_proof,
)


class TestKeypair:
    def test_gen_device_keypair_pem(self):
        kp = gen_device_keypair()
        assert kp["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
        assert kp["public_key"].startswith("-----BEGIN PUBLIC KEY-----")
        # 公钥必须能从私钥导出（一致性）
        private_key = serialization.load_pem_private_key(
            kp["private_key"].encode("ascii"), password=None)
        assert private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii") == kp["public_key"]


class TestDeviceProof:
    def test_signature_verifies_with_public_key(self):
        kp = gen_device_keypair()
        proof = sign_device_proof(
            "POST", "/trae/api/v3/oauth/ExchangeToken",
            "en1oxy7wnw8j9n", "refresh-token-abc", kp["private_key"])
        assert proof["Timestamp"] > 0
        assert len(proof["Nonce"]) == 32  # 16 字节 hex

        payload = "\n".join([
            "POST", "/trae/api/v3/oauth/ExchangeToken",
            "en1oxy7wnw8j9n", "refresh-token-abc",
            str(proof["Timestamp"]), proof["Nonce"],
        ]).encode("utf-8")
        public_key = serialization.load_pem_public_key(kp["public_key"].encode("ascii"))
        sig = base64.b64decode(proof["Signature"])
        # 验签通过 = 签名算法与 Node crypto.sign("sha256") 同构（DER ECDSA-SHA256）
        public_key.verify(sig, payload, ec.ECDSA(hashes.SHA256()))

    def test_signature_changes_with_payload(self):
        kp = gen_device_keypair()
        p1 = sign_device_proof("POST", "/a", "c1", "rt1", kp["private_key"])
        p2 = sign_device_proof("POST", "/a", "c1", "rt1", kp["private_key"])
        # timestamp/nonce 每次不同 → 签名不同
        assert p1["Nonce"] != p2["Nonce"]
        assert p1["Signature"] != p2["Signature"]


class TestDeviceInfo:
    def test_device_info_fields(self):
        kp = gen_device_keypair()
        info = device_info(kp["public_key"], device_id="1804766141240218",
                           machine_id="a" * 64, ide_version="0.1.51")
        assert info["DeviceID"] == "1804766141240218"
        assert info["PlatformCode"] == "SOLO_PC"
        assert info["DeviceType"] == "PC"
        assert info["DevicePublicKey"] == kp["public_key"]
        assert info["ClientVersion"] == "0.1.51"


class TestMachineId:
    def test_compute_machine_id_stable_and_hex(self):
        a = compute_machine_id()
        b = compute_machine_id()
        assert a == b  # 同进程内稳定
        assert len(a) == 64
        int(a, 16)  # 合法 hex


class TestParseExpire:
    def test_epoch_seconds(self):
        assert parse_expire(1786500000).startswith("2026-")
        assert parse_expire(None) is None

    def test_milliseconds_absolute(self):
        """TRAE TokenExpireAt 为毫秒级 epoch（对齐 main.js new Date(...)）。"""
        out = parse_expire(1786500000000)
        assert out.startswith("2026-08-12T02:00:00")

    def test_milliseconds_relative_duration(self):
        """TokenExpireDuration 为相对当前时刻的毫秒时长。"""
        out = parse_expire(3600_000, relative=True)  # 1 小时
        import datetime as _dt

        assert out is not None
        # 约等于 now + 1h
        diff = _dt.datetime.fromisoformat(out) - _dt.datetime.now(_dt.timezone.utc)
        assert _dt.timedelta(hours=0.9) < diff < _dt.timedelta(hours=1.1)

    def test_iso_passthrough(self):
        assert parse_expire("2026-08-12T10:00:00+00:00") == "2026-08-12T10:00:00+00:00"

    def test_out_of_range_returns_none_not_raise(self):
        """Windows fromtimestamp 对超范围值抛 OSError(22)——必须吞掉返回 None。"""
        assert parse_expire(99999999999999999999) is None
        assert parse_expire("1e30") is None
