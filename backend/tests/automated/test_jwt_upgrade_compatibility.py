"""Pre-upgrade python-jose tokens remain readable without algorithm fallback."""
import pytest
import jwt

from app.security import decode_token

SECRET = 'compatibility-fixture-secret-key-not-production-64-bytes-recommended'
# Frozen vectors generated with the former python-jose encoder, not PyJWT.
PREFIX = 'eyJzdWIiOiI0MiIsInJvbGUiOiJ0ZWFjaGVyIiwidHlwZSI6ImFjY2VzcyIsImp0aSI6InByZS11cGdyYWRlLWpvc2Utc2Vzc2lvbiIsImV4cCI6NDEwMjQ0NDgwMCwic3YiOjN9'
VECTORS = {
    'HS256': 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.' + PREFIX + '.lXctdzVn8n4KLzkyruP_URIKuZCIDELGNdg5EtbaY_g',
    'HS384': 'eyJhbGciOiJIUzM4NCIsInR5cCI6IkpXVCJ9.' + PREFIX + '.UOjsd5VQiGa3uwTdnV-1Ps7JE7r3iR5koDOGgLRgw8l6cZY3kUFAx1RpHyFcLTSs',
    'HS512': 'eyJhbGciOiJIUzUxMiIsInR5cCI6IkpXVCJ9.' + PREFIX + '.thBhvRiib7yMcooBAmtv6FUOmwwgcyqa_C3SCfrE8zQmLs9w1EFd4KKDOP__k4jZZzLGpBvUNByJ3yM5TKSQjA',
}


@pytest.mark.parametrize('algorithm', VECTORS)
def test_pre_upgrade_tokens_preserve_identity_and_revocation_claims(algorithm):
    payload = decode_token(VECTORS[algorithm], SECRET, algorithm)
    assert payload == {'sub': '42', 'role': 'teacher', 'type': 'access',
                       'jti': 'pre-upgrade-jose-session', 'exp': 4102444800, 'sv': 3}


@pytest.mark.parametrize('algorithm', ['HS384', 'HS512', 'RS256', 'none'])
def test_configured_algorithm_is_never_inferred_from_token_header(algorithm):
    with pytest.raises(ValueError):
        decode_token(VECTORS['HS256'], SECRET, algorithm)


@pytest.mark.parametrize('claims', [
    {'sub': 42}, {'sub': '0'}, {'sub': '-1'}, {'sub': '１２'}, {'sub': '42.0'},
    {'type': 'admin'}, {'type': None}, {'jti': 7}, {'jti': ''},
    {'exp': float('nan')}, {'exp': float('inf')}, {'exp': float('-inf')},
    {'exp': {}}, {'exp': []}, {'exp': None}, {'exp': True},
    {'sv': True}, {'sv': 0}, {'sv': '3'},
])
def test_invalid_identity_expiry_and_session_claims_fail_closed(claims):
    payload = {'sub': '42', 'type': 'access', 'jti': 'fixture', 'exp': 4102444800, 'sv': 3}
    payload.update(claims)
    token = jwt.encode(payload, SECRET, algorithm='HS256')
    with pytest.raises(ValueError):
        decode_token(token, SECRET, 'HS256')


def test_expired_old_token_only_decodes_when_expiry_check_explicitly_disabled():
    token = jwt.encode({'sub': '42', 'type': 'refresh', 'jti': 'fixture', 'exp': 1, 'sv': 3},
                       SECRET, algorithm='HS256')
    with pytest.raises(ValueError):
        decode_token(token, SECRET, 'HS256')
    assert decode_token(token, SECRET, 'HS256', verify_exp=False)['exp'] == 1
