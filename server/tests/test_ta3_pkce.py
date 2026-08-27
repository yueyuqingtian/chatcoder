"""ta3 PKCE(SM3) 工具单测：国标向量 + 参考项目契约格式。"""
import re

from app.auth.ta3.pkce import gen_code_verifier, sm3_challenge, sm3_hash, sm3_hex


def test_sm3_standard_vectors():
    # 国标 GB/T 32905-2016 附录 A 测试向量（与参考项目 sm-crypto 输出一致）
    assert sm3_hex(b"") == "1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b"
    assert sm3_hex(b"abc") == "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"
    # 512 位消息分组边界（64 字节，需走第二轮压缩）
    msg = b"abcd" * 16
    assert len(msg) == 64
    digest = sm3_hex(msg)
    assert len(digest) == 64
    assert digest == sm3_hex(msg)  # 幂等


def test_sm3_str_input():
    assert sm3_hex("abc") == sm3_hex(b"abc")


def test_sm3_challenge_matches_hex_base64url():
    """challenge = base64url(SM3(verifier)) 无填充（对齐参考项目 sm3Challenge）。"""
    import base64

    verifier = gen_code_verifier()
    expected = base64.urlsafe_b64encode(sm3_hash(verifier.encode("utf-8"))).decode("ascii").rstrip("=")
    assert sm3_challenge(verifier) == expected
    assert "=" not in sm3_challenge(verifier)


def test_code_verifier_format():
    """verifier：43 字符 base64url（RFC 7636），无填充符。"""
    verifier = gen_code_verifier()
    assert len(verifier) == 43
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", verifier)


def test_verifier_randomness():
    assert gen_code_verifier() != gen_code_verifier()
