"""TRAE PKCE(S256) 单测：verifier/challenge 格式与可验证性。"""
import base64
import hashlib

from app.auth.trae.pkce import gen_code_verifier, s256_challenge


class TestPkce:
    def test_verifier_is_base64url(self):
        v = gen_code_verifier()
        # base64url 无填充
        assert v == base64.urlsafe_b64encode(
            base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))).decode("ascii").rstrip("=")
        # RFC 7636: 43~128 字符（48B → 64 字符）
        assert 43 <= len(v) <= 128

    def test_challenge_matches_verifier(self):
        v = gen_code_verifier()
        c = s256_challenge(v)
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(v.encode("utf-8")).digest()).decode("ascii").rstrip("=")
        assert c == expected

    def test_challenge_deterministic(self):
        v = "abc123"
        assert s256_challenge(v) == s256_challenge(v)
        assert s256_challenge(v) != s256_challenge("abc124")
