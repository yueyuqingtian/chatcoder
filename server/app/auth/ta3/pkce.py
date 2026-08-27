"""PKCE(SM3) 工具 — 参考项目 ta3 登录契约（对齐 ta3-new-coder src/auth/pkce.ts）。

服务端要求 code_challenge_method=SM3（非 SHA256），verifier 为 43 字符 base64url
随机串（32 字节），challenge = base64url(SM3(verifier)) 无填充。

SM3 用 vendored 纯 Python 实现（约 130 行，国标 GB/T 32905-2016），
零 C 依赖 —— 避免 gmssl 在 PyInstaller 打包环境的 hidden imports 问题。
已用国标测试向量验证：
    sm3("")    = 1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b
    sm3("abc") = 66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0
"""
import base64
import hashlib
import secrets
from typing import Union

_MASK32 = 0xFFFFFFFF


def _rotl(x: int, n: int) -> int:
    n %= 32
    return ((x << n) | (x >> (32 - n))) & _MASK32


# SM3 常量
_IV = [
    0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
    0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E,
]
_T = [0x79CC4519] * 16 + [0x7A879D8A] * 48


def _p0(x: int) -> int:
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x: int) -> int:
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def _ff(j: int, x: int, y: int, z: int) -> int:
    if j < 16:
        return x ^ y ^ z
    return (x & y) | (x & z) | (y & z)


def _gg(j: int, x: int, y: int, z: int) -> int:
    if j < 16:
        return x ^ y ^ z
    return (x & y) | ((~x) & z)


def sm3_hash(data: Union[bytes, bytearray, str]) -> bytes:
    """计算 SM3 摘要，返回 32 字节 bytes。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    data = bytes(data)

    # 1. 填充：消息 || 1 || 0* || 64 位比特长度（大端）
    bit_len = len(data) * 8
    data = data + b"\x80"
    while len(data) % 64 != 56:
        data += b"\x00"
    data += bit_len.to_bytes(8, "big")

    # 2. 迭代压缩
    v = list(_IV)
    for i in range(0, len(data), 64):
        block = data[i:i + 64]
        w = [int.from_bytes(block[j * 4:j * 4 + 4], "big") for j in range(16)]
        for j in range(16, 68):
            w.append(
                (_p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15))
                 ^ _rotl(w[j - 13], 7) ^ w[j - 6]) & _MASK32
            )
        wp = [w[j] ^ w[j + 4] for j in range(64)]

        a, b, c, d, e, f, g, h = v
        for j in range(64):
            ss1 = _rotl((_rotl(a, 12) + e + _rotl(_T[j], j)) & _MASK32, 7)
            ss2 = ss1 ^ _rotl(a, 12)
            tt1 = (_ff(j, a, b, c) + d + ss2 + wp[j]) & _MASK32
            tt2 = (_gg(j, e, f, g) + h + ss1 + w[j]) & _MASK32
            d, c, b, a = c, _rotl(b, 9), a, tt1
            h, g, f, e = g, _rotl(f, 19), e, _p0(tt2)
        v = [(x ^ y) & _MASK32 for x, y in zip(v, [a, b, c, d, e, f, g, h])]

    return b"".join(x.to_bytes(4, "big") for x in v)


def sm3_hex(data: Union[bytes, bytearray, str]) -> str:
    """SM3 摘要十六进制串（sm-crypto sm3() 同构输出）。"""
    return sm3_hash(data).hex()


def gen_code_verifier() -> str:
    """verifier：43 字符 base64url 随机串（32 字节 → 43 字符），满足 RFC 7636。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def sm3_challenge(verifier: str) -> str:
    """challenge = base64url(SM3(verifier))，无填充。

    与参考项目 src/auth/pkce.ts:26 sm3Challenge 完全一致：
    sm-crypto sm3(字节数组)→hex，转字节再 base64url。
    """
    return base64.urlsafe_b64encode(sm3_hash(verifier.encode("utf-8"))).decode("ascii").rstrip("=")


# 兼容 hashlib 风格（便于测试/审计）
class _Sm3Context:
    def __init__(self) -> None:
        self._data = bytearray()

    def update(self, data: bytes) -> None:
        self._data.extend(data)

    def digest(self) -> bytes:
        return sm3_hash(bytes(self._data))

    def hexdigest(self) -> str:
        return self.digest().hex()


# 测试向量自检（模块导入时验证一次，防实现漂移）
assert sm3_hex(b"") == "1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b", "SM3 空串向量失败"
assert sm3_hex(b"abc") == "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0", "SM3 abc 向量失败"
assert sm3_hex("abc") == "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0", "SM3 str 入参失败"
