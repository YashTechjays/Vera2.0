"""The success envelope (API Contract §7.1)."""

from control_plane.responses import ResponseModel, ResponseStatus, ok


def test_ok_builds_success_envelope() -> None:
    env = ok({"x": 1})
    assert env.status is ResponseStatus.SUCCESS
    assert env.data == {"x": 1}
    assert env.error_code is None
    assert env.description is None
    assert env.message == "Operation completed successfully."


def test_ok_accepts_custom_message_and_none_data() -> None:
    env = ok(None, message="Logged out.")
    assert env.data is None
    assert env.message == "Logged out."
    assert env.status is ResponseStatus.SUCCESS


def test_success_serializes_to_contract_shape() -> None:
    dumped = ok([1, 2]).model_dump(mode="json")
    assert dumped == {
        "data": [1, 2],
        "status": "SUCCESS",
        "message": "Operation completed successfully.",
        "error_code": None,
        "description": None,
    }


def test_response_model_is_generic() -> None:
    env: ResponseModel[int] = ResponseModel[int](data=7)
    assert env.data == 7
