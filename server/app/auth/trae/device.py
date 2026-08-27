"""TRAE 设备签名工具 — EC P-256 密钥对与 DeviceProof 生成。

对齐 TRAE SOLO CN out/main.js `auth/common/util.js`：
- `V7e`：generateKeyPairSync("ec", {namedCurve: "P-256"}, PEM)
- `z7e`：ECDSA-SHA256 对 `"{method}\\n{path}\\n{clientId}\\n{refreshToken}\\n{ts}\\n{nonce}"`
  签名，输出 base64。Node crypto.sign 输出 DER 编码，与 cryptography 默认一致。
方案: docs/plan-trae-solo-provider-integration.md §5.1。
"""
from __future__ import annotations

import base64
import secrets
import time
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


def gen_device_keypair() -> dict:
    """生成 EC P-256 密钥对，返回 PEM 串（对齐 Node generateKeyPairSync P-256 PEM）。"""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return {"private_key": private_pem, "public_key": public_pem}


def sign_device_proof(
    method: str,
    path: str,
    client_id: str,
    refresh_token: str,
    private_key_pem: str,
) -> dict:
    """生成 DeviceProof {Signature, Timestamp, Nonce}。

    payload = "\\n".join([method, path, clientId, refreshToken, timestamp, nonce])
    signature = base64(ECDSA-SHA256(payload, privateKey))
    """
    private_key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    timestamp = int(time.time())
    nonce = secrets.token_hex(16)
    payload = "\n".join([method, path, client_id, refresh_token,
                         str(timestamp), nonce]).encode("utf-8")
    signature = private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
    return {
        "Signature": base64.b64encode(signature).decode("ascii"),
        "Timestamp": timestamp,
        "Nonce": nonce,
    }


def device_info(public_key_pem: str, *, device_id: str, machine_id: str,
                ide_version: str, device_brand: str = "", device_model: str = "",
                device_cpu: str = "", os_info: str = "", os_version: str = "",
                platform_code: str = "SOLO_PC") -> dict:
    """构造 ExchangeToken 的 DeviceInfo 字段（对齐 exchangeTokenByAuthCode j()）。

    缺省硬件字段留空即可；服务端校验重点是 DevicePublicKey + DeviceID/MachineID 的稳定性。
    """
    return {
        "DeviceID": device_id,
        "MachineID": machine_id,
        "PlatformCode": platform_code,
        "DeviceType": "PC",
        "DeviceName": "",
        "DeviceModel": device_model,
        "DeviceBrand": device_brand,
        "DeviceCPU": device_cpu,
        "ClientVersion": ide_version,
        "DevicePublicKey": public_key_pem,
        "OSInfo": os_info,
        "OSVersion": os_version,
    }


def compute_machine_id() -> str:
    """本机稳定机器指纹（64hex）——首登录生成后必须固化到 trae_auth 复用。"""
    import hashlib
    import platform
    import uuid

    parts = [
        platform.node() or "",
        platform.machine() or "",
        str(uuid.getnode()),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def parse_expire(value: object, *, relative: bool = False) -> str | None:
    """ExchangeToken 的 TokenExpireAt/Duration → ISO 时间串；None 表示未知。

    TRAE 时间戳为毫秒级 epoch（对齐 main.js X9e：`new Date(e.TokenExpireAt)`，
    JS Date 接收毫秒）；`TokenExpireDuration` 为相对当前时刻的毫秒时长。
    Windows 上 `datetime.fromtimestamp` 对超范围值抛 OSError(22)（已实测），
    因此任何非法/超范围输入都返回 None，绝不抛异常（仅影响预刷新判断，不阻断登录）。
    """
    if value is None:
        return None
    try:
        num = float(str(value))
    except (TypeError, ValueError):
        return str(value)
    try:
        if relative:
            # TokenExpireDuration：相对当前时刻的毫秒时长
            return datetime.fromtimestamp(time.time() + num / 1000.0, timezone.utc).isoformat()
        # TokenExpireAt：绝对毫秒时间戳（> 1e11 ≈ 5138 年，必为毫秒）
        if num > 1e11:
            num = num / 1000.0
        return datetime.fromtimestamp(num, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None
