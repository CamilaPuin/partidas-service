import pytest
from fastapi import HTTPException

from app.routers.matches import get_current_user_id


def test_get_current_user_id_preserves_keycloak_uuid():
    user_id = "11111111-2222-4333-8444-555555555555"

    assert get_current_user_id(user_id) == user_id


@pytest.mark.parametrize("user_id", [None, "", "user/with/path", "user with spaces"])
def test_get_current_user_id_rejects_invalid_external_id(user_id):
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_id(user_id)

    assert exc_info.value.status_code == 403
